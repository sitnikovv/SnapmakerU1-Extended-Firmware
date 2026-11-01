#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Проверки назначения катушки без принтера, сети и базы Spoolman."""

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import AsyncMock


OVERLAY = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LINK = load('moonraker_spoollink', OVERLAY / 'root/home/lava/moonraker/moonraker/components/spoollink.py')
package = types.ModuleType('test_klipper')
package.__path__ = []
sys.modules[package.__name__] = package
protocol = types.ModuleType('test_klipper.filament_protocol')
protocol.FILAMENT_INFO_STRUCT = {}
sys.modules[protocol.__name__] = protocol
ROUTER = load('test_klipper.spoollink', OVERLAY / 'root/home/lava/klipper/klippy/extras/spoollink.py')

UID = '01020304'
OTHER_UID = '05060708'
SPOOL = {'id': 42, 'extra': {'card_uids': json.dumps(UID)},
         'filament': {'material': 'PLA', 'vendor': {'name': 'Bambu'}, 'color_hex': '112233'}}


def tag(uid=UID, event=1.):
    return {'CARD_UID': list(bytes.fromhex(uid)), 'CARD_EVENT_TIME': event}


class WebRequest:
    error = RuntimeError

    def __init__(self, params):
        self.params = copy.deepcopy(params)
        self.reply = None

    def get(self, key, default=None):
        return self.params.get(key, default)

    get_str = get_int = get

    def send(self, reply):
        self.reply = reply


class GCodeCommand:
    def __init__(self, channel):
        self.channel = channel

    def get_int(self, key, **kwargs):
        return self.channel


class Environment:
    error = RuntimeError

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.force_generic = False
        self.info = [tag(), {}, {}, {}]
        self.ids = [42, 0, 0, 0]
        self.reading = [0, 0, 0, 0]
        self.spool_id = None
        self.active_changes = []
        self.pending_reports = {}
        self.handlers = {}
        self.remotes = {}
        self.commands = {}
        self.messages = []
        self.applied = []
        self.requests = []
        self.query_gate = None
        self.apply_gate = None
        self.query_failure = False
        self.data = [copy.deepcopy(SPOOL)]
        self.http_gate = None
        self.http_calls = 0
        self.concurrent = self.max_concurrent = 0
        self.patch_gate = None
        self.patch_started = asyncio.Event()
        self.link = LINK.SpoolLink(self)
        self.router = ROUTER.SpoolLink(self)

    def get_server(self):
        return self

    def get_printer(self):
        return self

    def get(self, key, default=None):
        return {'server': 'http://spoolman.test', 'cache_dir': self.cache_dir}.get(key, default)

    def getboolean(self, key, default):
        return self.force_generic

    def lookup_component(self, name, default=None):
        return self if name in ('http_client', 'klippy_apis', 'spoolman') else default

    def lookup_object(self, name):
        return self

    def register_remote_method(self, name, method):
        self.remotes[name] = method

    def register_event_handler(self, name, callback):
        self.handlers[name] = callback

    def register_command(self, name, command):
        self.commands[name] = command

    def register_endpoint(self, *args):
        pass

    def has_remote_method(self, name):
        return name in self.remotes

    def call_remote_method(self, name, **params):
        self.link._fire(self.remotes[name](**params))

    def get_all_filament_info(self):
        return self.info

    def _rfid_filament_info_update_cb(self, channel, info, is_clear=False):
        self.applied.append((channel, copy.deepcopy(info)))
        self.ids[channel] = info['SPOOL_ID']

    def respond_raw(self, message):
        self.messages.append(message)

    def run_script_from_command(self, script):
        params = dict(token.split('=', 1) for token in script.split()[1:])
        self.ids[int(params['CONFIG_EXTRUDER'])] = int(params['FILAMENT_SPOOL_ID'])

    async def run_gcode(self, *args):
        pass

    def status(self):
        return copy.deepcopy({'filament_detect': {'info': self.info, 'state': self.reading},
                              'print_task_config': {'filament_spool_id': self.ids},
                              'spoollink': self.router.get_status(0)})

    async def query_objects(self, objects):
        snapshot = self.status()
        if self.query_gate is not None:
            await self.query_gate.wait()
        if self.query_failure:
            raise RuntimeError('запрос состояния не удался')
        return snapshot

    async def _send_klippy_request(self, endpoint, params):
        self.requests.append(copy.deepcopy(params))
        if self.apply_gate is not None:
            await self.apply_gate.wait()
        request = WebRequest(params)
        self.router._handle_set(request)
        return request.reply

    async def get_http(self, url, **kwargs):
        self.http_calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        snapshot = copy.deepcopy(self.data)
        try:
            if self.http_gate is not None:
                await self.http_gate.wait()
        finally:
            self.concurrent -= 1
        if '/spool/' in url:
            wanted = int(url.rsplit('/', 1)[1])
            snapshot = next((s for s in snapshot if s['id'] == wanted), None)
        return types.SimpleNamespace(status_code=404 if snapshot is None else 200,
                                     json=lambda: snapshot, text=lambda: '')

    async def request(self, method, url, body):
        self.patch_started.set()
        if self.patch_gate is not None:
            await self.patch_gate.wait()
        spool = next(s for s in self.data if s['id'] == int(url.rsplit('/', 1)[1]))
        spool['extra'].update(body['extra'])
        return types.SimpleNamespace(status_code=200, json=lambda: copy.deepcopy(spool))

    def set_active_spool(self, spool_id):
        self.spool_id = spool_id
        self.active_changes.append(spool_id)
        self.handlers['spoolman:active_spool_set']({'spool_id': spool_id})

    def move(self, length):
        if self.spool_id is not None:
            self.pending_reports[self.spool_id] = self.pending_reports.get(self.spool_id, 0) + length


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.env = Environment(self.cache.name)
        self.link = self.env.link
        self.link.http_client = types.SimpleNamespace(get=self.env.get_http, request=self.env.request)
        self.link._handle_status_update(self.env.status(), 1.)
        await self.drain()
        self.env.applied.clear()
        self.env.active_changes.clear()
        self.env.requests.clear()
        self.env.http_calls = 0

    async def asyncTearDown(self):
        self.link._handle_klippy_disconnect()
        await self.drain()
        self.cache.cleanup()

    async def drain(self, cycles=20):
        for _ in range(cycles):
            await asyncio.sleep(0)

    def refresh(self, event=2., uid=UID, channel=0):
        self.env.info[channel] = tag(uid, event)
        self.env.ids[channel] = 0
        self.link._handle_status_update(self.env.status(), event)

    async def test_refresh_keeps_every_millimeter(self):
        self.env.move(10)
        self.env.http_gate = asyncio.Event()
        self.refresh()
        await self.drain()
        self.assertEqual(self.env.spool_id, 42)
        self.env.move(5)
        self.env.http_gate.set()
        await self.drain()
        self.env.move(7)
        self.assertEqual(self.env.pending_reports, {42: 22})
        self.assertNotIn(None, self.env.active_changes)

    async def test_spool_zero_before_card_event_uses_authoritative_query(self):
        self.env.http_gate = asyncio.Event()
        self.env.info[0] = tag(event=2.)
        self.env.ids[0] = 0
        self.link._handle_status_update({'print_task_config': {'filament_spool_id': self.env.ids}}, 2.)
        await self.drain()
        self.assertEqual(self.env.spool_id, 42)
        self.env.http_gate.set()
        await self.drain()
        self.assertEqual(self.env.spool_id, 42)

    async def test_card_event_before_spool_zero(self):
        self.env.http_gate = asyncio.Event()
        self.env.info[0] = tag(event=2.)
        self.link._handle_status_update({'filament_detect': {'info': self.env.info}}, 2.)
        self.env.ids[0] = 0
        self.link._handle_status_update({'print_task_config': {'filament_spool_id': self.env.ids}}, 2.)
        await self.drain()
        self.assertEqual(self.env.spool_id, 42)
        self.env.http_gate.set()
        await self.drain()

    async def test_plain_zero_without_read_is_respected(self):
        self.env.ids[0] = 0
        self.link._handle_status_update({'print_task_config': {'filament_spool_id': self.env.ids}}, 2.)
        await self.drain()
        self.assertIsNone(self.env.spool_id)
        self.assertEqual(self.env.applied, [])

    async def test_query_failure_releases_assignment(self):
        self.env.query_failure = True
        self.env.ids[0] = 0
        self.link._handle_status_update({'print_task_config': {'filament_spool_id': self.env.ids}}, 2.)
        await self.drain()
        self.assertIsNone(self.env.spool_id)

    async def test_reader_in_progress_holds_until_result(self):
        self.env.reading[0] = 1
        self.env.ids[0] = 0
        self.link._handle_status_update(self.env.status(), 2.)
        await self.drain()
        self.assertEqual(self.env.spool_id, 42)
        self.env.reading[0] = 0
        self.link._handle_status_update(self.env.status(), 3.)
        await self.drain()
        self.assertIsNone(self.env.spool_id)

    async def test_partial_ptc_update_keeps_assignment(self):
        self.link._handle_status_update({'print_task_config': {'extruders_used': [True]}}, 2.)
        await self.drain()
        self.assertEqual(self.env.spool_id, 42)
        self.assertEqual(self.env.http_calls, 0)

    async def test_old_or_repeated_events_do_not_resolve(self):
        self.link._handle_status_update(self.env.status(), 2.)
        self.env.info[0] = tag(event=.5)
        self.link._handle_status_update(self.env.status(), 3.)
        await self.drain()
        self.assertEqual(self.env.http_calls, 0)

    async def test_repeated_refreshes_coalesce(self):
        self.env.http_gate = asyncio.Event()
        self.refresh(2.)
        await self.drain()
        self.refresh(3.)
        self.refresh(4.)
        await self.drain()
        self.assertEqual(self.env.http_calls, 1)
        self.env.http_gate.set()
        await self.drain()
        self.assertEqual(self.env.http_calls, 2)
        self.assertEqual(self.env.max_concurrent, 1)
        self.assertEqual(len(self.env.applied), 1)
        self.assertEqual(self.env.spool_id, 42)

    async def test_changed_uid_rejects_old_lookup_and_cache_write(self):
        self.env.http_gate = asyncio.Event()
        self.refresh()
        await self.drain()
        self.link._delete_cache(UID)
        self.refresh(3., OTHER_UID)
        await self.drain()
        self.assertIsNone(self.env.spool_id)
        self.env.http_gate.set()
        await self.drain()
        self.assertEqual(self.env.applied, [])
        self.assertIsNone(self.link._load_cache(UID))

    async def test_empty_read_preserves_positive_manual_assignment(self):
        self.env.info[0] = tag('', 2.)
        self.link._handle_status_update(self.env.status(), 2.)
        await self.drain()
        self.assertEqual(self.env.spool_id, 42)

    async def test_real_removal_clears_and_rejects_pending_lookup(self):
        self.env.http_gate = asyncio.Event()
        self.refresh()
        await self.drain()
        self.env.info[0] = tag('', 3.)
        self.link._handle_status_update(self.env.status(), 3.)
        await self.drain()
        self.assertIsNone(self.env.spool_id)
        self.env.http_gate.set()
        await self.drain()
        self.assertEqual(self.env.applied, [])

    async def test_clear_during_lookup_does_not_recreate_cache(self):
        self.env.http_gate = asyncio.Event()
        self.refresh()
        await self.drain()
        self.env.router.cmd_SPOOLLINK_CLEAR(GCodeCommand(0))
        await self.drain()
        self.assertIsNone(self.env.spool_id)
        self.assertIsNone(self.link._load_cache(UID))
        self.env.http_gate.set()
        await self.drain()
        self.assertEqual(self.env.applied, [])
        self.assertIsNone(self.link._load_cache(UID))
        self.assertEqual(json.loads(self.env.data[0]['extra']['card_uids']), '')

    async def test_already_sent_apply_is_rejected_after_clear(self):
        self.env.apply_gate = asyncio.Event()
        self.refresh()
        await self.drain()
        self.assertEqual(len(self.env.requests), 1)
        self.env.router.cmd_SPOOLLINK_CLEAR(GCodeCommand(0))
        await self.drain()
        self.env.apply_gate.set()
        await self.drain()
        self.assertEqual(self.env.applied, [])
        self.assertIsNone(self.env.spool_id)

    async def test_already_sent_apply_is_rejected_after_new_card(self):
        self.env.apply_gate = asyncio.Event()
        self.refresh()
        await self.drain()
        self.refresh(3., OTHER_UID)
        self.env.apply_gate.set()
        await self.drain()
        self.assertEqual(self.env.applied, [])

    async def test_clear_preserves_second_uid(self):
        self.env.data[0]['extra']['card_uids'] = json.dumps(UID + ',' + OTHER_UID)
        await self.link._unlink_card(0, UID)
        self.assertEqual(json.loads(self.env.data[0]['extra']['card_uids']), OTHER_UID)

    async def test_clear_waits_for_in_flight_binding(self):
        self.env.data[0]['extra']['card_uids'] = json.dumps(OTHER_UID)
        self.env.patch_gate = asyncio.Event()
        await self.link._resolve_spool(0, spool_id=42, card_uid=UID)
        await self.env.patch_started.wait()
        self.env.router.cmd_SPOOLLINK_CLEAR(GCodeCommand(0))
        await self.drain()
        self.env.patch_gate.set()
        await self.drain()
        self.assertEqual(json.loads(self.env.data[0]['extra']['card_uids']), OTHER_UID)
        self.assertEqual(self.env.applied, [])

    async def test_same_uid_can_be_assigned_again_after_clear(self):
        self.env.router.cmd_SPOOLLINK_CLEAR(GCodeCommand(0))
        await self.drain()
        self.link._handle_status_update(self.env.status(), 2.)
        await self.link._resolve_spool(0, spool_id=42, card_uid=UID, expected_generation=1)
        await self.drain()
        self.assertEqual(self.env.spool_id, 42)
        self.assertEqual(json.loads(self.env.data[0]['extra']['card_uids']), UID)

    async def test_failed_unlink_blocks_automatic_restore(self):
        self.link._retry = AsyncMock(side_effect=RuntimeError('нет сети'))
        await self.link._unlink_card(0, UID)
        self.refresh()
        await self.drain()
        self.assertIsNone(self.env.spool_id)
        self.assertEqual(self.env.applied, [])

    async def test_clear_without_uid_invalidates_manual_lookup(self):
        self.env.info[0] = tag('', 2.)
        self.link._handle_status_update(self.env.status(), 2.)
        await self.drain()
        self.env.http_gate = asyncio.Event()
        await self.link._resolve_spool(0, spool_id=42)
        await self.drain()
        self.env.router.cmd_SPOOLLINK_CLEAR(GCodeCommand(0))
        await self.drain()
        self.env.http_gate.set()
        await self.drain()
        self.assertEqual(self.env.applied, [])
        self.assertIsNone(self.env.spool_id)

    async def test_cache_fallback_during_refresh(self):
        self.link._retry = AsyncMock(side_effect=RuntimeError('нет сети'))
        self.refresh()
        await self.drain()
        self.assertEqual(self.env.spool_id, 42)
        self.assertIn('[cached]', self.env.requests[0]['message'])

    async def test_unknown_card_does_not_restore_old_spool(self):
        self.env.data = []
        self.refresh()
        await self.drain()
        self.assertIsNone(self.env.spool_id)
        self.assertEqual(self.env.applied, [])
        self.assertIsNone(self.link._load_cache(UID))

    async def test_ambiguous_card_does_not_restore_old_spool(self):
        self.env.data.append(dict(copy.deepcopy(SPOOL), id=43))
        self.refresh()
        await self.drain()
        self.assertIsNone(self.env.spool_id)
        self.assertEqual(self.env.applied, [])

    async def test_corrupt_cache_is_not_used(self):
        for content in ('{', '[]', '{"id": "42", "filament": {}}', '{"id": 0, "filament": {}}'):
            with self.subTest(content=content):
                Path(self.link._cache_path(UID)).write_text(content)
                self.assertIsNone(self.link._load_cache(UID))

    async def test_force_generic_vendor_is_preserved(self):
        self.link._force_generic_vendor = True
        self.refresh()
        await self.drain()
        self.assertEqual(self.env.applied[-1][1]['VENDOR'], 'Generic')

    async def test_other_tool_is_selected_during_refresh(self):
        self.env.http_gate = asyncio.Event()
        self.refresh()
        self.env.ids[1] = 43
        self.link._handle_status_update({'toolhead': {'extruder': 'extruder1'},
                                        'print_task_config': {'filament_spool_id': self.env.ids}}, 3.)
        await self.drain()
        self.assertEqual(self.env.spool_id, 43)
        self.env.http_gate.set()
        await self.drain()
        self.assertEqual(self.env.spool_id, 43)

    async def test_disconnect_rejects_old_response(self):
        self.env.http_gate = asyncio.Event()
        self.refresh()
        await self.drain()
        self.link._handle_klippy_disconnect()
        self.env.http_gate.set()
        await self.drain()
        self.assertEqual(self.env.applied, [])
        self.assertEqual(self.link._channels, {})

    async def test_same_uid_newer_event_rejects_already_sent_apply(self):
        self.env.apply_gate = asyncio.Event()
        self.refresh()
        await self.drain()
        self.refresh(3.)
        self.env.apply_gate.set()
        await self.drain()
        self.assertEqual(len(self.env.applied), 1)
        self.assertEqual(self.env.requests[-1]['expected_card_event_time'], 3.)

    async def test_manual_assignment_catches_up_with_subscription(self):
        self.env.info[0] = tag(OTHER_UID, 2.)
        self.env.data[0]['extra']['card_uids'] = json.dumps(OTHER_UID)
        await self.link._resolve_spool(0, spool_id=42, card_uid=OTHER_UID)
        await self.drain()
        self.assertEqual(self.env.spool_id, 42)
        self.assertEqual(len(self.env.applied), 1)

    async def test_manual_assignment_from_stale_clear_generation_is_rejected(self):
        self.env.router.cmd_SPOOLLINK_CLEAR(GCodeCommand(0))
        await self.drain()
        await self.link._resolve_spool(0, spool_id=42, card_uid=UID, expected_generation=0)
        await self.drain()
        self.assertEqual(self.env.applied, [])
        self.assertIsNone(self.env.spool_id)

    async def test_successful_clear_is_not_undone_by_next_scan(self):
        self.env.router.cmd_SPOOLLINK_CLEAR(GCodeCommand(0))
        await self.drain()
        self.refresh()
        await self.drain()
        self.assertIsNone(self.env.spool_id)
        self.assertEqual(self.env.applied, [])

    async def test_late_verification_does_not_undo_new_assignment(self):
        self.env.query_gate = asyncio.Event()
        self.env.ids[0] = 0
        self.link._handle_status_update({'print_task_config': {'filament_spool_id': self.env.ids}}, 2.)
        await self.drain()
        self.refresh(3.)
        await self.drain()
        self.env.query_gate.set()
        await self.drain()
        self.assertEqual(self.env.spool_id, 42)

    async def test_assignment_without_card_still_works(self):
        self.env.info[0] = tag('', 2.)
        self.link._handle_status_update(self.env.status(), 2.)
        await self.drain()
        await self.link._resolve_spool(0, spool_id=42)
        await self.drain()
        self.assertEqual(self.env.spool_id, 42)
        self.assertEqual(self.env.applied[-1][1]['CARD_UID'], [])

    async def test_delayed_query_does_not_revive_finished_reader_state(self):
        self.env.query_gate = asyncio.Event()
        self.env.reading[0] = 1
        self.env.ids[0] = 0
        self.link._handle_status_update(self.env.status(), 2.)
        await self.drain()
        self.env.reading[0] = 0
        self.link._handle_status_update(self.env.status(), 3.)
        self.env.query_gate.set()
        await self.drain()
        self.assertIsNone(self.env.spool_id)
        self.assertFalse(self.link._channels[0].reading)


class EmptyReadTests(unittest.TestCase):
    def setUp(self):
        patch = (OVERLAY.parent / '13-patch-rfid/patches/05-add-filament-detect-set-endpoint.patch').read_text()
        added = '\n'.join(line[1:] for line in patch.splitlines()
                          if line.startswith('+') and not line.startswith('+++'))
        source = textwrap.dedent(added[added.index('    def _handle_filament_detect_set'):])
        namespace = {'filament_protocol': types.SimpleNamespace(FILAMENT_INFO_STRUCT={
            'ALPHA': 255, 'RGB_1': 0, 'OFFICIAL': False}),
            'FILAMENT_DT_STATE_IDLE': 0, 'logging': __import__('logging')}
        exec(compile(source, str(OVERLAY), 'exec'), namespace)
        self.calls = []
        self.detector = types.SimpleNamespace(
            _channel_nums=4, _state=[0] * 4, _filament_info=[{} for _ in range(4)],
            reactor=types.SimpleNamespace(monotonic=lambda: 2.),
            printer=types.SimpleNamespace(lookup_object=lambda *args:
                types.SimpleNamespace(print_task_config={'filament_official': [True] * 4})),
            _filament_info_update=lambda ch, info, clear: self.calls.append((info, clear)))
        self.handler = namespace['_handle_filament_detect_set']

    def run_read(self, info):
        request = WebRequest({'channel': 0, 'info': info})
        request.get_dict = request.get
        request.set_error = lambda error: self.fail(str(error))
        self.handler(self.detector, request)
        self.assertEqual(request.reply, {'state': 'success'})
        return self.calls[-1]

    def test_empty_response_is_non_destructive(self):
        self.assertFalse(self.run_read({})[1])

    def test_uid_only_response_is_non_destructive(self):
        info, clear = self.run_read({'CARD_UID': [1, 2, 3, 4], 'CARD_TYPE': 'NTAG'})
        self.assertFalse(clear)
        self.assertEqual(info['CARD_UID'], [1, 2, 3, 4])

    def test_full_metadata_response_updates_filament(self):
        info, clear = self.run_read({'CARD_UID': [1, 2, 3, 4], 'VENDOR': 'Bambu', 'MAIN_TYPE': 'PLA'})
        self.assertTrue(clear)
        self.assertTrue(info['OFFICIAL'])


if __name__ == '__main__':
    unittest.main()
