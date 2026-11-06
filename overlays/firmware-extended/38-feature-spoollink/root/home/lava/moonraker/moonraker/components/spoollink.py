# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @paxx12

# SpoolLink — bridge between Spoolman and the Snapmaker AFC/RFID stack.
#
# Runs inside Moonraker as a component. It registers the
# `spoollink_resolve_spool` and `spoollink_unlink_card` remote methods for
# the Klipper `[spoollink]` router, resolves scanned cards (or explicit spool IDs) against the
# Spoolman REST API, binds card UIDs to spools, keeps Moonraker's active
# spool in sync with the toolhead, and pushes the resolved filament info
# back into Klipper via the `spoollink/set` endpoint.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from .http_client import HttpClient
    from .klippy_apis import KlippyAPI as APIComp

RESOLVE_METHOD = "spoollink_resolve_spool"
UNLINK_METHOD = "spoollink_unlink_card"
SET_ENDPOINT = "spoollink/set"
# Pending changes that Klipper counters can rebuild are saved after this delay.
SAVE_DELAY = 10.
# A lookup that fails because Spoolman is unreachable returns this value.
RETRY = object()


class _OutdatedRequest(asyncio.CancelledError):
    pass


@dataclass
class ChannelSession:
    snapshot: Optional[dict] = None
    requested_revision: int = -1
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True)
class ResolutionRequest:
    connection: int
    revision: int
    read_time: float
    uid: str
    spool_id: Optional[int]


@dataclass(frozen=True)
class UsageTarget:
    uid: str
    spool_id: Optional[int] = None
    # Set for usage that waits for the next spool selection of this channel.
    channel: Optional[int] = None


def _unquote(value: str) -> str:
    s = value.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s


def _parse_card_uids(spool: dict) -> List[str]:
    raw = _unquote((spool.get("extra") or {}).get("card_uids") or "")
    return [u.strip().upper() for u in raw.split(",") if u.strip()]


def _parse_variant(vendor: str, filament: dict) -> str:
    variant = _unquote((filament.get("extra") or {}).get("variant") or "")
    if variant:
        return variant
    return "Basic" if vendor.lower() == "snapmaker" else ""


class SpoolLink:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        url = config.get("server").strip().rstrip("/")
        if "://" not in url:
            url = "http://" + url
        self._spoolman_url = url
        self._cache_dir: Optional[str] = config.get("cache_dir", None)
        self._force_generic_vendor = config.getboolean(
            "force_generic_vendor", False)
        self.http_client: HttpClient = self.server.lookup_component("http_client")
        self.klippy_apis: APIComp = self.server.lookup_component("klippy_apis")

        self._channels: Dict[int, ChannelSession] = {}
        self._card_locks: Dict[str, asyncio.Lock] = {}
        self._blocked_cards: set = set()
        self._background: set = set()
        self._pending_usage: Dict[UsageTarget, float] = {}
        self._usage_tasks: Dict[UsageTarget, asyncio.Task] = {}
        # Each waiting lookup registers the tag or spool number it waits for.
        self._wake_events: Dict[asyncio.Event, tuple] = {}
        # Spool numbers that Spoolman did not have when a channel selected them.
        self._missing_numbers: Dict[int, set] = {}
        # Set while a feed update is applied, so that its moves are saved once at the end.
        self._feed_moves: Optional[bool] = None
        self._unlink_tasks: Dict[str, asyncio.Task] = {}
        self._deleted_routes: Dict[int, Any] = {}
        self._save_handle: Optional[asyncio.TimerHandle] = None
        self._router = True
        self._ptc_spool_ids: List[int] = []
        self._waiting_seen: Dict[int, bool] = {}
        self._foreign: set = set()
        self._foreign_busy: set = set()
        self._last_server: Optional[str] = None
        self._connection = 0
        self._toolhead_extruder: str = "extruder"
        self._active_spool_id: Optional[int] = -1
        database = self.server.lookup_component('database')
        self._state_db = database.register_local_namespace('spoollink')
        self._server_key = 'state_' + hashlib.sha256(url.encode()).hexdigest()
        self._state_key = 'accounting'
        self._saved_servers: dict = {}
        self._restore_task: Optional[asyncio.Task] = None
        self._state_writes: set = set()
        self._restored = False
        self._closing = False

        self.server.register_remote_method(RESOLVE_METHOD, self._resolve_spool)
        self.server.register_remote_method(UNLINK_METHOD, self._unlink_card)
        self.server.register_event_handler(
            "server:klippy_ready", self._handle_klippy_ready)
        self.server.register_event_handler(
            "server:klippy_disconnect", self._handle_klippy_disconnect)
        self.server.register_event_handler(
            "spoolman:active_spool_set", self._handle_active_spool_set)
        self.server.register_event_handler(
            "spoolman:spoolman_status_changed", self._handle_spoolman_status)

    async def component_init(self) -> None:
        await self.restore_state()
        logging.info(
            "spoollink starting (spoolman: %s, cache: %s, "
            "force generic vendor: %s)",
            self._spoolman_url, self._cache_dir or "disabled",
            self._force_generic_vendor)
        await self._ensure_fields()

    # -- State shared with the Spoolman reporting queue ---------------------

    async def restore_state(self) -> None:
        if self._restore_task is None:
            self._restore_task = asyncio.create_task(self._restore_state())
        await asyncio.shield(self._restore_task)

    async def _restore_state(self) -> None:
        saved = await self._state_db.get(self._state_key)
        spoolman = self.server.lookup_component('spoolman')
        try:
            if saved is None:
                state = await self._state_db.get(self._server_key)
                servers = {} if state is None else {self._server_key: state}
                tracking = {'session': None, 'heads': {}}
                last_server = None
            else:
                if not isinstance(saved, dict) or saved['version'] != 2:
                    raise ValueError('unsupported accounting version')
                servers = saved['servers']
                last_server = saved.get('last_server')
                if last_server is not None and type(last_server) is not str:
                    raise ValueError('invalid last server')
                if not isinstance(servers, dict):
                    raise ValueError('invalid servers')
                tracking = saved['extrusion']
                if tracking['session'] is not None and type(tracking['session']) is not str:
                    raise ValueError('invalid printer session')
                if not isinstance(tracking['heads'], dict):
                    raise ValueError('invalid extrusion state')
                for name, row in tracking['heads'].items():
                    if name not in ('extruder', 'extruder1', 'extruder2', 'extruder3'):
                        raise ValueError('invalid extruder')
                    total, retracted = row['total'], row['retracted']
                    if type(total) not in (int, float) or not math.isfinite(total) or total < 0:
                        raise ValueError('invalid extrusion total')
                    if type(retracted) not in (int, float) or not math.isfinite(retracted) or retracted < 0:
                        raise ValueError('invalid retraction')
                    usage = row['usage']
                    if not isinstance(usage, dict):
                        raise ValueError('invalid measured usage')
                    for key, length in usage.items():
                        parts = json.loads(key)
                        if not isinstance(parts, list) or len(parts) not in (3, 4):
                            raise ValueError('invalid measured target')
                        uid, sid, requested = parts[:3]
                        if len(parts) == 4 and parts[3] is not True:
                            raise ValueError('invalid measured target')
                        if type(uid) is not str or type(sid) is not int or sid < 0:
                            raise ValueError('invalid measured target')
                        if requested is not None and not self._valid_spool_id(requested):
                            raise ValueError('invalid measured selection')
                        if not self._valid_length(length):
                            raise ValueError('invalid measured length')
            if self._server_key not in servers:
                legacy = await self._state_db.get(self._server_key)
                if legacy is not None:
                    servers = dict(servers, **{self._server_key: legacy})
            parsed = {}
            for server_key, state in servers.items():
                if type(server_key) is not str or not server_key.startswith('state_'):
                    raise ValueError('invalid server key')
                if not isinstance(state, dict) or state['version'] != 1:
                    raise ValueError('unsupported state version')
                blocked = state['blocked']
                if not isinstance(blocked, list) or any(type(uid) is not str for uid in blocked):
                    raise ValueError('invalid blocked tags')
                pending = {}
                for row in state['pending']:
                    uid, sid, length = row['uid'], row['spool_id'], row['length']
                    channel = row.get('channel')
                    if type(uid) is not str or (sid is not None and not self._valid_spool_id(sid)):
                        raise ValueError('invalid usage target')
                    if channel is not None and (type(channel) is not int or not 0 <= channel < 4):
                        raise ValueError('invalid usage channel')
                    if not uid and sid is None and channel is None:
                        raise ValueError('missing usage target')
                    if not self._valid_length(length):
                        raise ValueError('invalid pending length')
                    target = UsageTarget(uid, sid, channel)
                    pending[target] = pending.get(target, 0.) + length
                reports = {}
                for name in ('reports', 'inflight'):
                    for key, length in state[name].items():
                        sid = int(key)
                        if str(sid) != key or not self._valid_spool_id(sid) or not self._valid_length(length):
                            raise ValueError('invalid report')
                        reports[sid] = reports.get(sid, 0.) + length
                operations = {}
                for key, operation in state.get('operations', {}).items():
                    sid = int(key)
                    if str(sid) != key or not self._valid_spool_id(sid):
                        raise ValueError('invalid operation spool')
                    # Records saved by older versions may carry an unused 'id'.
                    length = operation['length']
                    if not self._valid_length(length) or length > reports.get(sid, 0.) + 0.0000001:
                        raise ValueError('invalid usage operation length')
                    before = operation.get('before')
                    if before is not None and (type(before) not in (int, float)
                                               or not math.isfinite(before) or before < 0):
                        raise ValueError('invalid usage operation start')
                    operations[sid] = dict(operation)
                parsed[server_key] = (set(blocked), pending, reports, operations)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise RuntimeError('SpoolLink could not restore saved usage; the database record was kept') from error
        self._saved_servers = servers
        # Links saved for another Spoolman server must not charge this one.
        if last_server is not None and last_server != self._server_key:
            self._last_server = last_server
            self._foreign = set(range(4))
        else:
            self._last_server = self._server_key
        spoolman._extrusion_session = tracking['session']
        spoolman._extrusion_heads = {name: dict(row, usage=dict(row['usage'])) for name, row in tracking['heads'].items()}
        if self._server_key in parsed:
            blocked, pending, reports, operations = parsed[self._server_key]
            self._blocked_cards = set(blocked)
            self._pending_usage = pending
            spoolman.pending_reports = reports
            spoolman._usage_operations = operations
        self._restored = True
        await self.save_state()

    @staticmethod
    def _valid_spool_id(value) -> bool:
        return type(value) is int and value > 0

    @staticmethod
    def _valid_length(value) -> bool:
        return type(value) in (int, float) and math.isfinite(value) and value > 0

    def save_state(self) -> asyncio.Future:
        if not self._restored:
            raise RuntimeError('SpoolLink state has not been restored')
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        spoolman = self.server.lookup_component('spoolman')
        server = {
            'version': 1,
            'blocked': sorted(self._blocked_cards),
            'pending': [{'uid': target.uid, 'spool_id': target.spool_id,
                         'channel': target.channel, 'length': length}
                        for target, length in self._pending_usage.items()],
            'reports': {str(sid): length for sid, length in spoolman.pending_reports.items()},
            'inflight': {str(sid): length for sid, length in spoolman._inflight_reports.items()},
            'operations': {str(sid): dict(operation) for sid, operation in spoolman._usage_operations.items()},
        }
        self._saved_servers = dict(self._saved_servers, **{self._server_key: server})
        # Keep the queues and their measured feed positions in one SQLite record.
        state = {
            'version': 2, 'servers': self._saved_servers,
            'last_server': self._last_server,
            'extrusion': {'session': spoolman._extrusion_session,
                          'heads': {name: dict(row, usage=dict(row['usage'])) for name, row in spoolman._extrusion_heads.items()}},
        }
        # Queue the read beside the write. This also detects a rejected SQLite write.
        written = self._state_db.insert(self._state_key, state)
        readback = self._state_db.get(self._state_key)

        async def verify():
            await asyncio.shield(written)
            if await asyncio.shield(readback) != state:
                raise RuntimeError('SpoolLink could not save filament usage to the database')

        task = asyncio.create_task(verify())
        self._state_writes.add(task)
        task.add_done_callback(self._state_writes.discard)
        task.add_done_callback(self._task_done)
        return task

    def request_save(self) -> None:
        """Save soon. For changes that the running totals of Klipper can rebuild."""
        if (self._save_handle is not None or not self._restored
                or self._closing):
            return
        self._save_handle = asyncio.get_running_loop().call_later(
            SAVE_DELAY, self._deferred_save)

    def _deferred_save(self) -> None:
        self._save_handle = None
        if self._restored and not self._closing:
            self.save_state()

    async def flush_state(self) -> None:
        if not self._restored:
            return
        await self.save_state()
        if self._state_writes:
            await asyncio.gather(*self._state_writes)

    async def on_exit(self) -> None:
        self._closing = True
        tasks = list(self._background)
        self._handle_klippy_disconnect()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.flush_state()

    # -- Klippy lifecycle ---------------------------------------------------

    async def _handle_klippy_ready(self) -> None:
        await self.restore_state()
        logging.info("[spoollink] Klippy ready, subscribing to objects")
        known = await self.klippy_apis.get_object_list([])
        # Klipper may run without the router after a partial update. Then the
        # links are read from the print task configuration.
        self._router = 'spoollink' in known
        if self._router:
            objects = {"spoollink": ["channels"], "toolhead": ["extruder"]}
        else:
            logging.warning("[spoollink] Klipper has no [spoollink] section")
            objects = {"print_task_config": ["filament_spool_id"],
                       "toolhead": ["extruder"]}
        status = await self.klippy_apis.subscribe_objects(
            objects, self._handle_status_update, {})
        self._handle_status_update(status, 0.)
        for target in list(self._pending_usage):
            self._start_pending_usage(target)
        for uid in list(self._blocked_cards):
            self._start_unlink(uid, None)

    def _handle_klippy_disconnect(self) -> None:
        logging.info("[spoollink] Klippy disconnected")
        spoolman = self.server.lookup_component('spoolman', None)
        if spoolman is not None:
            spoolman._feed_ready = False
        self._connection += 1
        for task in list(self._background):
            task.cancel()
        self._usage_tasks.clear()
        self._unlink_tasks.clear()
        self._channels.clear()
        self._waiting_seen.clear()
        self._foreign_busy.clear()
        self._active_spool_id = -1

    # -- Remote method / subscription callbacks -----------------------------

    def _handle_active_spool_set(self, payload: Dict[str, Any]) -> None:
        spool_id = payload.get("spool_id")
        if spool_id != self._active_spool_id:
            self._active_spool_id = spool_id
            logging.info("[spoollink] active spool received: spool_id=%s", spool_id)
            self._fire(self._sync_active_spool())

    def _session(self, channel: int) -> ChannelSession:
        if channel not in self._channels:
            self._channels[channel] = ChannelSession()
        return self._channels[channel]

    def _handle_status_update(self, status: Dict[str, Any], eventtime: float) -> None:
        th = status.get("toolhead")
        if th is not None:
            extruder = th.get("extruder")
            if extruder is not None and extruder != self._toolhead_extruder:
                logging.info("[spoollink] toolhead extruder changed: %s → %s",
                             self._toolhead_extruder, extruder)
                self._toolhead_extruder = extruder

        ptc = status.get("print_task_config")
        if ptc is not None and "filament_spool_id" in ptc:
            self._ptc_spool_ids = list(ptc.get("filament_spool_id") or [])

        # Read the UID, revision and spool ID together so they stay in sync.
        for channel, snapshot in enumerate(status.get("spoollink", {}).get("channels", [])):
            session = self._session(channel)
            previous = session.snapshot
            revision = snapshot['revision']
            if previous is not None and previous['revision'] > revision:
                continue
            session.snapshot = dict(snapshot)
            if channel in self._foreign:
                if snapshot['spool_id']:
                    if channel not in self._foreign_busy:
                        self._foreign_busy.add(channel)
                        self._fire(self._drop_foreign_link(channel, dict(snapshot)))
                else:
                    self._foreign.discard(channel)
                    self._finish_foreign()
            spool_id = self._channel_spool_id(channel)
            uid = snapshot.get('usage_uid', snapshot['uid'])
            requested = snapshot.get('requested_spool_id')
            if snapshot.get('waiting'):
                self._waiting_seen[channel] = True
            elif self._waiting_seen.pop(channel, False) and not spool_id and not requested:
                # The channel was cleared or its spool was taken out.
                self._discard_channel_wait(channel)
            if requested and requested in self._missing_numbers.get(channel, ()):
                pass
            elif requested and requested != spool_id:
                self._retarget_pending_usage(uid, requested, channel)
            else:
                self._settle_pending_usage(uid, spool_id)
            if (snapshot['lookup'] and snapshot['uid']
                    and session.requested_revision != revision):
                session.requested_revision = revision
                self._fire(self._resolve_spool(
                    channel, spool_id=requested, card_uid=snapshot['uid'], revision=revision,
                    read_time=snapshot['read_time']))
        self._fire(self._sync_active_spool())

    def _channel_spool_id(self, channel: int) -> int:
        """The spool of a channel that this Spoolman server can account for."""
        if channel in self._foreign:
            return 0
        if not self._router:
            ids = self._ptc_spool_ids
            return (ids[channel] if channel < len(ids) else 0) or 0
        session = self._channels.get(channel)
        snapshot = session.snapshot if session is not None else None
        return (snapshot or {}).get('spool_id') or 0

    def _finish_foreign(self) -> None:
        if not self._foreign and self._last_server != self._server_key:
            self._last_server = self._server_key
            self.save_state()

    async def _drop_foreign_link(self, channel: int, snapshot: dict) -> None:
        """Remove a link whose spool number belongs to another Spoolman server."""
        try:
            context = ResolutionRequest(self._connection, snapshot['revision'],
                                        snapshot['read_time'], snapshot['uid'], None)
            await self._spoollink_set(
                channel,
                f"SpoolLink: E{channel + 1} spool #{snapshot['spool_id']} was linked "
                "on another Spoolman server, the link was removed",
                status="error", context=context, invalidate=True)
        finally:
            self._foreign_busy.discard(channel)

    # -- Usage waiting for a spool -----------------------------------------

    def record_feed(self, heads: dict, changes: list) -> None:
        spoolman = self.server.lookup_component('spoolman')
        self._feed_moves = False
        try:
            spoolman._extrusion_heads.update(heads)
            for name, target, length in changes:
                self.record_target_extrusion(
                    self._extruder_to_channel(name), target, length)
        finally:
            moved = self._feed_moves
            self._feed_moves = None
        # Klipper totals rebuild new feed, but they cannot rebuild a move
        # between targets. The measured positions are saved together with it.
        if moved:
            self.save_state()
        else:
            self.request_save()

    def _save_move(self) -> None:
        """Save a move of waiting usage. Klipper totals cannot rebuild it."""
        if self._feed_moves is not None:
            self._feed_moves = True
        elif self._restored and not self._closing:
            self.save_state()

    def record_target_extrusion(self, channel: int, target: dict, used_length: float) -> None:
        spoolman = self.server.lookup_component('spoolman', None)
        if spoolman is None:
            return
        uid = target.get('uid', '')
        spool_id = target['spool_id']
        if channel in self._foreign or not self._valid_spool_id(spool_id):
            spool_id = 0
        requested = target.get('requested_spool_id')
        if target.get('waiting'):
            key = UsageTarget('', None, channel)
        elif requested and requested in self._missing_numbers.get(channel, ()):
            # Klipper has not yet applied the answer that this number does not exist.
            key = UsageTarget('', None, channel)
        elif requested and requested != spool_id:
            self._retarget_pending_usage(uid, requested, channel)
            key = UsageTarget(uid, requested)
        elif spool_id:
            self._settle_pending_usage(uid, spool_id)
            spoolman._add_extrusion(spool_id, used_length)
            return
        elif uid:
            key = UsageTarget(uid)
        else:
            return
        self._pending_usage[key] = self._pending_usage.get(key, 0.) + used_length
        self.request_save()
        self._start_pending_usage(key)

    def _settle_pending_usage(self, uid: str, spool_id: int) -> bool:
        if isinstance(spool_id, bool) or not isinstance(spool_id, int) or spool_id <= 0:
            return False
        targets = (UsageTarget(uid), UsageTarget(uid, spool_id))
        used_length = sum(self._pending_usage.get(target, 0.) for target in targets)
        if not used_length:
            return False
        spoolman = self.server.lookup_component('spoolman', None)
        if spoolman is None:
            return False
        for target in targets:
            self._pending_usage.pop(target, None)
        # Both queues are saved together after the transfer is complete.
        spoolman._add_extrusion(spool_id, used_length)
        logging.info('[spoollink] recorded %.3f mm of pending usage on spool %s (tag %s)',
                     used_length, spool_id, uid or 'none')
        return True

    def _retarget_pending_usage(self, uid: str, spool_id: int, channel: Optional[int] = None) -> None:
        """Move usage that waits for this tag or channel to a selected spool."""
        used_length = self._pending_usage.pop(UsageTarget(uid), 0.)
        if channel is not None:
            used_length += self._pending_usage.pop(UsageTarget('', None, channel), 0.)
        if used_length:
            target = UsageTarget(uid, spool_id)
            self._pending_usage[target] = self._pending_usage.get(target, 0.) + used_length
            self._save_move()
            self._start_pending_usage(target)

    def _discard_channel_wait(self, channel: int) -> None:
        dropped = self._pending_usage.pop(UsageTarget('', None, channel), 0.)
        if dropped:
            logging.info('[spoollink] ch%d: dropped %.3f mm of usage without a spool',
                         channel, dropped)
            self._save_move()

    def _requeue_failed_selection(self, channel: int, uid: str, spool_id: int) -> None:
        """A selected spool does not exist. Its usage waits for the next selection."""
        self._missing_numbers.setdefault(channel, set()).add(spool_id)
        used_length = self._pending_usage.pop(UsageTarget(uid, spool_id), 0.)
        if used_length:
            target = UsageTarget('', None, channel)
            self._pending_usage[target] = self._pending_usage.get(target, 0.) + used_length
            self.save_state()

    def _start_pending_usage(self, target: UsageTarget) -> None:
        if self._closing or target.channel is not None:
            return
        previous = self._usage_tasks.get(target)
        if target not in self._pending_usage or (previous is not None and not previous.done()):
            return
        task = self._fire(self._resolve_pending_usage(target))
        self._usage_tasks[target] = task

        def finished(completed):
            if self._usage_tasks.get(target) is completed:
                del self._usage_tasks[target]

        task.add_done_callback(finished)

    async def _wait_for_wake(self, timeout: float, uid: str = '',
                             spool_id: Optional[int] = None) -> bool:
        """Wait for a matching Spoolman event or for the timeout. Returns True on an event."""
        event = asyncio.Event()
        self._wake_events[event] = (uid, spool_id)
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._wake_events.pop(event, None)

    def _wake_waiters(self, uids: Optional[set] = None,
                      spool_id: Optional[int] = None) -> None:
        """Wake all lookups, or only those that the changed spool can satisfy."""
        for event, (uid, number) in list(self._wake_events.items()):
            if (uids is None or (uid and uid.upper() in uids)
                    or (number is not None and number == spool_id)):
                event.set()

    async def _resolve_pending_usage(self, target: UsageTarget) -> None:
        warned = False
        delay = 5.
        label = target.uid or 'spool %s' % target.spool_id
        while target in self._pending_usage:
            failed = False
            try:
                async with self._card_locks.setdefault(target.uid, asyncio.Lock()):
                    if target not in self._pending_usage:
                        return
                    if target.spool_id is None and target.uid in self._blocked_cards:
                        # The tag is still on a spool that the user cleared.
                        spools = []
                    elif target.spool_id is not None:
                        spool = await self._spoolman_get_by_id(target.spool_id)
                        spools = [spool] if spool is not None else []
                    else:
                        spools = await self._spoolman_find_by_card(target.uid)
                    if target not in self._pending_usage:
                        return
                    if len(spools) == 1 and self._settle_pending_usage(target.uid, spools[0]['id']):
                        if target.spool_id is None:
                            self._link_tag_channels(target.uid)
                        return
            except Exception as error:
                failed = True
                if not warned:
                    logging.warning('[spoollink] could not resolve pending usage for %s: %s',
                                    label, error)
                    warned = True
            if not warned:
                logging.warning('[spoollink] pending usage for %s is waiting for a spool', label)
                warned = True
            # Spoolman events about this tag or spool wake the loop. The timer is
            # the safety net. The delay grows after a wake-up too.
            await self._wait_for_wake(
                delay, '' if target.spool_id is not None else target.uid, target.spool_id)
            delay = min(delay * 2, 30. if failed else 300.)

    def _link_tag_channels(self, uid: str) -> None:
        """Link the channels that hold this tag to the spool that was just found."""
        if uid in self._blocked_cards:
            return
        for channel, session in list(self._channels.items()):
            snapshot = session.snapshot
            if (snapshot is not None and snapshot['uid'] == uid
                    and not self._channel_spool_id(channel)
                    and not snapshot.get('waiting')
                    and not snapshot.get('requested_spool_id')):
                self._fire(self._resolve_spool(channel, card_uid=uid))

    def _handle_spoolman_status(self, status: dict) -> None:
        if status.get('spoolman_connected'):
            self._wake_waiters()

    def handle_spool_event(self, event_type: Any, payload: dict) -> None:
        """A spool changed in Spoolman. Called for each event of the websocket."""
        spool_id = payload.get('id')
        if type(spool_id) is int:
            self._wake_waiters(set(_parse_card_uids(payload)), spool_id)
        else:
            self._wake_waiters()
        if event_type == 'deleted' and type(spool_id) is int:
            spoolman = self.server.lookup_component('spoolman', None)
            lost = spoolman.take_unsent(spool_id) if spoolman is not None else 0.
            self._fire(self._handle_missing_spool(spool_id, lost))

    def spool_missing(self, spool_id: int, lost: float) -> None:
        """Spoolman answered 404 for a spool while usage was sent."""
        self._fire(self._handle_missing_spool(spool_id, lost))

    async def _handle_missing_spool(self, spool_id: int, lost: float) -> None:
        """The spool of a channel is gone. Its usage waits and the channel is unlinked."""
        now = asyncio.get_running_loop().time()
        channels = [channel for channel, session in sorted(self._channels.items())
                    if session.snapshot is not None
                    and session.snapshot['spool_id'] == spool_id
                    and channel not in self._foreign]
        if not channels:
            route = self._deleted_routes.get(spool_id)
            if route is not None and route[1] > now and lost:
                self._pending_usage[route[0]] = self._pending_usage.get(route[0], 0.) + lost
                self.save_state()
                self._start_pending_usage(route[0])
            return
        for channel in channels:
            snapshot = dict(self._channels[channel].snapshot)
            uid = snapshot.get('usage_uid') or ''
            target = UsageTarget(uid) if uid else UsageTarget('', None, channel)
            if lost:
                self._pending_usage[target] = self._pending_usage.get(target, 0.) + lost
                lost = 0.
                self.save_state()
                self._start_pending_usage(target)
            self._deleted_routes[spool_id] = (target, now + 60.)
            logging.warning('[spoollink] ch%d: spool %s no longer exists', channel, spool_id)
            context = ResolutionRequest(self._connection, snapshot['revision'],
                                        snapshot['read_time'], snapshot['uid'], None)
            await self._spoollink_set(
                channel,
                f"SpoolLink: E{channel + 1} spool #{spool_id} was removed from Spoolman",
                status="error", context=context, invalidate=True, waiting=not uid)

    # -- Active spool sync --------------------------------------------------

    @staticmethod
    def _extruder_to_channel(extruder: str) -> int:
        if extruder == "extruder":
            return 0
        try:
            return int(extruder.replace("extruder", ""))
        except ValueError:
            return 0

    async def _sync_active_spool(self) -> None:
        channel = self._extruder_to_channel(self._toolhead_extruder)
        spool_id = self._channel_spool_id(channel) or None
        if spool_id == self._active_spool_id:
            return
        logging.info("[spoollink] set active spool: channel=%d spool_id=%s → %s",
                     channel, self._active_spool_id, spool_id)
        self._active_spool_id = spool_id
        spoolman = self.server.lookup_component("spoolman", None)
        if spoolman is None:
            return
        try:
            spoolman.set_active_spool(spool_id or None)
        except Exception:
            self._active_spool_id = None
            raise

    # -- Klipper push -------------------------------------------------------

    async def _spoollink_set(self, channel: int, message: str,
                             info: Optional[dict] = None,
                             status: str = "ok", context: Optional[ResolutionRequest] = None,
                             invalidate: bool = False,
                             waiting: bool = False) -> Optional[dict]:
        params: Dict[str, Any] = {
            "channel": channel, "message": message, "status": status}
        if info is not None:
            params["info"] = info
        if context is not None:
            params.update(request_revision=context.revision,
                          request_uid=context.uid,
                          request_read_time=context.read_time)
        if invalidate:
            params['invalidate'] = True
            params['waiting'] = waiting
        try:
            return await self.klippy_apis._send_klippy_request(SET_ENDPOINT, params)
        except self.server.error as e:
            logging.error("[spoollink] ch%d: %s failed: %s", channel, SET_ENDPOINT, e)
            await self.klippy_apis.run_gcode(
                'RESPOND TYPE=error MSG="SpoolLink: Spoolman integration '
                'appears disabled on the printer — re-enable it via '
                'firmware-config and reboot"', None)
            return None

    # -- Task helpers -------------------------------------------------------

    def _fire(self, coro) -> "asyncio.Future":
        task = asyncio.ensure_future(coro)
        if self._closing:
            task.cancel()
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(self._task_done)
        return task

    @staticmethod
    def _task_done(task: "asyncio.Future") -> None:
        if not task.cancelled() and task.exception() is not None:
            logging.error("[spoollink] background task failed: %s",
                          task.exception(), exc_info=task.exception())

    async def _retry(self, fn, *args, retries=3, **kwargs):
        delay = 1.0
        for attempt in range(retries + 1):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                if attempt == retries:
                    raise
                logging.debug("[spoollink] attempt %d/%d failed: %s",
                              attempt + 1, retries, e)
                await asyncio.sleep(delay)
                delay *= 2

    # -- Local cache --------------------------------------------------------

    def _cache_path(self, card_uid: str) -> Optional[str]:
        if not self._cache_dir:
            return None
        return os.path.join(self._cache_dir, f"{card_uid.upper()}.json")

    def _load_cache(self, card_uid: str) -> Optional[dict]:
        path = self._cache_path(card_uid)
        if not path:
            return None
        try:
            with open(path) as f:
                spool = json.load(f)
            if not isinstance(spool, dict) or not isinstance(spool.get('filament'), dict):
                raise ValueError('cache entry has no spool data')
            identifier = spool.get('id')
            if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1:
                raise ValueError('cache entry has an invalid spool ID')
            logging.info("[spoollink] cache hit for card %s (spool %s)",
                         card_uid, spool.get("id"))
            return spool
        except FileNotFoundError:
            return None
        except Exception as e:
            logging.warning("[spoollink] cache read failed for %s: %s", card_uid, e)
            return None

    def _save_cache(self, card_uid: str, spool: dict) -> None:
        path = self._cache_path(card_uid)
        if not path:
            return
        try:
            os.makedirs(self._cache_dir, exist_ok=True)
            with open(path, "w") as f:
                json.dump(spool, f)
            logging.info("[spoollink] cached spool %s for card %s",
                         spool.get("id"), card_uid)
        except Exception as e:
            logging.warning("[spoollink] cache write failed for %s: %s", card_uid, e)

    def _delete_cache(self, card_uid: str) -> None:
        path = self._cache_path(card_uid)
        if not path:
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception as e:
            logging.warning("[spoollink] cache delete failed for %s: %s", card_uid, e)

    # -- Spoolman REST ------------------------------------------------------

    async def _ensure_fields(self) -> None:
        await self._ensure_field("spool", "card_uids", "Card UIDs")
        await self._ensure_field("filament", "variant", "Variant")

    async def _ensure_field(self, entity_type: str, key: str, name: str) -> None:
        base = f"{self._spoolman_url}/api/v1/field/{entity_type}"
        try:
            resp = await self.http_client.get(base, enable_cache=False)
            if resp.status_code != 200:
                logging.warning(
                    "[spoollink] could not read custom fields for %s (HTTP %s)",
                    entity_type, resp.status_code)
                return
            fields = resp.json()
            if any(f.get("key") == key for f in fields):
                logging.info("[spoollink] field %s/%s: exists", entity_type, key)
                return
            body = {
                "name": name,
                "field_type": "text",
                "order": 1,
                "default_value": json.dumps(""),
            }
            resp = await self.http_client.post(f"{base}/{key}", body=body)
            if resp.status_code in (200, 201):
                logging.info("[spoollink] field %s/%s: created", entity_type, key)
            else:
                logging.warning(
                    "[spoollink] could not create field %s/%s: HTTP %s %s",
                    entity_type, key, resp.status_code, resp.text)
        except Exception as e:
            logging.warning(
                "[spoollink] custom fields check failed (%s/%s): %s",
                entity_type, key, e)

    async def _spoolman_get_by_id(self, spool_id: int) -> Optional[dict]:
        resp = await self.http_client.get(
            f"{self._spoolman_url}/api/v1/spool/{spool_id}", enable_cache=False)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return None
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")

    async def _spoolman_find_by_card(self, card_uid: str) -> List[dict]:
        resp = await self.http_client.get(
            f"{self._spoolman_url}/api/v1/spool?limit=1000", enable_cache=False)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
        spools = resp.json()
        uid_upper = card_uid.upper()
        return [s for s in spools if uid_upper in _parse_card_uids(s)]

    async def _spoolman_patch_card_uids(self, spool: dict, uids: List[str]) -> dict:
        encoded = json.dumps(",".join(uids))
        resp = await self.http_client.request(
            "PATCH", f"{self._spoolman_url}/api/v1/spool/{spool['id']}",
            body={"extra": {"card_uids": encoded}})
        if resp.status_code == 200:
            return resp.json()
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")

    async def _spoolman_add_card_uid(self, spool: dict, card_uid: str) -> dict:
        uid_upper = card_uid.upper()
        existing = _parse_card_uids(spool)
        if uid_upper in existing:
            return spool
        return await self._spoolman_patch_card_uids(spool, existing + [uid_upper])

    async def _spoolman_remove_card_uid(self, spool: dict, card_uid: str) -> dict:
        uid_upper = card_uid.upper()
        existing = _parse_card_uids(spool)
        if uid_upper not in existing:
            return spool
        return await self._spoolman_patch_card_uids(
            spool, [u for u in existing if u != uid_upper])

    # -- Resolution ---------------------------------------------------------

    def _current(self, channel: int, context: ResolutionRequest) -> bool:
        session = self._channels.get(channel)
        latest = session.snapshot if session is not None else None
        return (context.connection == self._connection
                and (latest is None or latest['revision'] <= context.revision)
                and (bool(context.spool_id) or context.uid not in self._blocked_cards))

    async def _checked_call(self, channel, context, fn, *args, retries=3):
        async def invoke():
            if not self._current(channel, context):
                raise _OutdatedRequest()
            result = await fn(*args)
            if not self._current(channel, context):
                raise _OutdatedRequest()
            return result
        return await self._retry(invoke, retries=retries)

    async def _unlink_card(self, channel: int, card_uid: Any = None,
                           revision: Optional[int] = None, read_time: float = 0.) -> None:
        """Unbind a card from every spool holding it, so clearing a channel
        is not undone by the next scan of the same card."""
        card_uid = str(card_uid or "").upper()
        if channel is None:
            return
        session = self._session(channel)
        snapshot = session.snapshot or {}
        if revision is None:
            revision = snapshot.get('revision', 0) + 1
        if snapshot.get('revision', -1) <= revision:
            session.snapshot = dict(snapshot, revision=revision, uid=card_uid,
                                    read_time=read_time, spool_id=0, lookup=False)
        # Usage that waits for a selection belongs to the cleared spool.
        self._discard_channel_wait(channel)
        has_tag = bool(card_uid.strip('0'))
        if has_tag:
            self._blocked_cards.add(card_uid)
            self.save_state()
            self._delete_cache(card_uid)
        await self._sync_active_spool()
        if has_tag:
            self._start_unlink(card_uid, (channel, revision))
        else:
            await self._notify_unlinked(channel, card_uid, revision)

    async def _notify_unlinked(self, channel: int, card_uid: str, revision: int) -> None:
        try:
            await self.klippy_apis._send_klippy_request(SET_ENDPOINT, {
                'channel': channel, 'status': 'unlinked',
                'card_uid': card_uid, 'revision': revision})
        except Exception as e:
            logging.error("[spoollink] ch%d: unlink card %s failed: %s",
                          channel, card_uid, e)

    def _start_unlink(self, card_uid: str, notify: Optional[tuple]) -> None:
        if self._closing or not card_uid.strip('0'):
            return
        running = self._unlink_tasks.get(card_uid)
        if running is not None and not running.done():
            return
        task = self._fire(self._unlink_loop(card_uid, notify))
        self._unlink_tasks[card_uid] = task

        def finished(completed):
            if self._unlink_tasks.get(card_uid) is completed:
                del self._unlink_tasks[card_uid]

        task.add_done_callback(finished)

    async def _unlink_loop(self, card_uid: str, notify: Optional[tuple]) -> None:
        """Remove the tag from its spools. A failure is tried again until it works."""
        connection = self._connection
        delay = 5.
        retries = 3
        while card_uid in self._blocked_cards and connection == self._connection:
            try:
                async with self._card_locks.setdefault(card_uid, asyncio.Lock()):
                    spools = await self._retry(
                        self._spoolman_find_by_card, card_uid, retries=retries)
                    failed = False
                    for spool in spools:
                        try:
                            await self._retry(self._spoolman_remove_card_uid, spool,
                                              card_uid, retries=retries)
                        except Exception:
                            failed = True
                            logging.exception('[spoollink] failed to unlink tag %s', card_uid)
                    if not failed:
                        self._blocked_cards.discard(card_uid)
                        await self.save_state()
                        if notify is not None:
                            await self._notify_unlinked(notify[0], card_uid, notify[1])
                        return
            except Exception as e:
                logging.error("[spoollink] unlink card %s failed: %s", card_uid, e)
            retries = 0
            await self._wait_for_wake(delay, card_uid)
            delay = min(delay * 2, 60.)

    async def _resolve_spool(self, channel: int, spool_id: Any = None,
                             card_uid: Any = None, revision: Optional[int] = None,
                             read_time: float = 0.) -> None:
        connection = self._connection
        if channel is None:
            return
        session = self._session(channel)
        if revision is None:
            status = await self.klippy_apis.query_objects({'spoollink': ['channels']})
            if connection != self._connection:
                return
            self._handle_status_update(status, 0.)
            # Keep the query's revision so we can reject a late reply.
            snapshot = status['spoollink']['channels'][channel]
            revision, read_time = snapshot['revision'], snapshot['read_time']
            card_uid = snapshot['uid'] if card_uid is None else card_uid
        context = ResolutionRequest(connection, revision, read_time,
                                    str(card_uid or '').upper(), spool_id or None)
        if context.spool_id:
            # A selection takes the usage that waited for one.
            self._retarget_pending_usage(context.uid, context.spool_id, channel)
        retries = 3
        delay = 5.
        while True:
            try:
                async with session.lock:
                    async with self._card_locks.setdefault(context.uid, asyncio.Lock()):
                        if not self._current(channel, context):
                            return
                        outcome = await self._resolve_request(channel, context, retries)
            except _OutdatedRequest:
                return
            if outcome is not RETRY:
                return
            # Spoolman could not be reached. Try again until it answers or the
            # channel changes. The lock is free while this task waits.
            retries = 0
            await self._wait_for_wake(delay, context.uid, context.spool_id)
            delay = min(delay * 2, 60.)
            if not self._current(channel, context):
                return

    async def _resolve_request(self, channel, context, retries=3):
        spool_id = context.spool_id
        card_uid = context.uid or None
        if channel is None:
            logging.error("[spoollink] resolve_spool: missing channel")
            return
        logging.debug("[spoollink] ch%d: resolve spool_id=%s card_uid=%s",
                      channel, spool_id, card_uid)
        spool_by_id = None
        spools_by_card: List[dict] = []
        spoolman_ok = True

        if spool_id is not None:
            try:
                spool_by_id = await self._checked_call(
                    channel, context, self._spoolman_get_by_id, spool_id, retries=retries)
            except Exception as e:
                logging.error("[spoollink] ch%d: fetch spool %s failed: %s",
                              channel, spool_id, e)
                spoolman_ok = False
            else:
                if spool_by_id is not None:
                    self._missing_numbers.get(channel, set()).discard(spool_id)
                if spool_by_id is None:
                    # The number is not in Spoolman. The tag must not stand in for it.
                    self._requeue_failed_selection(channel, context.uid, spool_id)
                    await self._spoollink_set(
                        channel,
                        f"SpoolLink: E{channel + 1} spool #{spool_id} was not found in Spoolman",
                        status="error", context=context, invalidate=True, waiting=True)
                    return

        if card_uid is not None:
            try:
                spools_by_card = await self._checked_call(channel, context,
                    self._spoolman_find_by_card, card_uid, retries=retries)
            except Exception as e:
                logging.error("[spoollink] ch%d: fetch by card failed: %s",
                              channel, e)
                spoolman_ok = False

        if len(spools_by_card) > 1:
            self._delete_cache(card_uid)
            ids = ", ".join(f"#{s['id']}" for s in spools_by_card)
            logging.warning("[spoollink] ch%d: card %s assigned to multiple spools: %s",
                            channel, card_uid, ids)
            await self._spoollink_set(
                channel,
                f"SpoolLink: E{channel + 1} card {card_uid} "
                f"assigned to multiple spools: {ids}",
                status="error", context=context, invalidate=True)
            return
        spool_by_card = spools_by_card[0] if spools_by_card else None
        spool = spool_by_id or spool_by_card
        cached = False
        if spool is None:
            if card_uid is not None and not spoolman_ok:
                spool = self._load_cache(card_uid)
                if spool is not None:
                    logging.warning("[spoollink] ch%d: using cached data for card %s",
                                    channel, card_uid)
            cached = spool is not None and not spoolman_ok
            if spool is None:
                if not spoolman_ok:
                    return RETRY
                if card_uid is not None:
                    self._delete_cache(card_uid)
                    await self._spoollink_set(
                        channel,
                        f"SpoolLink: E{channel + 1} no spool found for card {card_uid}",
                        status="error", context=context, invalidate=True)
                return

        if card_uid is not None and spool_by_id is not None:
            if card_uid.upper() not in _parse_card_uids(spool_by_id):
                try:
                    spool = await self._checked_call(channel, context,
                        self._spoolman_add_card_uid, spool_by_id, card_uid)
                    logging.info("[spoollink] ch%d: bound spool %s to card %s",
                                 channel, spool_by_id["id"], card_uid)
                except Exception as e:
                    logging.error("[spoollink] ch%d: bind spool %s failed: %s",
                                  channel, spool_by_id["id"], e)

            for stale in spools_by_card:
                if stale["id"] == spool_by_id["id"]:
                    continue
                try:
                    await self._checked_call(channel, context,
                        self._spoolman_remove_card_uid, stale, card_uid)
                    logging.info("[spoollink] ch%d: unbound card %s from spool %s",
                                 channel, card_uid, stale["id"])
                except Exception as e:
                    logging.error(
                        "[spoollink] ch%d: unbind card %s from spool %s failed: %s",
                        channel, card_uid, stale["id"], e)

        accepted = await self._apply_spool(channel, spool, card_uid or "",
                                           cached=cached, context=context)
        if accepted and self._current(channel, context) and card_uid and spoolman_ok:
            self._save_cache(card_uid, spool)
            if context.spool_id:
                self._blocked_cards.discard(card_uid)
                await self.save_state()

    async def _apply_spool(self, channel: int, spool: dict, uid_hex: str,
                           cached: bool = False, context: Optional[ResolutionRequest] = None) -> bool:
        spool_id = spool.get("id", 0)
        filament = spool.get("filament", {})
        material = filament.get("material", "PLA")
        vendor = (filament.get("vendor") or {}).get("name", "Generic")
        variant = _parse_variant(vendor, filament)

        if (self._force_generic_vendor
                and vendor.strip().lower() != "snapmaker"):
            vendor = "Generic"
            variant = ""

        raw_multi = filament.get("multi_color_hexes") or ""
        colors = [c.strip().upper()[:6] for c in raw_multi.split(",") if c.strip()]
        color_hex = colors[0] if colors else (filament.get("color_hex") or "FFFFFF")[:6].upper()

        color_list = colors or [color_hex]
        color_nums = len(color_list)
        while len(color_list) < 5:
            color_list.append("000000")

        card_uid = [int(uid_hex[i:i+2], 16)
                    for i in range(0, len(uid_hex), 2)] if uid_hex else []
        alpha = 0xFF
        info = {
            "VENDOR": vendor,
            "MAIN_TYPE": material,
            "SUB_TYPE": variant,
            "RGB_1": int(color_list[0], 16),
            "RGB_2": int(color_list[1], 16),
            "RGB_3": int(color_list[2], 16),
            "RGB_4": int(color_list[3], 16),
            "RGB_5": int(color_list[4], 16),
            "ALPHA": alpha,
            "ARGB_COLOR": (alpha << 24) | int(color_list[0], 16),
            "COLOR_NUMS": color_nums,
            "MULTI_MODE": 0,
            "OFFICIAL": True,
            "SKU": 0,
            "SPOOL_ID": spool_id,
            "CARD_UID": card_uid,
            "CARD_TYPE": 0,
        }

        label = f"{vendor} {material}"
        if variant:
            label += f" {variant}"
        label += f" #{color_list[0]} (spool #{spool_id}, card {uid_hex or 'none'})"
        if cached:
            label += " [cached]"
        message = f"SpoolLink: E{channel + 1} loaded {label}"

        logging.info(
            "[spoollink] ch%d: applying spool %s — %s %s%s #%s (card %s)",
            channel, spool_id, vendor, material,
            f" {variant}" if variant else "",
            color_hex, uid_hex or "none")
        reply = await self._spoollink_set(channel, message, info=info, context=context)
        if reply is not None and reply.get('accepted', True):
            logging.info("[spoollink] ch%d: spool %s applied", channel, spool_id)
            return True
        return False


def load_component(config: ConfigHelper) -> SpoolLink:
    return SpoolLink(config)
