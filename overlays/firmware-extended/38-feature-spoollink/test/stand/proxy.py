"""HTTP and websocket proxy between Moonraker and Spoolman with fault injection.

Faults are rules matched by method and path.  Actions:

- ``status``: answer with the given HTTP status without reaching Spoolman;
- ``drop_after_commit``: forward the request, wait for Spoolman's answer, then
  close the client connection without replying;
- ``hold_after_commit``: forward the request and keep Spoolman's answer until
  the rule is released;
- ``json_merge``: forward the request and add the given keys to Spoolman's
  JSON answer.

``refuse()`` closes the listening socket and every open connection, so
Moonraker sees Spoolman as down.  ``accept()`` opens the socket again.
Every request is logged with its outcome.
"""

import asyncio
import json
import re
import threading
import time

import tornado.httpclient
import tornado.httpserver
import tornado.iostream
import tornado.web
import tornado.websocket

HOP_HEADERS = {'connection', 'keep-alive', 'transfer-encoding', 'content-length',
               'content-encoding', 'server', 'date'}


class Rule:
    def __init__(self, name, method, path, action, count=None, status=503, delay=0., merge=None):
        self.name = name
        self.merge = merge or {}
        self.delay = delay
        self.method = method
        self.path = re.compile(path)
        self.action = action
        self.count = count
        self.status = status
        self.hits = 0
        self.released = asyncio.Event()

    def matches(self, method, path):
        if self.count is not None and self.hits >= self.count:
            return False
        return (self.method in ('*', method)) and self.path.fullmatch(path) is not None


class Proxy:
    def __init__(self, upstream):
        self.upstream = upstream.rstrip('/')
        self.ws_upstream = 'ws' + self.upstream[len('http'):]
        self.loop = asyncio.new_event_loop()
        self.rules = []
        self.log = []
        self.lock = threading.Lock()
        self.sockets = []
        self.server = None
        self.websockets = set()
        self.port = None
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    # -- control from the test thread -------------------------------------

    def call(self, coroutine, timeout=30):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def start(self, port):
        self.port = port
        self.call(self._start())

    def stop(self):
        self.call(self._refuse())
        self.loop.call_soon_threadsafe(self.loop.stop)

    def refuse(self):
        self.call(self._refuse())

    def accept(self):
        self.call(self._start())

    def add_rule(self, name, method, path, action, count=None, status=503, delay=0., merge=None):
        """Actions: 'status' answers with `status` without asking Spoolman;
        'delay' waits `delay` seconds and then forwards; 'drop_after_commit'
        forwards and closes the connection without the reply;
        'hold_after_commit' forwards and keeps the reply until `release`;
        'json_merge' forwards and adds the keys of `merge` to the JSON reply."""
        async def add():
            rule = Rule(name, method, path, action, count, status, delay, merge)
            self.rules.append(rule)
            return rule
        return self.call(add())

    def remove_rule(self, name):
        async def remove():
            for rule in [r for r in self.rules if r.name == name]:
                rule.released.set()
                self.rules.remove(rule)
        self.call(remove())

    def release(self, name):
        async def release():
            for rule in self.rules:
                if rule.name == name:
                    rule.released.set()
        self.call(release())

    def hits(self, name):
        async def hits():
            return sum(rule.hits for rule in self.rules if rule.name == name)
        return self.call(hits())

    def requests(self, method=None, path=None):
        pattern = re.compile(path) if path else None
        with self.lock:
            rows = list(self.log)
        return [row for row in rows if (method is None or row['method'] == method)
                and (pattern is None or pattern.fullmatch(row['path']))]

    # -- event loop side -------------------------------------------------

    async def _start(self):
        if self.server is not None:
            return
        app = tornado.web.Application([
            (r'/api/v1/spool', SpoolSocket, {'proxy': self}),
            (r'.*', Forward, {'proxy': self}),
        ])
        self.server = tornado.httpserver.HTTPServer(app)
        self.server.listen(self.port, '127.0.0.1')

    async def _refuse(self):
        server, self.server = self.server, None
        if server is None:
            return
        server.stop()
        for socket in list(self.websockets):
            socket.close(1001, 'proxy refused')
        await server.close_all_connections()

    def record(self, row):
        with self.lock:
            self.log.append(row)

    def match(self, method, path):
        for rule in self.rules:
            if rule.matches(method, path):
                rule.hits += 1
                return rule
        return None


class Forward(tornado.web.RequestHandler):
    SUPPORTED_METHODS = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')

    def initialize(self, proxy):
        self.proxy = proxy

    def check_xsrf_cookie(self):
        pass

    async def _handle(self):
        request = self.request
        path = request.path
        body = request.body or None
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        row = {'time': time.time(), 'method': request.method, 'path': path,
               'query': request.query, 'body': parsed, 'outcome': None}
        self.proxy.record(row)
        rule = self.proxy.match(request.method, path)
        if rule is not None and rule.action == 'status':
            row['outcome'] = 'status %d (%s)' % (rule.status, rule.name)
            self.set_status(rule.status)
            self.set_header('Content-Type', 'application/json')
            self.finish(json.dumps({'message': 'fault injected by test proxy'}))
            return
        if rule is not None and rule.action == 'delay':
            row['outcome'] = 'delayed %.1f s (%s)' % (rule.delay, rule.name)
            await asyncio.sleep(rule.delay)
        headers = {k: v for k, v in request.headers.get_all()
                   if k.lower() not in HOP_HEADERS and k.lower() != 'host'}
        upstream = tornado.httpclient.HTTPRequest(
            self.proxy.upstream + request.uri, method=request.method, headers=headers,
            body=body if request.method in ('POST', 'PUT', 'PATCH') else None,
            allow_nonstandard_methods=True, request_timeout=30)
        client = tornado.httpclient.AsyncHTTPClient()
        response = await client.fetch(upstream, raise_error=False)
        row['status'] = response.code
        if rule is not None and rule.action == 'drop_after_commit':
            row['outcome'] = 'dropped after commit (%s)' % rule.name
            self.request.connection.close()
            self._finished = True
            return
        if rule is not None and rule.action == 'hold_after_commit':
            row['outcome'] = 'held after commit (%s)' % rule.name
            await rule.released.wait()
        elif row['outcome'] is None:
            row['outcome'] = 'forwarded'
        payload = response.body or b''
        if rule is not None and rule.action == 'json_merge' and response.code == 200:
            reply = json.loads(payload)
            reply.update(rule.merge)
            payload = json.dumps(reply).encode()
            row['outcome'] = 'forwarded with added keys (%s)' % rule.name
        self.set_status(response.code if response.code != 599 else 502)
        for key, value in response.headers.get_all():
            if key.lower() not in HOP_HEADERS:
                self.add_header(key, value)
        try:
            self.finish(payload)
        except tornado.iostream.StreamClosedError:
            row['outcome'] += ', client gone'

    get = post = put = patch = delete = _handle


class SpoolSocket(tornado.websocket.WebSocketHandler):
    """Websocket relay for `/api/v1/spool`; plain requests on that path are forwarded."""

    SUPPORTED_METHODS = Forward.SUPPORTED_METHODS
    _handle = Forward._handle
    post = put = patch = delete = Forward._handle

    def initialize(self, proxy):
        self.proxy = proxy
        self.upstream = None

    def check_xsrf_cookie(self):
        pass

    async def get(self, *args, **kwargs):
        if self.request.headers.get('Upgrade', '').lower() != 'websocket':
            return await self._handle()
        return await super().get(*args, **kwargs)

    def check_origin(self, origin):
        return True

    async def open(self):
        self.proxy.websockets.add(self)
        self.upstream = await tornado.websocket.websocket_connect(
            self.proxy.ws_upstream + '/api/v1/spool')
        asyncio.ensure_future(self._relay())

    async def _relay(self):
        while True:
            message = await self.upstream.read_message()
            if message is None:
                self.close(1001, 'upstream closed')
                return
            try:
                await self.write_message(message)
            except tornado.websocket.WebSocketClosedError:
                return

    def on_message(self, message):
        if self.upstream is not None:
            self.upstream.write_message(message)

    def on_close(self):
        self.proxy.websockets.discard(self)
        if self.upstream is not None:
            self.upstream.close()
