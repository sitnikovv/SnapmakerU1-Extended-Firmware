"""Edge cases of rule 2 and of usage delivery.

The rules are listed in README.md.
"""

import time
import unittest

from base import Case, TAG_A, TAG_U, number
from rig import StandError

USE = r'/api/v1/spool/\d+/use'
LIST = r'/api/v1/spool'


class BadNumberWhilePrinting(Case):
    """A selection of a missing spool while the head keeps feeding."""

    def test_B64_bad_number_while_feeding_waits_for_selection(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.create_spool('N')
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 4)
        with self.check('usage on A'):
            self.assertUsage({'A': 4.})
        since = len(self.rig.responses())
        # The head feeds while the firmware learns that 999 does not exist,
        # as in a print that goes on after a typing mistake. Small, frequent
        # moves make sure that some feed happens while the answer is handled.
        self.rig.select(0, 999)
        self.rig.extrude([.05] * 200, interval=.01)
        time.sleep(1.)
        with self.check('nothing charged after the bad number'):
            self.assertNoUsageFor(6., {'A': 4.})
        with self.check('message about the bad number'):
            self.assertMessage(since, 0, number(999))
        with self.check('channel unlinked'):
            self.assertLink(0, None)
        self.rig.select(0, self.rig.spools['N'])
        with self.check('waiting usage reaches N'):
            self.assertUsage({'A': 4., 'N': 10.}, timeout=15.)
        with self.check('channel linked to N'):
            self.assertLink(0, 'N')
        self.rig.extrude([1.] * 2)
        with self.check('later usage reaches N'):
            self.assertUsage({'A': 4., 'N': 12.})


class TaglessWaitingAcrossRestarts(Case):
    """A tagless channel that lost its spool keeps waiting across restarts."""

    def tagless_bad_number(self):
        """A tagless channel on M gets a number Spoolman does not have; 20 mm wait."""
        self.rig.create_spool('M')
        self.rig.create_spool('N')
        self.rig.load(0)
        self.rig.start_printer()
        self.wait_reads_settled()
        self.rig.select(0, self.rig.spools['M'])
        with self.step('channel linked to M'):
            self.assertLink(0, 'M')
        self.rig.extrude([1.] * 4)
        with self.check('usage on M'):
            self.assertUsage({'M': 4.})
        self.rig.select(0, 999)
        time.sleep(1.)
        self.rig.extrude([1.] * 20)
        with self.check('nothing charged after the bad number'):
            self.assertNoUsageFor(6., {'M': 4.})
        with self.check('channel unlinked'):
            self.assertLink(0, None)

    def test_B65_tagless_waiting_survives_klipper_restart(self):
        self.tagless_bad_number()
        self.rig.restart_klipper()
        self.wait_reads_settled()
        with self.check('channel still unlinked after the restart'):
            self.assertLink(0, None)
        self.rig.extrude([1.] * 5)
        with self.check('nothing charged after the restart'):
            self.assertNoUsageFor(6., {'M': 4.})
        self.rig.select(0, self.rig.spools['N'])
        with self.check('waiting usage reaches N'):
            self.assertUsage({'M': 4., 'N': 25.}, timeout=15.)

    def test_B66_tagless_waiting_survives_printer_restart(self):
        self.tagless_bad_number()
        self.rig.reboot()
        self.wait_reads_settled()
        with self.check('channel still unlinked after the restart'):
            self.assertLink(0, None)
        self.rig.extrude([1.] * 5)
        with self.check('nothing charged after the restart'):
            self.assertNoUsageFor(6., {'M': 4.})
        self.rig.select(0, self.rig.spools['N'])
        with self.check('waiting usage reaches N'):
            self.assertUsage({'M': 4., 'N': 25.}, timeout=15.)

    def test_B67_removal_after_klipper_restart_discards_tagless_waiting(self):
        self.tagless_bad_number()
        self.rig.restart_klipper()
        self.wait_reads_settled()
        self.rig.remove(0)
        time.sleep(1.)
        self.rig.insert(0)
        self.wait_reads_settled()
        self.rig.select(0, self.rig.spools['N'])
        with self.check('channel linked to N'):
            self.assertLink(0, 'N')
        self.rig.extrude([1.] * 2)
        with self.check('only new usage reaches N'):
            self.assertUsage({'M': 4., 'N': 2.})


class WaitingTagWhilePrinting(Case):
    """Rule 2e: a waiting tag is not polled on every usage report of another spool."""

    def list_requests(self, start, end):
        return [row['time'] for row in self.rig.proxy.requests('GET', LIST)
                if 'limit=1000' in row['query'] and start <= row['time'] <= end]

    def test_B68_waiting_tag_is_not_polled_while_another_spool_prints(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.create_spool('N')
        self.rig.load(0, TAG_A)
        self.rig.load(1, TAG_U)
        self.rig.start_printer()
        self.wait_reads_settled()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.activate(1)
        self.rig.extrude([1.] * 5)
        self.rig.activate(0)
        time.sleep(5.)
        # One minute of printing on A. Each usage report changes spool A in
        # Spoolman, and Spoolman announces every change.
        start = time.time()
        self.rig.extrude([.5] * 120, interval=.5)
        end = time.time()
        with self.check('usage on A'):
            self.assertUsage({'A': 60., 'N': 0.}, timeout=20.)
        with self.check('spool list requests in 60 s of printing'):
            self.assertLessEqual(len(self.list_requests(start, end)), 5,
                                 'Spool list requests in 60 s of printing')
        self.rig.add_uid(self.rig.spools['N'], TAG_U.uid)
        with self.check('waiting usage reaches N'):
            self.assertUsage({'A': 60., 'N': 5.}, timeout=15.)


class DeletionDuringDelivery(Case):
    """The spool is deleted while its usage request is on the way."""

    def test_B69_spool_deleted_while_its_usage_is_being_sent(self):
        self.rig.create_spool('A', [TAG_A.uid])
        # SQLite gives a deleted last number to the next spool; the spare prevents it.
        self.rig.create_spool('spare')
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        spool = self.rig.spools['A']
        self.rig.print_state('printing')
        self.rig.extrude([1.] * 5)
        with self.check('usage before deletion'):
            self.assertUsage({'A': 5., 'spare': 0.})
        # Spoolman records the next usage request, but its reply is held.
        self.rig.proxy.add_rule('hold', 'PUT', USE, 'hold_after_commit')
        self.rig.extrude([1.] * 7)
        def held():
            return [row for row in self.rig.proxy.requests('PUT', USE)
                    if row['outcome'] and row['outcome'].startswith('held')]
        # The proxy marks a request as held once Spoolman has answered it.
        with self.step('usage request on the way'):
            self.wait_until(lambda: held(), 15., 'No usage request was held')
        time.sleep(1.)
        held = held()
        applied = sum(row['body']['use_length'] for row in held)
        self.rig.delete_spool(spool)
        time.sleep(2.)
        self.rig.proxy.remove_rule('hold')
        self.rig.extrude([1.] * 3)
        self.rig.create_spool('P', [TAG_A.uid])
        # Spoolman applied the held request to A before the deletion. The
        # rest of the 7 mm was not delivered and waits for the tag.
        with self.check('undelivered usage reaches P'):
            self.assertUsage({'A': None, 'spare': 0., 'P': 7. - applied + 3.}, timeout=20.)
        self.rig.extrude([1.] * 2)
        with self.check('later usage reaches P'):
            self.assertUsage({'A': None, 'spare': 0., 'P': 7. - applied + 5.})


class InvalidNumber(Case):
    """A negative spool number must not stop accounting."""

    def test_B70_negative_number_does_not_stop_accounting(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        # Channel 3 has no spool and was not read since the start.
        self.rig.gcode('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=3 FILAMENT_SPOOL_ID=-1 FORCE=1')
        self.rig.activate(3)
        self.rig.extrude([1.] * 3)
        self.rig.activate(0)
        self.rig.extrude([1.] * 2)
        with self.check('usage on A'):
            self.assertUsage({'A': 2.})
        try:
            self.rig.restart_moonraker()
            failure = None
        except StandError as error:
            failure = error
        with self.check('no failed component after restart'):
            self.assertIsNone(failure, 'Moonraker did not start: %s' % failure)
            self.assertEqual(self.rig.moonraker_api('GET', '/server/info')['failed_components'], [])
        self.rig.extrude([1.] * 3)
        with self.check('usage on A after restart'):
            self.assertUsage({'A': 5.}, timeout=20.)


if __name__ == '__main__':
    unittest.main()
