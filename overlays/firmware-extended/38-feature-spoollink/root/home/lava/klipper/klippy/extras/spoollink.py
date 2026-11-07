# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @paxx12, @sitnikovv

import logging
import math
import os
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

from . import filament_protocol


@dataclass(frozen=True)
class Assignment:
    uid: str
    spool_id: int


@dataclass
class ChannelState:
    revision: int = 0
    assignment: Optional[Assignment] = None
    lookup_uid: Optional[str] = None
    pending_unlink: Optional[Tuple[str, int]] = None
    usage_uid: str = ''
    requested_spool_id: Optional[int] = None
    # The spool of the channel is gone and usage waits for the next selection.
    # It is saved, so the wait survives a restart.
    waiting: bool = False
    # The link came from a tag. It is saved, so the next start still knows it.
    tag_linked: bool = False
    # No reader result has arrived since Klipper started.
    first_read: bool = True

    def spool_id(self, reported_id, uid=''):
        # Only a positive integer is a spool number.
        if type(reported_id) is not int or reported_id <= 0:
            return 0
        if self.assignment is None:
            # Use the saved spool ID until the first read or user action.
            return reported_id if self.revision == 0 else 0
        if (self.assignment.spool_id == reported_id
                and (not uid or self.assignment.uid == uid)):
            return reported_id
        return 0

    def observe(self, reported_id):
        """Notice a link that was removed from outside, for example by the screen."""
        if self.assignment is not None and not reported_id:
            self.assignment = None
            self.usage_uid = ''
            self.waiting = False
            self.tag_linked = False

    def read(self, uid, reported_id, clearing):
        """Apply a reader result. Returns True when the saved link must be removed."""
        previous_id = self.spool_id(reported_id)
        first = self.first_read
        self.first_read = False
        self.revision += 1
        self.lookup_uid = uid or None
        if clearing:
            self.usage_uid = ''
            self.requested_spool_id = None
            self.waiting = False
            self.reject()
            return False
        if uid:
            if uid != self.usage_uid:
                self.requested_spool_id = None
                self.waiting = False
            self.usage_uid = uid
            # A new tag must not use the previous spool assignment.
            if self.assignment != Assignment(uid, reported_id):
                self.assignment = None
            if self.pending_unlink and self.pending_unlink[0] != uid:
                self.pending_unlink = None
            return False
        if first and self.tag_linked and previous_id:
            # The spool was changed while the printer was off and the new one has no tag.
            self.assignment = None
            self.usage_uid = ''
            self.requested_spool_id = None
            self.waiting = False
            self.tag_linked = False
            return True
        if self.assignment is None and previous_id:
            self.assignment = Assignment('', previous_id)
        return False

    def select(self, uid, spool_id):
        self.revision += 1
        self.lookup_uid = None
        self.requested_spool_id = spool_id or None
        self.waiting = False
        self.pending_unlink = None if spool_id else (uid, self.revision)
        if not spool_id:
            self.assignment = None
            self.tag_linked = False
            # Usage under a tag keeps waiting. Usage without a tag is dropped.
            self.usage_uid = uid
        elif uid:
            self.usage_uid = uid

    def accept(self, uid, spool_id):
        self.assignment = Assignment(uid, spool_id)
        self.lookup_uid = None
        self.requested_spool_id = None
        self.waiting = False
        self.tag_linked = bool(uid)
        if uid:
            self.usage_uid = uid

    def reject(self):
        self.assignment = None
        self.lookup_uid = None
        self.tag_linked = False

    def fail(self, waiting):
        """The spool of the channel does not exist in Spoolman."""
        self.reject()
        self.requested_spool_id = None
        self.waiting = waiting

    def unlink_done(self, uid, revision):
        if self.pending_unlink == (uid, revision):
            self.pending_unlink = None


LINKS_FILE = 'spoollink.json'
LINKS_DEFAULT = {'tag_linked': [False] * 4, 'waiting': [False] * 4}


class SpoolLink:
    def __init__(self, config):
        self.printer = config.get_printer()
        self._session_id = uuid.uuid4().hex
        self._feed = {name: {'position': 0., 'peak': 0.} for name in
                      ('extruder', 'extruder1', 'extruder2', 'extruder3')}
        self._initial_retractions = None
        self._feed_segments = {name: [] for name in self._feed}
        self._feed_usage = {name: {} for name in self._feed}
        self.gcode = self.printer.lookup_object('gcode')
        self._channels = [ChannelState() for _ in range(4)]
        self._links_path = os.path.join(self.printer.get_snapmaker_config_dir(),
                                        LINKS_FILE)
        saved = self.printer.load_snapmaker_config_file(
            self._links_path, dict(LINKS_DEFAULT))
        links = {}
        for key in LINKS_DEFAULT:
            flags = saved.get(key)
            if not isinstance(flags, list) or len(flags) != 4:
                flags = [False] * 4
            links[key] = flags
        for index, state in enumerate(self._channels):
            state.tag_linked = links['tag_linked'][index] is True
            state.waiting = links['waiting'][index] is True
        self._saved_links = self._link_flags()
        self.gcode.register_command('SPOOLLINK_SELECT', self.cmd_SPOOLLINK_SELECT)
        self.printer.lookup_object('webhooks').register_endpoint(
            'spoollink/set', self._handle_set)
        self.printer.lookup_object('webhooks').register_endpoint(
            'spoollink/feed', self._handle_feed)
        self._set_filament_config = None
        self.printer.register_event_handler(
            'klippy:connect', self._handle_connect)

    def _handle_connect(self):
        # All modules are loaded now. Take over the command that sets the
        # filament config, so that a spool number given to it is a selection.
        handler = self.gcode.register_command('SET_PRINT_FILAMENT_CONFIG', None)
        if handler is None:
            return
        self._set_filament_config = handler
        self.gcode.register_command(
            'SET_PRINT_FILAMENT_CONFIG', self.cmd_SET_PRINT_FILAMENT_CONFIG)

    def cmd_SET_PRINT_FILAMENT_CONFIG(self, gcmd):
        self._set_filament_config(gcmd)
        spool_id = gcmd.get_int('FILAMENT_SPOOL_ID', 0)
        channel = gcmd.get_int('CONFIG_EXTRUDER')
        # SpoolLink itself only writes the number 0 with this command.
        if spool_id > 0 and 0 <= channel < len(self._channels):
            if self._ids()[channel] == spool_id:
                self._select(channel, spool_id)

    def _link_flags(self):
        return {'tag_linked': [state.tag_linked for state in self._channels],
                'waiting': [state.waiting for state in self._channels]}

    def _save_links(self):
        # The flags tell the next start whether a saved link came from a tag
        # and whether a channel still waits for a selection.
        flags = self._link_flags()
        if flags == self._saved_links:
            return
        self._saved_links = flags
        self.printer.update_snapmaker_config_file(
            self._links_path, flags, dict(LINKS_DEFAULT))

    def record_move(self, extruder, distance):
        row = self._feed[extruder]
        previous = row['peak']
        row['position'] += distance
        row['peak'] = max(row['peak'], row['position'])
        if row['peak'] <= previous:
            return
        channel = 0 if extruder == 'extruder' else int(extruder[len('extruder'):])
        info = self.printer.lookup_object('filament_detect').get_all_filament_info()[channel]
        snapshot = self._snapshot(channel, info, self._ids()[channel])
        target = (snapshot['usage_uid'], snapshot['spool_id'],
                  snapshot['requested_spool_id'], snapshot['waiting'])
        if self._initial_retractions is None:
            segments = self._feed_segments[extruder]
            if segments and segments[-1]['target'] == target:
                segments[-1]['end'] = row['peak']
            else:
                segments.append({'start': previous, 'end': row['peak'], 'target': target})
        else:
            length = row['peak'] - max(previous, self._initial_retractions[extruder])
            self._add_feed_usage(extruder, target, length)

    def _add_feed_usage(self, extruder, target, length):
        if length > 0:
            totals = self._feed_usage[extruder]
            totals[target] = totals.get(target, 0.) + length

    def _handle_feed(self, web_request):
        if web_request.get('session_id') != self._session_id:
            raise web_request.error('The printer session has changed')
        retractions = web_request.get('retractions')
        if not isinstance(retractions, dict) or set(retractions) != set(self._feed):
            raise web_request.error('Retractions must include all four heads')
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in retractions.values()):
            raise web_request.error('Retractions must be finite nonnegative lengths')
        if self._initial_retractions is not None and retractions != self._initial_retractions:
            raise web_request.error('Feed tracking is already initialized')
        if self._initial_retractions is None:
            self._initial_retractions = dict(retractions)
            for name, segments in self._feed_segments.items():
                for segment in segments:
                    length = segment['end'] - max(segment['start'], retractions[name])
                    self._add_feed_usage(name, segment['target'], length)
                segments.clear()
        web_request.send(self.get_status(0))

    @staticmethod
    def _uid(info):
        value = info.get('CARD_UID')
        if not isinstance(value, (list, tuple)) or not any(value):
            return ''
        return ''.join('%02X' % byte for byte in value)

    def _ids(self):
        return self.printer.lookup_object('print_task_config').print_task_config['filament_spool_id']

    def reader_update(self, channel, info, clearing):
        """Apply a reader result. Returns True when a spool is assigned to the channel."""
        current_id = self._ids()[channel]
        uid = self._uid(info)
        state = self._channels[channel]
        drop_link = state.read(uid, current_id, clearing)
        preserved_id = state.spool_id(current_id, uid)
        if uid and preserved_id:
            info['SPOOL_ID'] = preserved_id
        if drop_link:
            logging.info('[spoollink] ch%d: no tag after the start, removing the saved link', channel)
            self.printer.get_reactor().register_async_callback(
                lambda eventtime: self.gcode.run_script(
                    'SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d FILAMENT_SPOOL_ID=0 FORCE=1'
                    % channel))
        self._save_links()
        return state.assignment is not None

    def keeps_manual_state(self, channel, info):
        """True when a clear command must not touch a channel without a tag.

        The channel then holds a spool selected by hand, or usage that waits
        for a selection. Only removing the filament, Clear in Filament Manager
        or a spool number 0 ends that state.
        """
        if self._uid(info):
            return False
        state = self._channels[channel]
        if state.waiting:
            return True
        return not state.tag_linked and state.spool_id(self._ids()[channel]) > 0

    def _snapshot(self, channel, info, reported_id):
        state = self._channels[channel]
        state.observe(reported_id)
        self._save_links()
        uid = self._uid(info)
        return {
            'revision': state.revision, 'uid': uid,
            'read_time': info.get('CARD_EVENT_TIME', 0),
            'spool_id': state.spool_id(reported_id, uid),
            'usage_uid': state.usage_uid if state.revision else uid,
            'requested_spool_id': state.requested_spool_id,
            'waiting': state.waiting,
            'lookup': bool(uid and state.lookup_uid == uid
                           and state.pending_unlink is None),
        }

    def get_status(self, eventtime):
        info = self.printer.lookup_object('filament_detect').get_all_filament_info()
        initial = self._initial_retractions or {}
        feed = {name: {'total': max(0., row['peak'] - initial.get(name, 0.)),
                       'retracted': max(initial.get(name, 0.), row['peak']) - row['position'],
                       'usage': [{'uid': target[0], 'spool_id': target[1],
                                  'requested_spool_id': target[2], 'waiting': target[3],
                                  'length': length}
                                 for target, length in self._feed_usage[name].items()]}
                for name, row in self._feed.items()}
        return {'session_id': self._session_id,
                'feed_initialized': self._initial_retractions is not None,
                'feed': feed,
                'channels': [self._snapshot(channel, info[channel], spool_id)
                             for channel, spool_id in enumerate(self._ids())]}

    def cmd_SPOOLLINK_SELECT(self, gcmd):
        channel = gcmd.get_int('CHANNEL', minval=0, maxval=len(self._channels) - 1)
        spool_id = gcmd.get_int('SPOOL_ID', minval=0)
        self._select(channel, spool_id)

    def _select(self, channel, spool_id):
        state = self._channels[channel]
        info = self.printer.lookup_object('filament_detect').get_all_filament_info()[channel]
        state.select(self._uid(info), spool_id)
        self._save_links()
        snapshot = self.get_status(0)['channels'][channel]
        payload = {'channel': channel, 'card_uid': snapshot['uid'],
                   'revision': snapshot['revision'], 'read_time': snapshot['read_time']}
        if spool_id:
            payload['spool_id'] = spool_id
            method = 'spoollink_resolve_spool'
        else:
            self.gcode.run_script_from_command(
                'SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d FILAMENT_SPOOL_ID=0 FORCE=1' % channel)
            method = 'spoollink_unlink_card'
        self.printer.lookup_object('webhooks').call_remote_method(method, **payload)

    def _handle_set(self, web_request):
        wh = self.printer.lookup_object('webhooks')
        if not wh.has_remote_method('spoollink_resolve_spool'):
            raise web_request.error('spoollink not supported')

        channel = web_request.get_int('channel')
        if not 0 <= channel < len(self._channels):
            raise web_request.error('channel is out of range')
        state = self._channels[channel]
        message = web_request.get_str('message', '')
        status = web_request.get_str('status', 'ok')

        if status == 'unlinked':
            state.unlink_done(web_request.get('card_uid'), web_request.get('revision'))
            web_request.send({})
            return

        revision = web_request.get('request_revision', None)
        if revision is not None:
            current = self.get_status(0)['channels'][channel]
            if (revision != current['revision']
                    or web_request.get('request_uid') != current['uid']
                    or web_request.get('request_read_time') != current['read_time']):
                web_request.send({'accepted': False})
                return

        if status == 'error':
            if web_request.get('invalidate', False):
                state.fail(bool(web_request.get('waiting', False)))
                self._save_links()
                rejected = dict(self.printer.lookup_object('filament_detect').get_all_filament_info()[channel])
                rejected['SPOOL_ID'] = 0
                self.printer.lookup_object('print_task_config')._rfid_filament_info_update_cb(
                    channel, rejected, is_clear=True)
            logging.warning('[spoollink] ch%d error: %s', channel, message)
            self.gcode.respond_raw('!! %s' % message)
            web_request.send({})
            return

        info = dict(filament_protocol.FILAMENT_INFO_STRUCT)
        info.update(web_request.get('info', {}))
        if message:
            logging.info('[spoollink] ch%d: %s', channel, message)
        ptc = self.printer.lookup_object('print_task_config')
        ptc._rfid_filament_info_update_cb(channel, info, is_clear=True)
        state.accept(self._uid(info), info.get('SPOOL_ID', 0))
        self._save_links()
        self.gcode.respond_raw('// %s' % message)
        web_request.send({'accepted': True})

def load_config(config):
    return SpoolLink(config)
