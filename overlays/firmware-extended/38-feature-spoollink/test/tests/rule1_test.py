# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @sitnikovv

"""Rule 1: a channel linked to a Spoolman spool charges its usage to that spool."""

import time
import unittest

from base import Case, TAG_A, TAG_A_PARSED, TAG_B


class Rule1(Case):
    def test_B01_tag_link_charges_its_spool(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.create_spool('other')
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 10)
        with self.check('usage on A'):
            self.assertUsage({'A': 10.})

    def test_B02_tag_with_filament_data_charges_its_spool(self):
        self.rig.create_spool('A', [TAG_A_PARSED.uid])
        self.rig.load(0, TAG_A_PARSED)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 10)
        with self.check('usage on A'):
            self.assertUsage({'A': 10.})

    def test_B03_manual_selection_charges_the_selected_spool(self):
        self.rig.create_spool('M')
        self.rig.load(0)
        self.rig.start_printer()
        self.wait_reads_settled()
        self.rig.select(0, self.rig.spools['M'])
        with self.step('channel linked to M'):
            self.assertLink(0, 'M')
        # The screen edits the filament of another channel.
        self.rig.gcode('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=1 VENDOR=Generic '
                       'FILAMENT_TYPE=PLA FILAMENT_SUBTYPE=Basic')
        with self.check('link stays after another channel is edited'):
            self.assertLinkStays(0, 'M')
        self.rig.extrude([1.] * 10)
        with self.check('usage on M'):
            self.assertUsage({'M': 10.})

    def test_B04_unlinked_channel_charges_nothing(self):
        self.rig.create_spool('M')
        self.rig.load(0)
        self.rig.start_printer()
        self.wait_reads_settled()
        self.rig.extrude([1.] * 10)
        with self.check('nothing charged'):
            self.assertNoUsageFor(12., {})

    def test_B05_tool_changes_charge_each_head(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.create_spool('B', [TAG_B.uid])
        self.rig.load(0, TAG_A)
        self.rig.load(1, TAG_B)
        self.rig.start_printer()
        with self.step('channels linked to A and B'):
            self.assertLink(0, 'A')
            self.assertLink(1, 'B')
        self.rig.extrude([1.] * 5)
        self.rig.activate(1)
        self.rig.extrude([1.] * 7)
        self.rig.activate(0)
        self.rig.extrude([1.] * 3)
        with self.check('usage after head changes'):
            self.assertUsage({'A': 8., 'B': 7.})

    def test_B06_retraction_is_not_charged_twice(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 10 + [-2., 2., 1., 1., 1.])
        with self.check('usage on A'):
            self.assertUsage({'A': 13.})

    def test_B07_retraction_before_tool_change_is_not_charged_twice(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.create_spool('B', [TAG_B.uid])
        self.rig.load(0, TAG_A)
        self.rig.load(1, TAG_B)
        self.rig.start_printer()
        with self.step('channels linked to A and B'):
            self.assertLink(0, 'A')
            self.assertLink(1, 'B')
        self.feed_then_retract([1.] * 10, -2.)
        self.rig.activate(1)
        self.rig.extrude([1.] * 5)
        self.rig.activate(0)
        self.rig.extrude([2., 1., 1., 1.])
        with self.check('usage after head changes'):
            self.assertUsage({'A': 13., 'B': 5.})

    def two_heads(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.create_spool('B', [TAG_B.uid])
        self.rig.load(0, TAG_A)
        self.rig.load(1, TAG_B)
        self.rig.start_printer()
        with self.step('channels linked to A and B'):
            self.assertLink(0, 'A')
            self.assertLink(1, 'B')

    def test_B42_service_commands_clear_the_channel(self):
        self.two_heads()
        self.rig.gcode('FILAMENT_DT_CLEAR CHANNEL=0')
        with self.check('channel 1 cleared by FILAMENT_DT_CLEAR'):
            self.assertLink(0, None)
        self.rig.extrude([1.] * 5)
        with self.check('nothing charged after FILAMENT_DT_CLEAR'):
            self.assertNoUsageFor(8., {})
        self.rig.gcode('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=1 FILAMENT_SPOOL_ID=0 FORCE=1')
        with self.check('channel 2 cleared by SET_PRINT_FILAMENT_CONFIG'):
            self.assertLink(1, None)
        self.rig.activate(1)
        self.rig.extrude([1.] * 5)
        with self.check('nothing charged after SET_PRINT_FILAMENT_CONFIG'):
            self.assertNoUsageFor(10., {})

    def test_B61_read_on_another_channel_does_not_disturb_the_active_head(self):
        self.two_heads()
        self.rig.activate(1)
        join = self.background(lambda: self.rig.extrude([.5] * 30, interval=.1))
        for _ in range(2):
            time.sleep(1.)
            self.rig.reread(0)
        join()
        with self.check('usage on B'):
            self.assertUsage({'B': 15.})
        with self.check('channel 1 still linked to A'):
            self.assertLink(0, 'A')


if __name__ == '__main__':
    unittest.main()
