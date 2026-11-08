# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @sitnikovv

"""Rule 2: usage without a known spool waits until the spool is known."""

import time
import unittest

from base import Case, TAG_A, TAG_B, TAG_U, TAG_V, number
from rig import TAG_STORE

USE = r'/api/v1/spool/\d+/use'
LIST = r'/api/v1/spool'


class Waiting(Case):
    """Usage of an unregistered tag U waits and reaches the spool registered later."""

    def unknown_tag(self):
        """Tag U is not in Spoolman. The head feeds 8 mm of new filament."""
        self.rig.create_spool('N')
        self.rig.load(0, TAG_U)
        self.rig.start_printer()
        self.wait_reads_settled()
        # The retraction and its return add no new filament.
        self.rig.extrude([1.] * 6 + [-2., 2., 1., 1.])
        with self.check('nothing charged while the tag is unknown'):
            self.assertNoUsageFor(6., {})

    def create_from_tag(self, name, tag):
        # Filament Manager creates the spool together with the tag UID.
        self.rig.create_spool(name, [tag.uid])

    def test_B17_registration_by_uid_charges_and_links(self):
        self.unknown_tag()
        self.rig.add_uid(self.rig.spools['N'], TAG_U.uid)
        with self.check('waiting usage reaches N'):
            self.assertUsage({'N': 8.}, timeout=15.)
        with self.check('channel linked to N'):
            self.assertLink(0, 'N', timeout=5.)
        self.rig.extrude([1.] * 3)
        with self.check('later usage reaches N'):
            self.assertUsage({'N': 11.})

    def test_B18_spool_created_from_the_tag_charges_and_links(self):
        self.unknown_tag()
        self.create_from_tag('P', TAG_U)
        with self.check('waiting usage reaches P'):
            self.assertUsage({'P': 8.}, timeout=15.)
        with self.check('channel linked to P'):
            self.assertLink(0, 'P', timeout=5.)
        self.rig.extrude([1.] * 3)
        with self.check('later usage reaches P'):
            self.assertUsage({'P': 11.})

    def test_B19_registration_and_reread(self):
        self.unknown_tag()
        self.rig.add_uid(self.rig.spools['N'], TAG_U.uid)
        self.rig.reread(0)
        with self.check('channel linked to N'):
            self.assertLink(0, 'N')
        self.rig.extrude([1.] * 3)
        with self.check('all usage reaches N'):
            self.assertUsage({'N': 11.}, timeout=15.)

    def paused_swap(self):
        """Print on A, pause, swap in a spool with the unregistered tag U, finish."""
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.print_state('printing')
        self.rig.extrude([1.] * 6)
        with self.check('usage before the swap'):
            self.assertUsage({'A': 6.})
        self.rig.print_state('paused')
        self.rig.remove(0)
        time.sleep(1.)
        self.rig.insert(0, TAG_U)
        self.wait_reads_settled()
        self.rig.print_state('printing')
        self.rig.extrude([1.] * 9)
        self.rig.print_state('complete')
        with self.check('nothing charged while the tag is unknown'):
            self.assertNoUsageFor(6., {'A': 6.})

    def test_B20_paused_swap_then_registration_by_uid(self):
        self.paused_swap()
        self.rig.create_spool('N')
        self.rig.add_uid(self.rig.spools['N'], TAG_U.uid)
        with self.check('waiting usage reaches N'):
            self.assertUsage({'A': 6., 'N': 9.}, timeout=15.)
        with self.check('channel linked to N'):
            self.assertLink(0, 'N', timeout=5.)
        self.rig.extrude([1.] * 2)
        with self.check('later usage reaches N'):
            self.assertUsage({'A': 6., 'N': 11.})

    def test_B21_paused_swap_then_spool_created_from_the_tag(self):
        self.paused_swap()
        self.create_from_tag('N', TAG_U)
        with self.check('waiting usage reaches N'):
            self.assertUsage({'A': 6., 'N': 9.}, timeout=15.)
        with self.check('channel linked to N'):
            self.assertLink(0, 'N', timeout=5.)
        self.rig.extrude([1.] * 2)
        with self.check('later usage reaches N'):
            self.assertUsage({'A': 6., 'N': 11.})

    def test_B22_waiting_survives_replacement_of_the_tagged_spool(self):
        self.unknown_tag()
        self.rig.create_spool('B', [TAG_B.uid])
        self.rig.remove(0)
        time.sleep(2.)
        self.rig.insert(0, TAG_B)
        self.wait_reads_settled()
        with self.check('channel linked to B'):
            self.assertLink(0, 'B')
        self.rig.extrude([1.] * 3)
        with self.check('replacement spool charged'):
            self.assertUsage({'B': 3.})
        self.rig.add_uid(self.rig.spools['N'], TAG_U.uid)
        with self.check('waiting usage reaches N'):
            self.assertUsage({'B': 3., 'N': 8.}, timeout=15.)

    def test_B23_waiting_survives_graceful_moonraker_restart(self):
        self.unknown_tag()
        self.rig.restart_moonraker()
        self.rig.add_uid(self.rig.spools['N'], TAG_U.uid)
        with self.check('waiting usage reaches N'):
            self.assertUsage({'N': 8.}, timeout=15.)

    def test_B24_waiting_survives_killed_moonraker(self):
        self.unknown_tag()
        self.rig.restart_moonraker(kill=True)
        self.rig.add_uid(self.rig.spools['N'], TAG_U.uid)
        with self.check('waiting usage reaches N'):
            self.assertUsage({'N': 8.}, timeout=15.)

    def test_B25_waiting_survives_klipper_restart(self):
        self.unknown_tag()
        self.rig.restart_klipper()
        self.wait_reads_settled()
        self.rig.add_uid(self.rig.spools['N'], TAG_U.uid)
        with self.check('waiting usage reaches N'):
            self.assertUsage({'N': 8.}, timeout=15.)

    def test_B26_waiting_survives_printer_restart(self):
        self.unknown_tag()
        self.rig.shutdown()
        self.rig.boot()
        self.wait_reads_settled()
        self.rig.add_uid(self.rig.spools['N'], TAG_U.uid)
        with self.check('waiting usage reaches N'):
            self.assertUsage({'N': 8.}, timeout=15.)

    def test_B51_manual_selection_takes_the_waiting_usage(self):
        self.unknown_tag()
        self.rig.select(0, self.rig.spools['N'])
        with self.check('waiting usage reaches N'):
            self.assertUsage({'N': 8.}, timeout=15.)
        with self.check('channel linked to N'):
            self.assertLink(0, 'N')
        with self.check('tag bound to N'):
            self.assertIn(TAG_U.uid, self.rig.card_uids(self.rig.spools['N']))
        self.rig.extrude([1.] * 2)
        with self.check('later usage reaches N'):
            self.assertUsage({'N': 10.})

    def test_B52_two_unknown_tags_on_two_heads_wait_separately(self):
        self.rig.create_spool('N')
        self.rig.create_spool('P')
        self.rig.load(0, TAG_U)
        self.rig.load(1, TAG_V)
        self.rig.start_printer()
        self.wait_reads_settled()
        self.rig.extrude([1.] * 4)
        self.rig.activate(1)
        self.rig.extrude([1.] * 6)
        with self.check('nothing charged while the tags are unknown'):
            self.assertNoUsageFor(6., {})
        self.rig.add_uid(self.rig.spools['P'], TAG_V.uid)
        with self.check('second head usage reaches P'):
            self.assertUsage({'P': 6.}, timeout=15.)
        self.rig.add_uid(self.rig.spools['N'], TAG_U.uid)
        with self.check('first head usage reaches N'):
            self.assertUsage({'N': 4., 'P': 6.}, timeout=15.)

    def test_B32_tag_waiting_survives_clear(self):
        self.unknown_tag()
        self.rig.select(0, 0)
        time.sleep(2.)
        self.rig.add_uid(self.rig.spools['N'], TAG_U.uid)
        with self.check('waiting usage reaches N'):
            self.assertUsage({'N': 8.}, timeout=15.)


class MissingSpool(Case):
    """The channel's spool disappears from Spoolman."""

    def test_B27_spool_deleted_during_print(self):
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
            self.assertUsage({'A': 5.})
        # Spoolman refuses the next usage, so 7 mm are unsent at the deletion.
        self.rig.proxy.add_rule('hold', 'PUT', USE, 'status', status=503)
        self.rig.extrude([1.] * 7)
        time.sleep(1.)
        since = len(self.rig.responses())
        self.rig.delete_spool(spool)
        with self.check('message about the deleted spool'):
            self.assertMessage(since, 0, number(spool))
        with self.check('channel unlinked'):
            self.assertLink(0, None)
        self.rig.proxy.remove_rule('hold')
        self.rig.extrude([1.] * 8)
        with self.check('nothing charged while the tag is unknown'):
            self.assertNoUsageFor(6., {'A': None})
        self.rig.create_spool('P', [TAG_A.uid])
        with self.check('waiting usage reaches P'):
            self.assertUsage({'A': None, 'P': 15.}, timeout=20.)
        with self.check('channel linked to P'):
            self.assertLink(0, 'P', timeout=5.)
        self.rig.extrude([1.] * 3)
        with self.check('later usage reaches P'):
            self.assertUsage({'A': None, 'P': 18.})

    def tagless_bad_number(self):
        """A tagless channel on M gets SET_SPOOL_ID with a number Spoolman does not have."""
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
        since = len(self.rig.responses())
        self.rig.select(0, 999)
        time.sleep(1.)
        self.rig.extrude([1.] * 20)
        with self.check('nothing charged after the bad number'):
            self.assertNoUsageFor(6., {'M': 4.})
        return since

    def test_B28_unknown_number_without_tag_waits_for_selection(self):
        since = self.tagless_bad_number()
        with self.check('message about the bad number'):
            self.assertMessage(since, 0, number(999))
        with self.check('channel unlinked'):
            self.assertLink(0, None)
        self.rig.select(0, self.rig.spools['N'])
        with self.check('waiting usage reaches N'):
            self.assertUsage({'M': 4., 'N': 20.}, timeout=15.)
        self.rig.extrude([1.] * 2)
        with self.check('later usage reaches N'):
            self.assertUsage({'M': 4., 'N': 22.})

    def test_B29_unknown_number_with_tag_waits_for_selection(self):
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
        self.rig.select(0, 999)
        time.sleep(1.)
        self.rig.extrude([1.] * 10)
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
        with self.check('tag moved to N'):
            self.assertIn(TAG_A.uid, self.rig.card_uids(self.rig.spools['N']))
            self.assertNotIn(TAG_A.uid, self.rig.card_uids(self.rig.spools['A']))
        self.rig.extrude([1.] * 2)
        with self.check('later usage reaches N'):
            self.assertUsage({'A': 4., 'N': 12.})

    def test_B30_clear_discards_tagless_waiting(self):
        self.tagless_bad_number()
        self.rig.select(0, 0)
        time.sleep(1.)
        self.rig.select(0, self.rig.spools['N'])
        with self.check('channel linked to N'):
            self.assertLink(0, 'N')
        self.rig.extrude([1.] * 2)
        with self.check('only new usage reaches N'):
            self.assertUsage({'M': 4., 'N': 2.})

    def test_B31_removing_tagless_spool_discards_waiting(self):
        self.tagless_bad_number()
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

    def test_B33_failed_clear_keeps_usage_off_the_old_spool(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.create_spool('N')
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 4)
        with self.check('usage on A'):
            self.assertUsage({'A': 4.})
        # Spoolman cannot remove the tag from A, so Clear fails on the server side.
        self.rig.block_tag_writes('patch')
        self.rig.select(0, 0)
        with self.check('channel cleared'):
            self.assertLink(0, None)
        time.sleep(10.)
        self.rig.reread(0)
        self.wait_reads_settled()
        with self.check('channel stays cleared after the same tag is read'):
            self.assertLinkStays(0, None, seconds=3.)
        self.rig.extrude([1.] * 8)
        with self.check('nothing charged to the old spool'):
            self.assertNoUsageFor(8., {'A': 4.})
        self.rig.proxy.remove_rule('patch')
        with self.check('tag removed from A after Spoolman recovers'):
            self.wait_until(lambda: TAG_A.uid not in self.rig.card_uids(self.rig.spools['A']),
                            60., 'The tag is still on spool A')
        self.rig.select(0, self.rig.spools['N'])
        with self.check('waiting usage reaches N'):
            self.assertUsage({'A': 4., 'N': 8.}, timeout=15.)
        self.rig.extrude([1.] * 2)
        with self.check('later usage reaches N'):
            self.assertUsage({'A': 4., 'N': 10.})


class MissingSpoolMore(Case):
    def test_B63_new_spool_with_the_deleted_number_gets_nothing(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 4)
        with self.check('usage on A'):
            self.assertUsage({'A': 4.})
        number_a = self.rig.spools.pop('A')
        self.rig.delete_spool(number_a)
        time.sleep(3.)
        # SQLite gives the deleted last number to the next spool.
        self.rig.create_spool('Q')
        with self.step('Q has the number of A'):
            self.assertEqual(self.rig.spools['Q'], number_a)
        with self.check('channel unlinked'):
            self.assertLink(0, None)
        self.rig.extrude([1.] * 8)
        with self.check('nothing charged to the new spool'):
            self.assertNoUsageFor(12., {})

    def test_B47_tag_on_two_spools_waits_until_it_is_unique(self):
        if TAG_STORE == 'native':
            self.skipTest('Spoolman links a native tag to one spool only')
        self.rig.create_spool('N', [TAG_U.uid])
        self.rig.create_spool('P', [TAG_U.uid])
        self.rig.load(0, TAG_U)
        self.rig.start_printer()
        self.wait_reads_settled()
        with self.check('message names both spools'):
            self.assertMessage(0, 0, number(self.rig.spools['N']))
            self.assertMessage(0, 0, number(self.rig.spools['P']))
        with self.check('channel unlinked'):
            self.assertLink(0, None)
        self.rig.extrude([1.] * 5)
        with self.check('nothing charged while the tag is on two spools'):
            self.assertNoUsageFor(8., {})
        self.rig.set_uids(self.rig.spools['P'], [])
        with self.check('waiting usage reaches N'):
            self.assertUsage({'N': 5.}, timeout=15.)
        with self.check('channel linked to N'):
            self.assertLink(0, 'N', timeout=5.)


class Clearing(Case):
    """SET_SPOOL_ID 0 clears the channel and removes the tag from its spool."""

    def tagged_on_a(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.create_spool('N')
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 4)
        with self.check('usage on A'):
            self.assertUsage({'A': 4.})

    def test_B49_successful_clear_then_the_same_tag_is_read(self):
        self.tagged_on_a()
        self.rig.select(0, 0)
        with self.check('channel cleared'):
            self.assertLink(0, None)
        with self.check('tag removed from A'):
            self.wait_until(lambda: TAG_A.uid not in self.rig.card_uids(self.rig.spools['A']),
                            15., 'The tag is still on spool A')
        self.rig.reread(0)
        self.wait_reads_settled()
        with self.check('channel stays cleared after the same tag is read'):
            self.assertLinkStays(0, None, seconds=3.)
        self.rig.extrude([1.] * 8)
        with self.check('nothing charged to the old spool'):
            self.assertNoUsageFor(8., {'A': 4.})
        self.rig.select(0, self.rig.spools['N'])
        with self.check('waiting usage reaches N'):
            self.assertUsage({'A': 4., 'N': 8.}, timeout=15.)
        self.rig.extrude([1.] * 2)
        with self.check('later usage reaches N'):
            self.assertUsage({'A': 4., 'N': 10.})

    def test_B54_failed_clear_survives_moonraker_restart(self):
        self.tagged_on_a()
        self.rig.block_tag_writes('patch')
        self.rig.select(0, 0)
        with self.check('channel cleared'):
            self.assertLink(0, None)
        time.sleep(10.)
        self.rig.restart_moonraker()
        self.rig.reread(0)
        self.wait_reads_settled()
        with self.check('channel stays cleared after restart and read'):
            self.assertLinkStays(0, None, seconds=3.)
        self.rig.extrude([1.] * 8)
        with self.check('nothing charged to the old spool'):
            self.assertNoUsageFor(8., {'A': 4.})

    def test_B46_late_reply_to_a_selection_does_not_undo_clear(self):
        self.rig.create_spool('M')
        self.rig.load(0)
        self.rig.start_printer()
        self.wait_reads_settled()
        # Spoolman is slow to answer the selection of M.
        self.rig.proxy.add_rule('slow', 'GET', r'/api/v1/spool/%d' % self.rig.spools['M'],
                                'hold_after_commit', count=1)
        self.rig.select(0, self.rig.spools['M'])
        time.sleep(1.)
        self.rig.select(0, 0)
        time.sleep(1.)
        self.rig.proxy.release('slow')
        time.sleep(3.)
        with self.check('channel stays cleared after the late reply'):
            self.assertLinkStays(0, None, seconds=3.)
        self.rig.extrude([1.] * 5)
        with self.check('nothing charged'):
            self.assertNoUsageFor(10., {})


class Lookups(Case):
    """Spoolman is asked again only when needed."""

    def list_requests(self, start, end):
        return self.rig.lookup_requests(start, end)

    def test_B38_tag_read_during_outage_links_after_recovery(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.start_printer()
        self.rig.proxy.add_rule('errors', '*', '.*', 'status', status=503)
        self.rig.insert(0, TAG_A)
        self.wait_reads_settled()
        start = time.time()
        self.rig.extrude([.5] * 40, interval=.5)
        end = time.time()
        with self.check('spool list requests during the outage'):
            self.assertLessEqual(len(self.list_requests(start, end)), 10,
                                 'Spool list requests in 20 s of outage')
        self.rig.proxy.remove_rule('errors')
        with self.check('channel linked to A after recovery'):
            self.assertLink(0, 'A', timeout=30.)
        with self.check('all usage reaches A'):
            self.assertUsage({'A': 20.}, timeout=30.)

    def test_B45_late_lookup_of_a_removed_tag_does_not_apply(self):
        self.rig.create_spool('X', [TAG_A.uid])
        self.rig.create_spool('B', [TAG_B.uid])
        self.rig.start_printer()
        # Spoolman is slow to answer the lookup of the first tag.
        self.rig.proxy.add_rule('slow', 'GET', LIST, 'hold_after_commit', count=1)
        self.rig.insert(0, TAG_A)
        self.wait_reads_settled()
        time.sleep(1.)
        self.rig.remove(0)
        time.sleep(.5)
        self.rig.insert(0, TAG_B)
        self.wait_reads_settled()
        time.sleep(2.)
        self.rig.proxy.release('slow')
        time.sleep(3.)
        with self.check('channel on B after the late reply'):
            self.assertLinkStays(0, 'B', seconds=3.)
        self.rig.extrude([1.] * 5)
        with self.check('usage on B'):
            self.assertUsage({'B': 5.})

    def test_B48_reads_during_outage_keep_counting(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 2)
        with self.check('usage before the outage'):
            self.assertUsage({'A': 2.})
        self.rig.proxy.add_rule('errors', '*', '.*', 'status', status=503)
        join = self.background(lambda: self.rig.extrude([.5] * 40, interval=.1))
        for _ in range(3):
            time.sleep(1.)
            self.rig.reread(0)
        join()
        self.rig.proxy.remove_rule('errors')
        with self.check('channel linked to A'):
            self.assertLink(0, 'A')
        with self.check('usage on A'):
            self.assertUsage({'A': 22.}, timeout=30.)

    def test_B60_tag_change_during_outage_splits_usage(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.create_spool('B', [TAG_B.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 4)
        with self.check('usage before the outage'):
            self.assertUsage({'A': 4.})
        self.rig.proxy.add_rule('errors', '*', '.*', 'status', status=503)
        self.rig.extrude([1.] * 3)
        self.rig.remove(0)
        time.sleep(1.)
        self.rig.insert(0, TAG_B)
        self.wait_reads_settled()
        self.rig.extrude([1.] * 6)
        # A NAS restart takes minutes; 15 s outlasts any short retry of the lookup.
        time.sleep(15.)
        self.rig.proxy.remove_rule('errors')
        with self.check('usage after recovery'):
            self.assertUsage({'A': 7., 'B': 6.}, timeout=30.)
        with self.check('channel linked to B'):
            self.assertLink(0, 'B', timeout=30.)

    def test_B39_unregistered_tag_is_not_polled_constantly(self):
        self.rig.create_spool('N')
        self.rig.load(0, TAG_U)
        self.rig.start_printer()
        self.wait_reads_settled()
        self.rig.extrude([1.] * 5)
        time.sleep(5.)
        start = time.time()
        time.sleep(120.)
        requests = self.list_requests(start, time.time())
        with self.check('spool list requests in 120 s'):
            self.assertLessEqual(len(requests), 8, 'Spool list requests in 120 s')
        with self.check('polling interval grows'):
            gaps = [later - earlier for earlier, later in zip(requests, requests[1:])]
            for earlier, later in zip(gaps, gaps[1:]):
                self.assertGreaterEqual(later, earlier - .5, 'The polling interval got shorter')
            if len(gaps) >= 2:
                self.assertGreater(gaps[-1], gaps[0] + 1., 'The polling interval did not grow')
        self.rig.add_uid(self.rig.spools['N'], TAG_U.uid)
        with self.check('waiting usage reaches N'):
            self.assertUsage({'N': 5.}, timeout=10.)


if __name__ == '__main__':
    unittest.main()
