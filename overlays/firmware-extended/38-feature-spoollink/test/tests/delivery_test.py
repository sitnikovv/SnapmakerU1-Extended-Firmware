# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @sitnikovv

"""Delivery: every millimetre reaches Spoolman exactly once."""

import time
import unittest

from base import Case, TAG_A, TAG_B

USE = r'/api/v1/spool/\d+/use'


class Linked(Case):
    def linked(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.load(0, TAG_A)
        self.rig.start_printer()
        with self.step('channel linked to A'):
            self.assertLink(0, 'A')


class Delivery(Linked):
    def test_B08_restart_after_retraction_does_not_count_the_return(self):
        self.linked()
        self.feed_then_retract([1.] * 10, -2.5)
        with self.check('usage before restart'):
            self.assertUsage({'A': 10.})
        self.rig.print_state('paused')
        self.rig.restart_moonraker()
        self.rig.extrude([2.5, 1., 1., 1., 1.])
        with self.check('usage after restart'):
            self.assertUsage({'A': 14.})

    def test_B09_graceful_restart_keeps_unsent_usage(self):
        self.linked()
        # Spoolman refuses the usage, so it is still unsent at the restart.
        self.rig.proxy.add_rule('hold', 'PUT', USE, 'status', status=503)
        self.rig.extrude([1.] * 10)
        time.sleep(6.)
        self.rig.restart_moonraker()
        self.rig.proxy.remove_rule('hold')
        self.rig.extrude([1.] * 3)
        with self.check('usage after restart'):
            self.assertUsage({'A': 13.})

    def test_B10_killed_moonraker_keeps_unsent_usage(self):
        self.linked()
        self.rig.proxy.add_rule('hold', 'PUT', USE, 'status', status=503)
        self.rig.extrude([1.] * 10)
        time.sleep(6.)
        self.rig.restart_moonraker(kill=True)
        self.rig.proxy.remove_rule('hold')
        self.rig.extrude([1.] * 3)
        with self.check('usage after restart'):
            self.assertUsage({'A': 13.})

    def test_B11_kill_during_applied_request_does_not_charge_twice(self):
        self.linked()
        # Spoolman saves the usage, but the reply waits until Moonraker is killed.
        self.rig.proxy.add_rule('reply', 'PUT', USE, 'hold_after_commit', count=1)
        self.rig.extrude([1.] * 10)
        with self.step('server applied the first request'):
            self.wait_until(lambda: any(row.get('status') == 200
                                        for row in self.rig.proxy.requests('PUT', USE)),
                            15., 'The usage request did not reach Spoolman')
            self.assertEqual(self.rig.used()['A'], 10.)
        self.rig.kill_moonraker()
        self.rig.proxy.release('reply')
        self.rig.start_moonraker()
        self.rig.extrude([1.] * 2)
        with self.check('usage after restart'):
            self.assertUsage({'A': 12.}, stable=12.)

    def test_B12_lost_reply_does_not_charge_twice(self):
        self.linked()
        # Spoolman saves the usage, then the connection drops before the reply.
        self.rig.proxy.add_rule('drop', 'PUT', USE, 'drop_after_commit', count=1)
        self.rig.extrude([1.] * 10)
        with self.check('usage after lost reply'):
            self.assertUsage({'A': 10.}, stable=12.)
        self.rig.extrude([1.] * 3)
        with self.check('usage after more feed'):
            self.assertUsage({'A': 13.})


class MoonrakerRestarts(Linked):
    def test_B16_feed_while_moonraker_is_down_is_counted(self):
        self.linked()
        self.rig.extrude([1.] * 3)
        with self.check('usage before Moonraker stops'):
            self.assertUsage({'A': 3.})
        self.rig.kill_moonraker()
        # Klipper keeps printing: 7 mm forward and a 2 mm retraction.
        self.rig.extrude([1.] * 7 + [-2.])
        self.rig.start_moonraker()
        self.rig.extrude([2., 1., 1.])
        with self.check('usage after Moonraker starts'):
            self.assertUsage({'A': 12.})

    def test_B55_failed_spool_does_not_replay_the_delivered_one(self):
        self.rig.create_spool('A', [TAG_A.uid])
        self.rig.create_spool('B', [TAG_B.uid])
        self.rig.load(0, TAG_A)
        self.rig.load(1, TAG_B)
        self.rig.start_printer()
        with self.step('channels linked to A and B'):
            self.assertLink(0, 'A')
            self.assertLink(1, 'B')
        self.rig.proxy.add_rule('B fails', 'PUT', r'/api/v1/spool/%d/use' % self.rig.spools['B'],
                                'status', status=503)
        self.rig.extrude([1.] * 4)
        self.rig.activate(1)
        self.rig.extrude([1.] * 6)
        with self.check('usage while B fails'):
            self.assertUsage({'A': 4., 'B': 0.})
        self.rig.restart_moonraker(kill=True)
        self.rig.proxy.remove_rule('B fails')
        with self.check('usage after restart'):
            self.assertUsage({'A': 4., 'B': 6.}, stable=12.)

    def test_B56_new_spoolman_server_gets_nothing_from_the_old_one(self):
        self.linked()
        # The usage is still unsent when the user moves to another server.
        self.rig.proxy.add_rule('hold', 'PUT', USE, 'status', status=503)
        self.rig.extrude([1.] * 5)
        time.sleep(6.)
        self.rig.shutdown_moonraker()
        self.rig.switch_spoolman()
        # The first spool on the new server gets the number A had on the old one.
        self.rig.create_spool('Q')
        with self.step('Q has the number of A'):
            self.assertEqual(self.rig.spools['Q'], self.rig.retired[0]['spools']['A'])
        self.rig.start_moonraker()
        with self.check('channel unlinked'):
            self.assertLink(0, None)
        self.rig.extrude([1.] * 3)
        with self.check('nothing charged on the new server'):
            self.assertNoUsageFor(12., {})

    def test_B57_killed_moonraker_after_retraction_does_not_count_the_return(self):
        self.linked()
        self.feed_then_retract([1.] * 10, -2.5)
        with self.check('usage before restart'):
            self.assertUsage({'A': 10.})
        self.rig.restart_moonraker(kill=True)
        self.rig.extrude([2.5, 1., 1., 1., 1.])
        with self.check('usage after restart'):
            self.assertUsage({'A': 14.})

    def test_B58_feed_before_moonraker_connects_to_a_new_klipper(self):
        self.linked()
        self.rig.extrude([1.] * 3)
        with self.check('usage before restart'):
            self.assertUsage({'A': 3.})
        self.rig.shutdown()
        self.rig.start_klipper()
        # Klipper prints before Moonraker is up again.
        self.rig.extrude([1.] * 5)
        self.rig.start_moonraker()
        self.wait_reads_settled()
        with self.check('channel linked to A'):
            self.assertLink(0, 'A')
        self.rig.extrude([1.] * 2)
        with self.check('usage after restart'):
            self.assertUsage({'A': 10.})


class PlainRequests(Linked):
    def test_B71_server_that_claims_numbered_operations_gets_plain_requests(self):
        """Usage requests carry only the length, whatever the server reports.

        No Spoolman release has numbered usage operations, so the firmware
        never sends an operation ID. Here the proxy adds the flag of that
        feature to every /api/v1/info answer. Spoolman ignores fields it does
        not know, so a request with an ID is applied like a plain one, and a
        retry that relies on the ID records the usage again.
        """
        self.rig.proxy.add_rule('claim', 'GET', r'/api/v1/info', 'json_merge',
                                merge={'usage_operation_ids': True})
        self.linked()
        self.rig.proxy.add_rule('drop', 'PUT', USE, 'drop_after_commit', count=1)
        self.rig.extrude([1.] * 10)
        with self.check('usage after lost reply'):
            self.assertUsage({'A': 10.}, stable=12.)
        self.rig.extrude([1.] * 3)
        with self.check('usage after more feed'):
            self.assertUsage({'A': 13.})
        with self.check('plain usage requests'):
            bodies = [row['body'] for row in self.rig.proxy.requests('PUT', USE)]
            self.assertTrue(bodies, 'No usage request reached the proxy')
            self.assertEqual([sorted(body) for body in bodies], [['use_length']] * len(bodies))


class Outages(Linked):
    def test_B14_spoolman_down_for_twenty_seconds(self):
        self.linked()
        self.rig.extrude([1.] * 5)
        with self.check('usage before the outage'):
            self.assertUsage({'A': 5.})
        self.rig.proxy.refuse()
        self.rig.extrude([1.] * 10)
        time.sleep(20.)
        self.rig.proxy.accept()
        with self.check('usage after the outage'):
            self.assertUsage({'A': 15.}, timeout=40.)

    def test_B15_spoolman_errors_after_moonraker_restart(self):
        self.linked()
        self.rig.extrude([1.] * 5)
        with self.check('usage before the errors'):
            self.assertUsage({'A': 5.})
        # Every HTTP request gets 503 while the websocket stays open.
        self.rig.proxy.add_rule('errors', '*', '.*', 'status', status=503)
        self.rig.restart_moonraker()
        self.rig.extrude([1.] * 10)
        time.sleep(12.)
        self.rig.proxy.remove_rule('errors')
        with self.check('usage after the errors'):
            self.assertUsage({'A': 15.}, timeout=30.)


if __name__ == '__main__':
    unittest.main()
