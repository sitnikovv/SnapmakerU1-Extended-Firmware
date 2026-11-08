# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @sitnikovv

"""Rule 11: clearing a channel keeps a spool selected by hand for a channel without a tag.

Filament Manager reads the tags with the G-code that its script sends. The
tests take that G-code from the Filament Manager script of the image, so they
check the buttons the way a user presses them.
"""

import re
import time
import unittest

from base import Case, StandError, TAG_A

FILAMENT_MANAGER = 'usr/local/filament-ui/html/script.js'
LITERAL = re.compile(r'`([^`]*)`|\'([^\'\n]*)\'|"([^"\n]*)"')
VARIABLE = re.compile(r'\$\{\s*(\w+)\s*\}')


def filament_manager_gcode(rig, function, channel=None):
    """The G-code lines that a Filament Manager function sends, in their order.

    The lines are the FILAMENT_DT_ commands in the string literals of the
    function body. `${channel}` is the channel that the function gets; any
    other variable is a loop over the four channels.
    """
    text = (rig.fw / FILAMENT_MANAGER).read_text()
    start = re.search(r'\bfunction\s+%s\s*\(' % function, text)
    if start is None:
        raise StandError('Filament Manager has no function %s' % function)
    begin = text.index('{', start.end())
    depth = 0
    for end in range(begin, len(text)):
        depth += {'{': 1, '}': -1}.get(text[end], 0)
        if depth == 0:
            break
    lines = []
    for match in LITERAL.finditer(text[begin:end]):
        literal = next(group for group in match.groups() if group is not None)
        for line in re.split(r'\\n|\n', literal):
            line = line.strip()
            if not line.startswith('FILAMENT_DT_'):
                continue
            names = VARIABLE.findall(line)
            if not names:
                lines.append(line)
            elif names == ['channel']:
                lines.append(VARIABLE.sub(str(channel), line))
            else:
                lines.extend(VARIABLE.sub(str(index), line) for index in range(4))
    (rig.workdir / ('filament-manager-%s.txt' % function)).write_text('\n'.join(lines) + '\n')
    return lines


class ReadSpoolTags(Case):
    def tag_and_manual(self):
        """Channel 1 is linked to A through its tag; channel 2 has no tag and M is selected."""
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.create_spool('M')
        self.rig.load(0, TAG_A)
        self.rig.load(1)
        self.rig.start_printer()
        self.wait_reads_settled()
        self.rig.select(1, self.rig.spools['M'])
        with self.step('channels linked to A and M'):
            self.assertLink(0, 'A')
            self.assertLink(1, 'M')

    def test_B77_read_spool_tags_keeps_the_manual_spool(self):
        self.tag_and_manual()
        lines = filament_manager_gcode(self.rig, 'refreshAllChannels')
        with self.step('Read spool tags reads every channel'):
            for channel in range(4):
                self.assertIn('FILAMENT_DT_UPDATE CHANNEL=%d' % channel, lines)
        self.rig.gcode('\n'.join(lines))
        self.wait_reads_settled()
        with self.check('manual link of channel 2 stays'):
            self.assertLink(1, 'M')
            self.assertLinkStays(1, 'M')
        with self.check('channel 1 linked to A'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 5)
        self.rig.activate(1)
        self.rig.extrude([1.] * 10)
        with self.check('usage on A and M'):
            self.assertUsage({'A': 5., 'M': 10.})

    def test_B78_refresh_of_the_channel_keeps_the_manual_spool(self):
        self.tag_and_manual()
        lines = filament_manager_gcode(self.rig, 'refreshSingleChannel', 1)
        with self.step('Refresh reads channel 2'):
            self.assertIn('FILAMENT_DT_UPDATE CHANNEL=1', lines)
        self.rig.gcode('\n'.join(lines))
        self.wait_reads_settled()
        with self.check('manual link stays'):
            self.assertLink(1, 'M')
            self.assertLinkStays(1, 'M')
        self.rig.activate(1)
        self.rig.extrude([1.] * 10)
        with self.check('usage on M'):
            self.assertUsage({'M': 10.})

    def test_B79_refresh_of_a_tag_keeps_its_spool_during_the_read(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        lines = filament_manager_gcode(self.rig, 'refreshSingleChannel', 0)
        with self.step('Refresh reads channel 1'):
            self.assertIn('FILAMENT_DT_UPDATE CHANNEL=0', lines)
        # A slow Spoolman keeps a new lookup of the tag running for a while.
        self.rig.proxy.add_rule('slow lookup', 'GET', r'/api/v1/spool', 'delay', delay=6.)
        self.rig.gcode('\n'.join(lines))
        # The head feeds right after the button, before the tag is read again.
        self.rig.extrude([5.])
        with self.check('channel keeps A during the read'):
            self.assertLinkStays(0, 'A', seconds=4.)
        self.rig.proxy.remove_rule('slow lookup')
        self.wait_reads_settled()
        with self.check('usage on A'):
            self.assertUsage({'A': 5.}, timeout=20.)


class ClearCommand(Case):
    def manual(self):
        """Channel 1 has no tag and M is selected for it."""
        self.rig.create_spool('M')
        self.rig.load(0)
        self.rig.start_printer()
        self.wait_reads_settled()
        self.rig.select(0, self.rig.spools['M'])
        with self.step('channel linked to M'):
            self.assertLink(0, 'M')

    def test_B80_clear_and_read_from_a_macro_keep_the_manual_spool(self):
        self.manual()
        with self.step('screen shows the filament of M'):
            self.assertEqual(self.rig.channel_vendor(0), 'Snapmaker')
        self.rig.gcode('FILAMENT_DT_CLEAR CHANNEL=0\nFILAMENT_DT_UPDATE CHANNEL=0')
        self.wait_reads_settled()
        with self.check('manual link stays'):
            self.assertLink(0, 'M')
            self.assertLinkStays(0, 'M')
        with self.check('filament settings of M stay'):
            self.assertEqual(self.rig.channel_vendor(0), 'Snapmaker')
        self.rig.extrude([1.] * 10)
        with self.check('usage on M'):
            self.assertUsage({'M': 10.})
        self.rig.select(0, 0)
        with self.check('Clear removes the link'):
            self.assertLink(0, None)
        self.rig.extrude([1.] * 5)
        with self.check('nothing charged after Clear'):
            self.assertNoUsageFor(8., {'M': 10.})

    def test_B81_reset_channel_keeps_the_manual_spool_until_removal(self):
        self.manual()
        lines = filament_manager_gcode(self.rig, 'resetChannel', 0)
        with self.step('Reset channel clears channel 1'):
            self.assertIn('FILAMENT_DT_CLEAR CHANNEL=0', lines)
        self.rig.gcode('\n'.join(lines))
        self.wait_reads_settled()
        with self.check('manual link stays'):
            self.assertLink(0, 'M')
            self.assertLinkStays(0, 'M')
        self.rig.extrude([1.] * 5)
        with self.check('usage on M'):
            self.assertUsage({'M': 5.})
        self.rig.remove(0)
        with self.check('removing the filament removes the link'):
            self.assertLink(0, None)
        self.rig.extrude([1.] * 5)
        with self.check('nothing charged after the removal'):
            self.assertNoUsageFor(8., {'M': 5.})

    def test_B82_clear_keeps_usage_that_waits_for_a_selection(self):
        self.manual()
        self.rig.create_spool('N')
        self.rig.extrude([1.] * 4)
        with self.check('usage on M'):
            self.assertUsage({'M': 4., 'N': 0.})
        self.rig.select(0, 999)
        time.sleep(1.)
        self.rig.extrude([1.] * 20)
        with self.check('nothing charged after the bad number'):
            self.assertNoUsageFor(6., {'M': 4., 'N': 0.})
        with self.check('channel unlinked'):
            self.assertLink(0, None)
        self.rig.gcode('FILAMENT_DT_CLEAR CHANNEL=0\nFILAMENT_DT_UPDATE CHANNEL=0')
        self.wait_reads_settled()
        self.rig.select(0, self.rig.spools['N'])
        with self.check('channel linked to N'):
            self.assertLink(0, 'N')
        with self.check('waiting usage reaches N'):
            self.assertUsage({'M': 4., 'N': 20.}, timeout=15.)


class BuiltinReaderClear(Case):
    rig_options = {'builtin_reader': True}

    def test_B83_builtin_reader_clear_keeps_the_manual_spool_until_removal(self):
        self.rig.create_spool('M')
        self.rig.load(0)
        self.rig.start_printer()
        self.wait_reads_settled()
        self.rig.select(0, self.rig.spools['M'])
        with self.step('channel linked to M'):
            self.assertLink(0, 'M')
        self.rig.gcode('FILAMENT_DT_CLEAR CHANNEL=0\nFILAMENT_DT_UPDATE CHANNEL=0')
        self.wait_reads_settled()
        with self.check('manual link stays'):
            self.assertLink(0, 'M')
            self.assertLinkStays(0, 'M')
        self.rig.extrude([1.] * 10)
        with self.check('usage on M'):
            self.assertUsage({'M': 10.})
        self.rig.remove(0)
        with self.check('removing the filament removes the link'):
            self.assertLink(0, None)
        self.rig.extrude([1.] * 5)
        with self.check('nothing charged after the removal'):
            self.assertNoUsageFor(8., {'M': 10.})


if __name__ == '__main__':
    unittest.main()
