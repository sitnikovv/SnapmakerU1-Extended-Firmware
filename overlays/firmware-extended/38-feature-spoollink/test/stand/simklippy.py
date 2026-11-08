# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @sitnikovv

"""Run the U1 Klipper host modules from a firmware tree without MCU hardware.

The real Klipper reactor, Printer, webhooks API server, G-code dispatcher,
gcode_macro, filament_detect, print_task_config and spoollink modules are
loaded from the firmware tree.  Only hardware objects are replaced:
toolhead, extruder heaters and steppers, filament sensors, the built-in RFID
reader and print_stats.  Extruder moves call the firmware's
PrinterExtruder.move function, so the firmware's own accounting hook runs.

Usage: simklippy.py <klippy-dir> <printer.cfg> <api-socket> <log-file>
"""

import json
import logging
import os
import sys
import threading
import time
import tty
import types


def _drain(fd):
    while os.read(fd, 4096):
        pass


def install_stubs():
    chelper = types.ModuleType('chelper')

    class Library:
        get_monotonic = staticmethod(time.monotonic)

    chelper.get_ffi = lambda: (None, Library())
    sys.modules['chelper'] = chelper
    for name in ('spidev', 'gpiod'):
        sys.modules[name] = types.ModuleType(name)


RESPONSES = []


class SimMove:
    def __init__(self, start, delta):
        self.start_pos = (0., 0., 0., start)
        self.end_pos = (0., 0., 0., start + delta)
        self.axes_d = (0., 0., 0., delta)
        self.axes_r = (0., 0., 0., 1. if delta >= 0 else -1.)
        self.accel = 1000.
        self.start_v = self.cruise_v = 5.
        self.accel_t = self.decel_t = 0.
        self.cruise_t = abs(delta) / 5.
        self.line = None


class SimExtruder:
    """Stands in for kinematics.extruder.PrinterExtruder."""

    def __init__(self, printer, name, move_function):
        self.printer = printer
        self.name = name
        self.extruder_num = 0 if name == 'extruder' else int(name[len('extruder'):])
        self.last_position = 0.
        self.trapq = None
        self.is_calibrating_flow = False
        self._move = move_function

    def trapq_append(self, *args):
        pass

    def move(self, print_time, move):
        # The firmware's PrinterExtruder.move, including its accounting hook.
        self._move(self, print_time, move)

    def get_name(self):
        return self.name

    def get_status(self, eventtime):
        return {'temperature': 220., 'target': 220., 'power': 0.,
                'can_extrude': True, 'pressure_advance': 0.,
                'smooth_time': 0.04, 'state': 'ACTIVATE' if self.printer.lookup_object(
                    'toolhead').extruder is self else 'PARKED'}


class SimToolhead:
    def __init__(self, printer):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.commanded_pos = [0., 0., 0., 0.]
        self.extruder = None
        self.print_time = 0.

    def get_status(self, eventtime):
        return {'position': list(self.commanded_pos),
                'extruder': self.extruder.get_name() if self.extruder else '',
                'homed_axes': 'xyz', 'print_time': self.print_time,
                'estimated_print_time': self.print_time, 'stalls': 0,
                'max_velocity': 300., 'max_accel': 5000.}

    def get_extruder(self):
        return self.extruder

    def set_extruder(self, extruder, extrude_pos):
        self.extruder = extruder
        self.commanded_pos[3] = extrude_pos

    def get_last_move_time(self):
        return self.print_time

    def dwell(self, delay):
        self.print_time += delay

    def wait_moves(self):
        pass

    def flush_step_generation(self):
        pass

    def extrude(self, delta):
        start = self.commanded_pos[3]
        self.commanded_pos[3] = start + delta
        move = SimMove(start, delta)
        self.print_time += move.cruise_t
        self.extruder.move(self.print_time, move)


class SimSensor:
    def __init__(self, detected):
        self.filament_detected = detected

    def get_status(self, eventtime):
        return {'filament_detected': self.filament_detected, 'enabled': True}


class SimPrintStats:
    def __init__(self):
        self.state = 'standby'

    def get_status(self, eventtime):
        return {'state': self.state, 'filename': '', 'message': '',
                'total_duration': 0., 'print_duration': 0., 'filament_used': 0.}


class SimReader:
    """Built-in reader for the `snapmaker` RFID mode.

    The real reader calls back from its own thread; this one does the same.
    """

    def __init__(self, enabled, tags):
        self.enabled = enabled
        self._tags = tags
        self._callback = None

    def register_cb_2_card_info_deal(self, callback):
        self._callback = callback

    def enable_first_scan_fail_fast(self):
        # Stock 2.1.0 asks the reader to give up early on the first scan
        # after the start. This model answers quickly anyway.
        pass

    def _deliver(self, channel, read):
        import extras.fm175xx_reader as reader
        time.sleep(.3)
        tag = self._tags.get(str(channel)) if read else None
        if not read:
            self._callback(channel, reader.FM175XX_CARD_INFO_CLEAR, reader.FM175XX_OK, None, None)
        elif tag is None:
            self._callback(channel, reader.FM175XX_CARD_INFO_READ, reader.FM175XX_CARD_TIMER_ERR,
                           None, None)
        else:
            data = list(bytes.fromhex(tag['uid'])) + [0] * 12
            self._callback(channel, reader.FM175XX_CARD_INFO_READ, reader.FM175XX_OK,
                           reader.FM175XX_MIFARE_CARD_TYPE_M1, data)

    def request_read_card_info(self, channel):
        threading.Thread(target=self._deliver, args=(channel, True), daemon=True).start()

    def request_clear_card_info(self, channel):
        threading.Thread(target=self._deliver, args=(channel, False), daemon=True).start()


def main():
    klippy_dir, config_file, api_socket, log_file, state_file = sys.argv[1:6]
    install_stubs()
    sys.path.insert(0, klippy_dir)
    logging.basicConfig(filename=log_file, level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    import klippy
    import reactor
    import configfile
    from kinematics import extruder as firmware_extruder

    with open(state_file) as stream:
        physical = json.load(stream)

    class SimPrinter(klippy.Printer):
        def set_main_mcu_power(self, state):
            pass

        def set_extruder_power(self, state, extruder=['all']):
            pass

        def _read_config(self):
            self.objects['configfile'] = pconfig = configfile.PrinterConfig(self)
            config = pconfig.read_main_config()
            sim = Sim(self, physical, firmware_extruder.PrinterExtruder.move)
            sim.add_objects()
            for section_config in config.get_prefix_sections(''):
                self.load_object(config, section_config.get_name(), None)
            sim.register_endpoints()
            pconfig.check_unused_options(config)

    start_args = {'config_file': config_file, 'apiserver': api_socket,
                  'start_reason': 'startup', 'factory_mode': False,
                  'software_version': 'sim', 'cpu_info': 'sim',
                  'log_file': log_file}
    # Klipper reads G-code from a pseudo-terminal. Nothing types into it here;
    # the other end only drains Klipper's output.
    gcode_fd, terminal = os.openpty()
    tty.setraw(terminal)
    threading.Thread(target=_drain, args=(terminal,), daemon=True).start()
    start_args['gcode_fd'] = gcode_fd
    klippy.queuefile.setup_bg_file_operations()
    try:
        klippy.asyncfilereader.setup_async_file_reader()
    except Exception:
        logging.exception('asyncfilereader setup failed')
    while True:
        main_reactor = reactor.Reactor(gc_checking=False)
        printer = SimPrinter(main_reactor, None, start_args)
        result = printer.run()
        if result in ('exit', 'error_exit'):
            break
        time.sleep(1.)
        main_reactor.finalize()
        logging.info('Restarting printer')
        start_args['start_reason'] = result
    klippy.queuefile.clear_bg_file_operations()


class Sim:
    """Hardware stand-ins and the control endpoints used by the tests."""

    def __init__(self, printer, physical, move_function):
        self.printer = printer
        self.physical = physical
        self.move_function = move_function
        # Kept across in-process RESTART, like the console history.
        self.responses = RESPONSES

    def add_objects(self):
        printer = self.printer
        self.toolhead = SimToolhead(printer)
        printer.add_object('toolhead', self.toolhead)
        self.extruders = []
        for index in range(4):
            name = 'extruder' if index == 0 else 'extruder%d' % index
            extruder = SimExtruder(printer, name, self.move_function)
            printer.add_object(name, extruder)
            self.extruders.append(extruder)
        self.toolhead.set_extruder(self.extruders[0], 0.)
        self.sensors = []
        for index in range(4):
            sensor = SimSensor(bool(self.physical['present'][index]))
            printer.add_object('filament_motion_sensor e%d_filament' % index, sensor)
            self.sensors.append(sensor)
        self.print_stats = SimPrintStats()
        printer.add_object('print_stats', self.print_stats)
        reader = SimReader(self.physical.get('builtin_reader', False), self.physical['tags'])
        printer.add_object('fm175xx_reader', reader)
        gcode = printer.lookup_object('gcode')
        gcode.register_command('FLOW_RESET_K', lambda gcmd: None)
        gcode.register_output_handler(self.responses.append)

    def register_endpoints(self):
        webhooks = self.printer.lookup_object('webhooks')
        webhooks.register_endpoint('sim/extrude', self._extrude)
        webhooks.register_endpoint('sim/activate', self._activate)
        webhooks.register_endpoint('sim/sensor', self._sensor)
        webhooks.register_endpoint('sim/print_state', self._print_state)
        webhooks.register_endpoint('sim/state', self._state)

    def _extrude(self, web_request):
        moves = web_request.get('moves')
        interval = web_request.get_float('interval', .05)
        reactor = self.printer.get_reactor()
        for delta in moves:
            self.toolhead.extrude(float(delta))
            reactor.pause(reactor.monotonic() + interval)
        web_request.send(self._snapshot())

    def _activate(self, web_request):
        index = web_request.get_int('index')
        extruder = self.extruders[index]
        if self.toolhead.extruder is not extruder:
            self.toolhead.set_extruder(extruder, extruder.last_position)
            self.printer.send_event('extruder:activate_extruder')
        web_request.send(self._snapshot())

    def _sensor(self, web_request):
        index = web_request.get_int('index')
        present = bool(web_request.get('present'))
        self.sensors[index].filament_detected = present
        self.printer.send_event('filament_switch_sensor:runout', index, present)
        web_request.send(self._snapshot())

    def _print_state(self, web_request):
        self.print_stats.state = web_request.get_str('state')
        web_request.send(self._snapshot())

    def _state(self, web_request):
        web_request.send(self._snapshot())

    def _snapshot(self):
        return {'position': self.toolhead.commanded_pos[3],
                'extruder': self.toolhead.extruder.get_name(),
                'last_positions': [e.last_position for e in self.extruders],
                'responses': list(self.responses)}


if __name__ == '__main__':
    main()
