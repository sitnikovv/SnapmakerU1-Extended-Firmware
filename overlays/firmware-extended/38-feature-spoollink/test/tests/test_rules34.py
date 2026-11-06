"""Rules 3 and 4: spools without tags and the first read after start."""

import time
import unittest

from base import Case, TAG_A, TAG_U

LIST = r'/api/v1/spool'


class FirstRead(Case):
    def test_B34_tagless_spool_after_power_off_swap_is_not_charged(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.shutdown()
        self.rig.swap_while_off(0, None)
        self.rig.boot()
        self.wait_reads_settled()
        with self.check('link removed after the empty first read'):
            self.assertLink(0, None)
        self.rig.extrude([1.] * 10)
        with self.check('nothing charged'):
            self.assertNoUsageFor(12., {})

    def test_B35_manual_link_survives_restart_with_empty_read(self):
        self.rig.create_spool('M')
        self.rig.load(0)
        self.rig.start_printer()
        self.wait_reads_settled()
        self.rig.select(0, self.rig.spools['M'])
        with self.step('channel linked to M'):
            self.assertLink(0, 'M')
        self.rig.shutdown()
        self.rig.boot()
        self.wait_reads_settled()
        with self.check('manual link survives the empty first read'):
            self.assertLinkStays(0, 'M')
        self.rig.extrude([1.] * 10)
        with self.check('usage on M'):
            self.assertUsage({'M': 10.})

    def test_B36_empty_read_during_work_keeps_tag_link(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.rfid.override[0] = 'empty'
        self.rig.reread(0)
        self.wait_reads_settled()
        with self.check('tag link survives an empty read'):
            self.assertLinkStays(0, 'A')
        self.rig.extrude([1.] * 10)
        with self.check('usage on A'):
            self.assertUsage({'A': 10.})

    def test_B37_same_tag_after_restart_keeps_link(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.shutdown()
        self.rig.boot()
        self.wait_reads_settled()
        with self.check('tag link survives restart'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 10)
        with self.check('usage on A'):
            self.assertUsage({'A': 10.})

    def test_B40_repeated_reads_of_the_same_tag_keep_counting(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 2)
        with self.check('usage before the reads'):
            self.assertUsage({'A': 2.})
        # Spoolman on the NAS answers each lookup in 2 s.
        self.rig.proxy.add_rule('slow', 'GET', LIST, 'delay', delay=2.)
        join = self.background(lambda: self.rig.extrude([.5] * 40, interval=.1))
        for _ in range(3):
            time.sleep(1.)
            self.rig.reread(0)
        join()
        self.rig.proxy.remove_rule('slow')
        with self.check('channel linked to A'):
            self.assertLink(0, 'A')
        with self.check('usage on A'):
            self.assertUsage({'A': 22.})

    def test_B50_another_tag_after_power_off_ends_the_manual_link(self):
        self.rig.create_spool('M')
        self.rig.load(0)
        self.rig.start_printer()
        self.wait_reads_settled()
        self.rig.select(0, self.rig.spools['M'])
        with self.step('channel linked to M'):
            self.assertLink(0, 'M')
        self.rig.extrude([1.] * 3)
        with self.check('usage on M'):
            self.assertUsage({'M': 3.})
        self.rig.shutdown()
        self.rig.swap_while_off(0, TAG_U)
        self.rig.boot()
        self.wait_reads_settled()
        with self.check('link removed after another tag is read'):
            self.assertLink(0, None)
        self.rig.extrude([1.] * 5)
        with self.check('nothing charged to M'):
            self.assertNoUsageFor(8., {'M': 3.})
        self.rig.rfid.override[0] = 'empty'
        self.rig.reread(0)
        self.wait_reads_settled()
        with self.check('empty read does not restore M'):
            self.assertLinkStays(0, None)
        self.rig.create_spool('N')
        self.rig.add_uid(self.rig.spools['N'], TAG_U.uid)
        with self.check('waiting usage reaches N'):
            self.assertUsage({'M': 3., 'N': 5.}, timeout=15.)


if __name__ == '__main__':
    unittest.main()
