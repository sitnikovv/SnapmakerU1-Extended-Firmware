"""Accounting without the Klipper router, database writes, built-in reader."""

import time
import unittest

from base import Case, TAG_A


class WithoutRouter(Case):
    # Spoolman was enabled in firmware-config and Moonraker restarted with
    # SpoolLink, but Klipper still runs without the `[spoollink]` section.
    rig_options = {'spoollink': False}

    def test_B41_accounting_without_klipper_spoollink(self):
        self.rig.create_spool('M')
        self.rig.load(0)
        self.rig.start_printer()
        self.wait_reads_settled()
        self.rig.gcode('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=0 FILAMENT_SPOOL_ID=%d'
                       % self.rig.spools['M'])
        with self.step('channel linked to M'):
            self.assertLink(0, 'M')
        time.sleep(1.)
        self.rig.extrude([1.] * 10)
        with self.check('usage on M'):
            self.assertUsage({'M': 10.})


class WithoutSpoolLink(Case):
    # Spoolman is configured by hand and SpoolLink is not used at all.
    rig_options = {'spoollink': False, 'moonraker_spoollink': False}

    def test_B53_plain_moonraker_spoolman_integration(self):
        self.rig.create_spool('M')
        self.rig.load(0)
        self.rig.start_printer()
        self.wait_reads_settled()
        # Fluidd and Mainsail select the active spool through this endpoint.
        self.rig.moonraker_api('POST', '/server/spoolman/spool_id', {'spool_id': self.rig.spools['M']})
        with self.check('active spool is M'):
            self.assertEqual(self.rig.moonraker_api('GET', '/server/spoolman/spool_id')['spool_id'],
                             self.rig.spools['M'])
        self.rig.extrude([1.] * 10)
        with self.check('usage on M'):
            self.assertUsage({'M': 10.})


class GenericVendor(Case):
    rig_options = {'force_generic_vendor': True}

    def test_B62_generic_vendor_option_still_works(self):
        filament = self.rig.create_filament('Elegoo')
        self.rig.create_spool('G', [TAG_A.uid], filament=filament)
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to G'):
            self.assertLink(0, 'G')
        with self.check('screen shows the Generic vendor'):
            self.assertEqual(self.rig.channel_vendor(0), 'Generic')
        self.rig.extrude([1.] * 5)
        with self.check('usage on G'):
            self.assertUsage({'G': 5.})


class DatabaseWrites(Case):
    rig_options = {'sql_trace': True}

    def test_B43_one_minute_of_printing_writes_little(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        start = time.time()
        self.rig.extrude([.25] * 240, interval=.25)
        end = time.time()
        with self.check('usage on A'):
            self.assertUsage({'A': 60.})
        with self.check('database writes'):
            trace = self.rig.workdir / 'sql-trace.log'
            rows = trace.read_text().splitlines() if trace.exists() else []
            writes = [row for row in rows if start <= float(row.split(' ', 1)[0]) <= end]
            self.assertLessEqual(len(writes), 30, 'Database writes during one minute of printing')


class BuiltinReader(Case):
    rig_options = {'builtin_reader': True}

    def test_B44_builtin_reader_tag_is_charged(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 10)
        with self.check('usage on A'):
            self.assertUsage({'A': 10.})


if __name__ == '__main__':
    unittest.main()
