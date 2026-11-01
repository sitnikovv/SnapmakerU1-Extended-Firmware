# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @paxx12

import logging

from . import filament_protocol

class SpoolLink:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self._generation = [0] * 4
        self.gcode.register_command('SPOOLLINK_CLEAR', self.cmd_SPOOLLINK_CLEAR)
        self.printer.lookup_object('webhooks').register_endpoint(
            'spoollink/set', self._handle_set)

    def get_status(self, eventtime):
        return {'generation': list(self._generation)}

    def cmd_SPOOLLINK_CLEAR(self, gcmd):
        channel = gcmd.get_int('CHANNEL', minval=0, maxval=len(self._generation) - 1)
        fd = self.printer.lookup_object('filament_detect')
        raw_uid = fd.get_all_filament_info()[channel].get('CARD_UID')
        uid = self._uid_to_hex(raw_uid)
        # Отменяем также запрос, уже отправленный Moonraker в Klipper.
        self._generation[channel] += 1
        self.gcode.run_script_from_command(
            'SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d '
            'FILAMENT_SPOOL_ID=0 FORCE=1' % channel)
        self.printer.lookup_object('webhooks').call_remote_method(
            'spoollink_unlink_card', channel=channel, card_uid=uid)

    @staticmethod
    def _uid_to_hex(raw_uid):
        if not isinstance(raw_uid, (list, tuple)) or not any(raw_uid):
            return ''
        return ''.join('%02X' % byte for byte in raw_uid)

    def _handle_set(self, web_request):
        wh = self.printer.lookup_object('webhooks')
        if not wh.has_remote_method('spoollink_resolve_spool'):
            raise web_request.error('spoollink not supported')

        channel = web_request.get_int('channel')
        message = web_request.get_str('message', '')
        status = web_request.get_str('status', 'ok')

        if status == 'error':
            logging.warning('[spoollink] ch%d error: %s', channel, message)
            self.gcode.respond_raw('!! %s' % message)
            web_request.send({})
            return

        generation = web_request.get('expected_generation', None)
        expected_uid = web_request.get('expected_uid', None)
        expected_time = web_request.get('expected_card_event_time', None)
        current = self.printer.lookup_object('filament_detect').get_all_filament_info()[channel]
        if ((generation is not None and generation != self._generation[channel])
                or (expected_uid is not None
                    and expected_uid != self._uid_to_hex(current.get('CARD_UID')))
                or (expected_time is not None
                    and expected_time != current.get('CARD_EVENT_TIME', 0))):
            web_request.send({'applied': False})
            return

        info = dict(filament_protocol.FILAMENT_INFO_STRUCT)
        info.update(web_request.get('info', {}))
        if message:
            logging.info('[spoollink] ch%d: %s', channel, message)
        ptc = self.printer.lookup_object('print_task_config')
        ptc._rfid_filament_info_update_cb(channel, info, is_clear=True)
        self.gcode.respond_raw('// %s' % message)
        web_request.send({'applied': True})

def load_config(config):
    return SpoolLink(config)
