"""Test stand: firmware Klipper modules, firmware Moonraker, real Spoolman.

Environment:
    U1BT_FIRMWARE      firmware tree name under firmware/
    U1BT_SERVER_IMAGE  Spoolman image, by default the 0.26.1 release
    U1BT_RUN_DIR       directory for logs of this run
"""

import hashlib
import itertools
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from proxy import Proxy

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / '.venv' / 'bin' / 'python'
SPOOLMAN_IMAGE = 'ghcr.io/donkie/spoolman:0.26.1'
SOCKET_BASE = Path('/run/user/%d/u1bt' % os.getuid())
_counter = itertools.count()


class StandError(RuntimeError):
    pass


PORT_BLOCK = 400
_port_lock = threading.Lock()
_next_port = None


def free_port():
    """A free local port for one server of this test.

    The suite runner gives each parallel test its own block of ports below
    32768, where the system takes ports for outgoing connections. So two
    tests never pick the same port, and a client connection cannot take the
    port before the server starts listening on it.
    """
    global _next_port
    base = os.environ.get('U1BT_PORT_BASE')
    if not base:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            return probe.getsockname()[1]
    base = int(base)
    with _port_lock:
        if _next_port is None:
            # Tests that run one after another in a block start at
            # different ports.
            _next_port = os.getpid() % PORT_BLOCK
        for _ in range(PORT_BLOCK):
            port = base + _next_port
            _next_port = (_next_port + 1) % PORT_BLOCK
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', port))
                except OSError:
                    continue
            return port
    raise StandError('No free port in the block that starts at %d' % base)


def http(method, url, body=None, timeout=15):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw) if raw else None
        except ValueError:
            return error.code, raw.decode(errors='replace')


def wait_for(check, timeout, description):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except Exception as error:  # the process may still be starting
            last_error = error
        time.sleep(.1)
    raise StandError('Timed out waiting for %s (last error: %s)' % (description, last_error))


class Tag:
    """A physical RFID tag.  `parsed` tags carry filament data for OpenRFID."""

    def __init__(self, uid, parsed=False):
        self.uid = uid.upper()
        self.parsed = parsed

    def card_uid(self):
        return list(bytes.fromhex(self.uid))


class Spoolman:
    def __init__(self, image, workdir, label=''):
        self.image = image
        self.name = 'u1bt-%d-%d' % (os.getpid(), next(_counter))
        self.port = free_port()
        self.url = 'http://127.0.0.1:%d' % self.port
        self.log = workdir / ('spoolman%s.log' % ('-' + label if label else ''))

    def start(self):
        subprocess.run(['podman', 'run', '-d', '--rm', '--pull=never', '--name', self.name,
                        '-p', '127.0.0.1:%d:8000' % self.port, self.image],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        wait_for(lambda: self.api('GET', '/api/v1/info')[0] == 200, 60, 'Spoolman to start')

    def stop(self):
        with open(self.log, 'wb') as stream:
            subprocess.run(['podman', 'logs', self.name], stdout=stream, stderr=stream, timeout=30)
        subprocess.run(['podman', 'rm', '-f', '-t', '1', self.name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)

    def api(self, method, path, body=None):
        return http(method, self.url + path, body)

    def ok(self, method, path, body=None):
        status, value = self.api(method, path, body)
        if status not in (200, 201):
            raise StandError('Spoolman %s %s failed: %s %s' % (method, path, status, value))
        return value


class OpenRFID:
    """Emulates OpenRFID in the `openrfid` mode used on the test printer.

    OpenRFID watches `filament_detect.state` through Moonraker and reads a
    slot when its state becomes 1.  It then posts the result to
    `/printer/filament_detect/set` with the bodies from
    `openrfid_u1_base.cfg` and `openrfid_u1_vendor.cfg`.
    """

    def __init__(self, rig):
        self.rig = rig
        self.override = {}
        self.reads = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(5)

    def _run(self):
        previous = [None] * 4
        while not self._stop.is_set():
            try:
                status = self.rig.query({'filament_detect': ['state']}, timeout=2)
                states = status['filament_detect']['state']
            except Exception:
                previous = [None] * 4
                time.sleep(.1)
                continue
            for channel, state in enumerate(states):
                if state == 1 and previous[channel] != 1:
                    threading.Thread(target=self._read, args=(channel,), daemon=True).start()
            previous = list(states)
            time.sleep(.1)

    def _read(self, channel):
        time.sleep(.3)
        slot = self.rig.slots[channel]
        tag = slot['tag'] if slot['present'] else None
        if self.override.get(channel) == 'empty':
            del self.override[channel]
            tag = None
        if tag is None:
            body = {'channel': channel}
        elif tag.parsed:
            body = {'channel': channel, 'info': {
                'VENDOR': 'Snapmaker', 'MAIN_TYPE': 'PLA', 'SUB_TYPE': 'SnapSpeed',
                'HOTEND_MIN_TEMP': 190, 'HOTEND_MAX_TEMP': 230, 'BED_TEMP': 60,
                'ALPHA': 255, 'RGB_1': 0xE2DEDB,
                'CARD_UID': tag.card_uid(), 'CARD_TYPE': 'M1'}}
        else:
            body = {'channel': channel, 'info': {'CARD_UID': tag.card_uid(), 'CARD_TYPE': 'M1'}}
        self.reads.append({'time': time.time(), 'body': body})
        try:
            self.rig.moonraker_api('POST', '/printer/filament_detect/set', body)
        except Exception as error:
            self.reads.append({'time': time.time(), 'error': str(error)})


class Rig:
    def __init__(self, test_id, firmware=None, server_image=None, spoollink=True,
                 builtin_reader=False, sql_trace=False, moonraker_spoollink=True,
                 force_generic_vendor=False):
        self.test_id = test_id
        self.firmware = firmware or os.environ.get('U1BT_FIRMWARE', 'built')
        self.fw = ROOT / 'firmware' / self.firmware / 'rootfs'
        if not self.fw.is_dir():
            raise StandError('Unknown firmware tree: %s' % self.fw)
        run_dir = Path(os.environ.get('U1BT_RUN_DIR', ROOT / 'runs' / 'manual'))
        self.workdir = run_dir / self.firmware / test_id
        if self.workdir.exists():
            shutil.rmtree(self.workdir)
        self.workdir.mkdir(parents=True)
        short = hashlib.sha1(str(self.workdir).encode()).hexdigest()[:10]
        self.sockets = SOCKET_BASE / short
        if self.sockets.exists():
            shutil.rmtree(self.sockets)
        self.sockets.mkdir(parents=True)
        self.spoolman = Spoolman(server_image or os.environ.get('U1BT_SERVER_IMAGE', SPOOLMAN_IMAGE),
                                 self.workdir)
        self.proxy = None
        self.spoollink = spoollink
        self.moonraker_spoollink = moonraker_spoollink
        self.force_generic_vendor = force_generic_vendor
        self.retired = []
        self.builtin_reader = builtin_reader
        self.sql_trace = sql_trace
        self.slots = [{'present': False, 'tag': None} for _ in range(4)]
        self.klippy = None
        self.moonraker = None
        self.moonraker_port = free_port()
        self.rfid = OpenRFID(self)
        self.vendor = self.filament = None
        self.spools = {}

    # -- lifecycle ---------------------------------------------------------

    def start_spoolman(self):
        self.spoolman.start()
        self.proxy = Proxy(self.spoolman.url)
        self.proxy_port = free_port()
        self.proxy.start(self.proxy_port)
        self._prepare_spoolman()

    def switch_spoolman(self):
        """Point Moonraker at another, empty Spoolman server.

        The old server keeps running, so the test can still read it through
        `retired`. Moonraker must be restarted to use the new configuration.
        """
        self.retired.append({'spoolman': self.spoolman, 'proxy': self.proxy,
                             'spools': dict(self.spools)})
        self.spoolman = Spoolman(self.spoolman.image, self.workdir, 'second')
        self.spools = {}
        self.start_spoolman()
        self._write_moonraker_config()

    def _prepare_spoolman(self):
        self.spoolman.ok('POST', '/api/v1/field/spool/card_uids',
                         {'name': 'Card UIDs', 'field_type': 'text', 'order': 1,
                          'default_value': json.dumps('')})
        self.spoolman.ok('POST', '/api/v1/field/filament/variant',
                         {'name': 'Variant', 'field_type': 'text', 'order': 1,
                          'default_value': json.dumps('')})
        self.vendor = self.spoolman.ok('POST', '/api/v1/vendor', {'name': 'Snapmaker'})['id']
        self.filament = self.spoolman.ok('POST', '/api/v1/filament', {
            'name': 'PLA White', 'vendor_id': self.vendor, 'material': 'PLA',
            'density': 1.24, 'diameter': 1.75, 'weight': 1000, 'color_hex': 'FFFFFF'})['id']

    def start_printer(self):
        self._write_klipper_config()
        self._write_moonraker_config()
        self.start_klipper()
        self.start_moonraker()
        if not self.builtin_reader:
            self.rfid.start()

    def start(self):
        self.start_spoolman()
        self.start_printer()

    def close(self):
        errors = []
        for action in (self.rfid.stop, lambda: self._stop_process('moonraker', signal.SIGTERM),
                       lambda: self._stop_process('klippy', signal.SIGTERM)):
            try:
                action()
            except Exception as error:
                errors.append(error)
        if self.proxy is not None:
            self.proxy.stop()
        self.spoolman.stop()
        (self.workdir / 'proxy-requests.json').write_text(
            json.dumps(self.proxy.requests() if self.proxy else [], indent=1, default=str))
        for index, old in enumerate(self.retired):
            old['proxy'].stop()
            old['spoolman'].stop()
            (self.workdir / ('proxy-requests-%d.json' % index)).write_text(
                json.dumps(old['proxy'].requests(), indent=1, default=str))
        (self.workdir / 'openrfid-reads.json').write_text(json.dumps(self.rfid.reads, indent=1))
        shutil.rmtree(self.sockets, ignore_errors=True)

    def _write_klipper_config(self):
        config = self.workdir / 'klipper'
        config.mkdir(exist_ok=True)
        text = '[filament_detect]\n\n[print_task_config]\n'
        if self.spoollink:
            shutil.copy(self.fw / 'usr/local/share/spoollink/klipper.cfg', config / 'spoollink.cfg')
            text += '\n[include spoollink.cfg]\n'
        (config / 'printer.cfg').write_text(text)

    def _write_moonraker_config(self):
        data = self.workdir / 'moonraker'
        (data / 'config' / 'snapmaker').mkdir(parents=True, exist_ok=True)
        cache = self.workdir / 'spoollink-cache'
        cache.mkdir(exist_ok=True)
        proxy = 'http://127.0.0.1:%d' % self.proxy_port
        text = ('[server]\nhost: 127.0.0.1\nport: %d\nklippy_uds_address: %s\n\n'
                '[authorization]\ntrusted_clients:\n    127.0.0.0/8\n\n'
                '[spoolman]\nserver: %s\nsync_rate: 5\n'
                % (self.moonraker_port, self.sockets / 'klippy.sock', proxy))
        if self.moonraker_spoollink:
            text += ('\n[spoollink]\nserver: %s\ncache_dir: %s\nforce_generic_vendor: %s\n'
                     % (proxy, cache, 'true' if self.force_generic_vendor else 'false'))
        (data / 'config' / 'moonraker.conf').write_text(text)

    def start_klipper(self):
        physical = {'present': [slot['present'] for slot in self.slots],
                    'builtin_reader': self.builtin_reader,
                    'tags': {str(i): {'uid': slot['tag'].uid} for i, slot in enumerate(self.slots)
                             if slot['present'] and slot['tag'] is not None}}
        (self.workdir / 'physical.json').write_text(json.dumps(physical))
        out = open(self.workdir / 'klippy.out', 'ab')
        self.klippy = subprocess.Popen(
            [str(PYTHON), '-B', str(ROOT / 'stand' / 'simklippy.py'),
             str(self.fw / 'home/lava/klipper/klippy'), str(self.workdir / 'klipper' / 'printer.cfg'),
             str(self.sockets / 'klippy.sock'), str(self.workdir / 'klippy.log'),
             str(self.workdir / 'physical.json')],
            cwd=self.workdir, stdout=out, stderr=out)
        wait_for(lambda: self.klippy_call('info')['state'] == 'ready', 30, 'Klipper to be ready')

    def start_moonraker(self):
        env = dict(os.environ, PYTHONPATH=str(self.fw / 'home/lava/moonraker'))
        if self.sql_trace:
            env['PYTHONPATH'] += os.pathsep + str(ROOT / 'stand' / 'sqltrace')
            env['U1BT_SQL_TRACE'] = str(self.workdir / 'sql-trace.log')
        out = open(self.workdir / 'moonraker.out', 'ab')
        self.moonraker = subprocess.Popen(
            [str(PYTHON), '-B', '-m', 'moonraker', '-d', str(self.workdir / 'moonraker'),
             '-l', str(self.workdir / 'moonraker.log'), '-u', str(self.sockets / 'moonraker.sock'),
             '-t', str(self.workdir / 'moonraker-tmp')],
            cwd=self.workdir, env=env, stdout=out, stderr=out)

        def ready():
            info = self.moonraker_api('GET', '/server/info')
            return (info['klippy_state'] == 'ready'
                    and self.moonraker_api('GET', '/server/spoolman/status')['spoolman_connected'])
        wait_for(ready, 60, 'Moonraker to connect to Klipper and Spoolman')
        # Let the components finish their klippy_ready handlers.
        time.sleep(1.)

    def _stop_process(self, name, sig):
        process = getattr(self, name)
        if process is None:
            return
        if process.poll() is None:
            process.send_signal(sig)
            try:
                process.wait(20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(10)
        setattr(self, name, None)

    def restart_moonraker(self, kill=False):
        self._stop_process('moonraker', signal.SIGKILL if kill else signal.SIGTERM)
        self.start_moonraker()

    def shutdown_moonraker(self):
        self._stop_process('moonraker', signal.SIGTERM)

    def stop_klipper(self):
        self._stop_process('klippy', signal.SIGTERM)

    def kill_moonraker(self):
        self._stop_process('moonraker', signal.SIGKILL)

    def shutdown(self):
        """The printer turns off: services stop in shutdown order."""
        self._stop_process('moonraker', signal.SIGTERM)
        self._stop_process('klippy', signal.SIGTERM)

    def boot(self):
        self.start_klipper()
        self.start_moonraker()

    def restart_klipper(self):
        """Klipper `RESTART`: the host process reloads its configuration."""
        started = self.klippy_call('info')['process_id']
        self.gcode('RESTART')
        time.sleep(1.5)
        wait_for(lambda: self.klippy_call('info')['state'] == 'ready', 30, 'Klipper restart')
        assert self.klippy_call('info')['process_id'] == started
        wait_for(lambda: self.moonraker_api('GET', '/server/info')['klippy_state'] == 'ready',
                 30, 'Moonraker to reconnect')
        time.sleep(1.)

    def reboot(self):
        """Both services stop in shutdown order and start again."""
        self._stop_process('moonraker', signal.SIGTERM)
        self._stop_process('klippy', signal.SIGTERM)
        self.start_klipper()
        self.start_moonraker()

    # -- Klipper and Moonraker access ------------------------------------

    def klippy_call(self, method, params=None, timeout=20):
        with socket.socket(socket.AF_UNIX) as stream:
            stream.settimeout(timeout)
            stream.connect(str(self.sockets / 'klippy.sock'))
            stream.sendall(json.dumps({'id': 1, 'method': method, 'params': params or {}}).encode()
                           + b'\x03')
            buffer = b''
            while True:
                chunk = stream.recv(65536)
                if not chunk:
                    raise StandError('Klipper closed the API connection')
                buffer += chunk
                while b'\x03' in buffer:
                    message, buffer = buffer.split(b'\x03', 1)
                    reply = json.loads(message)
                    if reply.get('id') == 1:
                        if 'error' in reply:
                            raise StandError('Klipper %s failed: %s' % (method, reply['error']))
                        return reply['result']

    def moonraker_api(self, method, path, body=None, timeout=15):
        status, value = http(method, 'http://127.0.0.1:%d%s' % (self.moonraker_port, path), body,
                             timeout)
        if status != 200:
            raise StandError('Moonraker %s %s: %s %s' % (method, path, status, value))
        return value['result']

    def query(self, objects, timeout=10):
        status, value = http('POST', 'http://127.0.0.1:%d/printer/objects/query'
                             % self.moonraker_port, {'objects': objects}, timeout)
        if status != 200:
            raise StandError('query failed: %s %s' % (status, value))
        return value['result']['status']

    def gcode(self, script):
        return self.moonraker_api('POST', '/printer/gcode/script?script='
                                  + urllib.parse.quote(script), timeout=60)

    # -- physical actions ------------------------------------------------

    def load(self, channel, tag=None):
        """Place a spool before Klipper starts."""
        self.slots[channel] = {'present': True, 'tag': tag}

    def insert(self, channel, tag=None):
        self.slots[channel] = {'present': True, 'tag': tag}
        self.klippy_call('sim/sensor', {'index': channel, 'present': True})

    def remove(self, channel):
        self.slots[channel] = {'present': False, 'tag': None}
        self.klippy_call('sim/sensor', {'index': channel, 'present': False})

    def swap_while_off(self, channel, tag=None):
        """Change the spool while the printer is off."""
        assert self.klippy is None and self.moonraker is None
        self.slots[channel] = {'present': True, 'tag': tag}

    def reread(self, channel):
        self.gcode('FILAMENT_DT_UPDATE CHANNEL=%d' % channel)

    def activate(self, channel):
        # A real tool change parks the head and picks another one, which takes
        # seconds. Status updates reach Moonraker every 0.25 s, so one second on
        # each side keeps the last move and the change in separate updates.
        time.sleep(1.)
        self.klippy_call('sim/activate', {'index': channel})
        time.sleep(1.)

    def extrude(self, moves, interval=.05):
        if isinstance(moves, (int, float)):
            moves = [moves]
        return self.klippy_call('sim/extrude', {'moves': moves, 'interval': interval},
                                timeout=max(20, len(moves) * interval * 2 + 10))

    def print_state(self, state):
        self.klippy_call('sim/print_state', {'state': state})

    def select(self, channel, spool_id):
        self.gcode('SET_SPOOL_ID LANE=E%d SPOOL_ID=%d' % (channel, spool_id))

    # -- Spoolman ------------------------------------------------------------

    def create_spool(self, name, uids=(), filament=None):
        body = {'filament_id': filament or self.filament, 'initial_weight': 1000, 'spool_weight': 200}
        if uids:
            body['extra'] = {'card_uids': json.dumps(','.join(uids))}
        spool = self.spoolman.ok('POST', '/api/v1/spool', body)
        self.spools[name] = spool['id']
        return spool['id']

    def card_uids(self, spool_id):
        spool = self.spoolman.ok('GET', '/api/v1/spool/%d' % spool_id)
        raw = (spool.get('extra') or {}).get('card_uids') or ''
        if raw.startswith('"'):
            raw = json.loads(raw)
        return [uid.strip().upper() for uid in raw.split(',') if uid.strip()]

    def set_uids(self, spool_id, uids):
        self.spoolman.ok('PATCH', '/api/v1/spool/%d' % spool_id,
                         {'extra': {'card_uids': json.dumps(','.join(uids))}})

    def add_uid(self, spool_id, uid):
        uids = self.card_uids(spool_id) + [uid]
        self.spoolman.ok('PATCH', '/api/v1/spool/%d' % spool_id,
                         {'extra': {'card_uids': json.dumps(','.join(uids))}})

    def delete_spool(self, spool_id):
        status, value = self.spoolman.api('DELETE', '/api/v1/spool/%d' % spool_id)
        if status != 200:
            raise StandError('delete failed: %s %s' % (status, value))

    def used(self):
        """Used length of every spool known to the test, by name."""
        result = {}
        for name, spool_id in self.spools.items():
            status, spool = self.spoolman.api('GET', '/api/v1/spool/%d' % spool_id)
            result[name] = spool['used_length'] if status == 200 else None
        return result

    # -- observations ----------------------------------------------------

    def links(self):
        return self.query({'print_task_config': ['filament_spool_id']})[
            'print_task_config']['filament_spool_id']

    def channel_vendor(self, channel):
        return self.query({'print_task_config': ['filament_vendor']})[
            'print_task_config']['filament_vendor'][channel]

    def create_filament(self, vendor_name):
        vendor = self.spoolman.ok('POST', '/api/v1/vendor', {'name': vendor_name})['id']
        return self.spoolman.ok('POST', '/api/v1/filament', {
            'name': 'PLA %s' % vendor_name, 'vendor_id': vendor, 'material': 'PLA',
            'density': 1.24, 'diameter': 1.75, 'weight': 1000, 'color_hex': 'FFFF00'})['id']

    def responses(self):
        return self.klippy_call('sim/state')['responses']

    def screen_errors(self):
        return self.query({'exception_manager': None})['exception_manager']['exceptions']
