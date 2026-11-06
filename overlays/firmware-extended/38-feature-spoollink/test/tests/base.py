"""Shared helpers for the behavior tests.

Each test is a list of named checks. A check made with `check` records a
failure and lets the test go on, so one run shows every broken check. A check
made with `step` stops the test, because the next steps make no sense without
it. The runner reports the names of all failed checks.
"""

import contextlib
import re
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'stand'))

from rig import Rig, Tag, StandError  # noqa: E402

TOLERANCE = 0.000001
STABLE_SECONDS = 7.
TAG_A = Tag('A1A2A3A4')
TAG_B = Tag('B1B2B3B4')
TAG_U = Tag('C1C2C3C4')
TAG_V = Tag('D1D2D3D4')
TAG_A_PARSED = Tag('A5A6A7A8', parsed=True)


def number(value):
    """A pattern for a spool number that does not match E1..E4 or longer numbers."""
    return r'(?<!\w)%d(?!\d)' % value


class Case(unittest.TestCase):
    rig_options = {}

    def setUp(self):
        name = self.id().rsplit('.', 1)[-1]
        self.rig = Rig(name, **self.rig_options)
        self.addCleanup(self.rig.close)
        self.addCleanup(self._check_screen)
        self.rig.start_spoolman()

    def _check_screen(self):
        if self.rig.moonraker is not None and self.rig.klippy is not None:
            errors = self.rig.screen_errors()
            self.assertEqual(errors, [], '[screen] An error appeared on the printer screen')

    # -- named checks ----------------------------------------------------

    @contextlib.contextmanager
    def check(self, name):
        """A named check. A failure is recorded and the test continues."""
        with self.subTest(check=name):
            yield

    @contextlib.contextmanager
    def step(self, name):
        """A named check that the rest of the test needs. A failure stops the test."""
        try:
            yield
        except self.failureException as error:
            raise self.failureException('[%s] %s' % (name, error)) from None

    def background(self, action):
        """Run `action` in a thread. Returns a function that waits for it."""
        errors = []

        def run():
            try:
                action()
            except Exception as error:  # reported by the join function
                errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def join(timeout=120.):
            thread.join(timeout)
            if thread.is_alive():
                raise StandError('A background action did not finish')
            if errors:
                raise errors[0]
        return join

    def wait_until(self, condition, timeout, message):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail(message)
            time.sleep(.5)

    # -- assertions ------------------------------------------------------

    def _usage_matches(self, expected):
        actual = self.rig.used()
        for name in self.rig.spools:
            want = expected.get(name, 0.)
            have = actual[name]
            if want is None or have is None:
                if want != have:
                    return False, actual
            elif abs(have - want) > TOLERANCE:
                return False, actual
        return True, actual

    def assertUsage(self, expected, timeout=30., stable=STABLE_SECONDS):
        """Every spool reaches its expected used length and stays there.

        Spools missing from `expected` must have no usage.  `None` means the
        spool must not exist.
        """
        deadline = time.monotonic() + timeout
        while True:
            matched, actual = self._usage_matches(expected)
            if matched:
                break
            if time.monotonic() > deadline:
                self.fail('Usage %s, expected %s' % (self._format(actual), self._format(expected)))
            time.sleep(.25)
        deadline = time.monotonic() + stable
        while time.monotonic() < deadline:
            matched, actual = self._usage_matches(expected)
            if not matched:
                self.fail('Usage changed to %s after reaching %s'
                          % (self._format(actual), self._format(expected)))
            time.sleep(.5)

    def assertNoUsageFor(self, seconds, expected):
        """The usage stays at `expected` for the whole period."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            matched, actual = self._usage_matches(expected)
            if not matched:
                self.fail('Usage %s, expected %s' % (self._format(actual), self._format(expected)))
            time.sleep(.5)

    def _format(self, values):
        return ', '.join('%s=%s' % (name, 'deleted' if value is None else '%.6f' % value)
                         for name, value in sorted(values.items()))

    def spool_number(self, name):
        return self.rig.spools[name] if name else 0

    def assertLink(self, channel, name, timeout=15.):
        """The channel shows the spool on the screen; `None` means no spool.

        After the screen shows the expected link, the check waits one more
        second before the test prints on. A person needs at least that long
        to see the change and resume printing. Moonraker learns about a new
        link from the next Klipper status update, about 0.25 s later, and the
        original firmware charges the active spool that Moonraker knows.
        """
        wanted = self.spool_number(name)
        deadline = time.monotonic() + timeout
        while True:
            current = self.rig.links()[channel]
            if (current or 0) == wanted:
                time.sleep(1.)
                return
            if time.monotonic() > deadline:
                self.fail('Channel %d shows spool %s, expected %s' % (channel, current, wanted))
            time.sleep(.2)

    def assertLinkStays(self, channel, name, seconds=3.):
        wanted = self.spool_number(name)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            current = self.rig.links()[channel]
            if (current or 0) != wanted:
                self.fail('Channel %d shows spool %s, expected %s' % (channel, current, wanted))
            time.sleep(.2)

    def messages_since(self, count):
        return self.rig.responses()[count:]

    def assertMessage(self, since, channel, pattern, timeout=15.):
        """A console message about the channel appears after `since` lines.

        The message is a `!!` or `//` line that names the channel as E1..E4.
        """
        label = re.compile(r'\bE%d\b' % (channel + 1))
        expression = re.compile(pattern, re.IGNORECASE)
        deadline = time.monotonic() + timeout
        while True:
            lines = self.messages_since(since)
            for line in lines:
                if line.startswith(('!!', '//')) and label.search(line) and expression.search(line):
                    return line
            if time.monotonic() > deadline:
                self.fail('No console message for E%d matching %r; got %r'
                          % (channel + 1, pattern, lines))
            time.sleep(.2)

    def feed_then_retract(self, forward, retract):
        """Feed forward, wait one second, then retract.

        The original firmware reads the E position from Klipper status
        updates and misses a peak that ends before the next update. The pause
        lets every firmware see the peak, so the test checks only how the
        retraction is handled.
        """
        self.rig.extrude(forward)
        time.sleep(1.)
        self.rig.extrude([retract])

    def wait_reads_settled(self, timeout=15.):
        """Every channel finished its RFID detection."""
        deadline = time.monotonic() + timeout
        while True:
            states = self.rig.query({'filament_detect': ['state']})['filament_detect']['state']
            if not any(states):
                return
            if time.monotonic() > deadline:
                self.fail('RFID detection did not finish: %s' % states)
            time.sleep(.1)
