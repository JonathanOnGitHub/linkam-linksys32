"""Tests for the hold semantics.

The programmer runs the hold itself: the compound command
``prog ramp <row> rate <r> limit <l> time <t>`` tells it to hold for
``t`` seconds once it reaches the limit.  The host must therefore never
simply sleep for ``t`` seconds and stop -- that double-counts the hold
and, worse, can cut the real hold short.

These tests exercise the host-side hold detection.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from linkam.protocol import LinkamProgrammer, Telemetry


class FakeProgrammer(LinkamProgrammer):
    """A programmer whose temperature is driven by the test."""

    def __init__(self):
        super().__init__("/dev/null")
        self._state.connected = True

    def feed(self, temperature: float, rate: float | None = None) -> None:
        self._state.temperature = temperature
        if rate is not None:
            self._state.rate = rate


def test_stable_temperature_returns_after_the_duration():
    p = FakeProgrammer()
    p.feed(25.0)
    t0 = time.monotonic()
    p.wait_for_stable_temperature(25.0, duration_s=1.0, settle_s=0.2)
    elapsed = time.monotonic() - t0
    assert 1.0 <= elapsed < 3.0


def test_stable_temperature_waits_for_the_temperature_to_settle():
    """A temperature still ramping through the band must not start the clock.

    Regression test: a temperature that is still rising towards the
    target passes through the tolerance band on its way.  If the hold
    clock started as soon as the temperature entered the band, the whole
    hold could be consumed by the ramp tail.
    """
    p = FakeProgrammer()
    p.feed(24.0)  # inside a 0.5 C band around 25.0, but still moving

    started = time.monotonic()

    def ramp():
        # Rise through the band over 2 s, then hold.
        for i in range(20):
            p.feed(24.0 + i * 0.05)
            time.sleep(0.1)
        for _ in range(100):
            p.feed(25.0)
            time.sleep(0.05)

    import threading

    t = threading.Thread(target=ramp, daemon=True)
    t.start()

    p.wait_for_stable_temperature(25.0, duration_s=1.0, settle_s=0.3)
    elapsed = time.monotonic() - started

    # The hold must start *after* the temperature has settled, so the
    # total must be at least the 2 s ramp plus the 1 s hold.
    assert elapsed >= 2.0, f"hold started during the ramp ({elapsed:.2f}s)"


def test_stable_temperature_resets_when_the_temperature_drifts_out():
    p = FakeProgrammer()
    p.feed(25.0)

    import threading

    def wobble():
        time.sleep(0.5)
        p.feed(30.0)   # out of the band
        time.sleep(0.5)
        p.feed(25.0)   # back in

    t = threading.Thread(target=wobble, daemon=True)
    t.start()

    started = time.monotonic()
    p.wait_for_stable_temperature(25.0, duration_s=1.0, settle_s=0.2)
    elapsed = time.monotonic() - started
    # The 1 s hold cannot have completed before the excursion at 0.5 s.
    assert elapsed >= 1.0


def test_stable_temperature_times_out_when_never_stable():
    p = FakeProgrammer()
    p.feed(10.0)   # never near the target
    with pytest.raises(TimeoutError):
        p.wait_for_stable_temperature(
            25.0, duration_s=0.2, settle_s=0.1, timeout=0.5
        )


def test_stable_temperature_without_any_reading_times_out():
    p = FakeProgrammer()
    # No temperature ever arrives.
    with pytest.raises(TimeoutError):
        p.wait_for_stable_temperature(
            25.0, duration_s=0.2, settle_s=0.1, timeout=0.4
        )


def test_hold_uses_the_reported_rate_to_detect_the_plateau():
    """A non-zero reported rate means the programmer is still ramping.

    This is the primary signal used to tell "reached the limit" from
    "still on its way there".  It has to work even for a very slow ramp,
    where the temperature barely moves during the settling window.
    """
    p = FakeProgrammer()
    p.feed(24.9, rate=0.5)     # inside the band, but still ramping slowly

    import threading

    def ramp():
        # Creep upwards for 2 s, then stop ramping.
        for i in range(20):
            p.feed(24.9 + i * 0.001, rate=0.5)
            time.sleep(0.1)
        p.feed(25.0, rate=0.0)  # at the limit, holding

    t = threading.Thread(target=ramp, daemon=True)
    t.start()

    started = time.monotonic()
    p.wait_for_stable_temperature(25.0, duration_s=0.5, settle_s=0.3)
    elapsed = time.monotonic() - started

    # A drift-only detector would have started the hold immediately,
    # because the temperature barely moved.  The rate signal must stop
    # that, so the hold cannot have completed before the ramp ended.
    assert elapsed >= 2.0, f"hold started during the ramp ({elapsed:.2f}s)"


class FakeT95Programmer(FakeProgrammer):
    """A programmer that behaves like the Linkam T95.

    The T95 does not report its programme at all, so the host never
    sees a rate of zero: the rate it sees is the one it programmed,
    whatever the instrument is doing.  So "the reported rate is zero, so
    it is holding" does not work on this link.
    """

    REPORTS_ZERO_RATE_WHILE_HOLDING = False


def test_settle_is_judged_from_the_temperature_when_the_rate_is_always_reported():
    """Regression test for a programme that hung on the T95.

    The rate stays at the programmed 2 C/min forever, so a settle
    detection that reads "rate is zero means holding" decides the
    temperature is moving forever and the hold never completes.
    """
    p = FakeT95Programmer()
    p.feed(25.0, rate=2.0)

    import threading

    def flatten():
        # Creep for a moment, then hold.
        for i in range(10):
            p.feed(25.0 + i * 0.001, rate=2.0)
            time.sleep(0.05)
        for _ in range(200):
            p.feed(25.0, rate=2.0)
            time.sleep(0.05)

    t = threading.Thread(target=flatten, daemon=True)
    t.start()

    started = time.monotonic()
    p.wait_for_stable_temperature(25.0, duration_s=0.5, settle_s=0.3)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"the hold never completed ({elapsed:.2f}s)"


def test_a_genuinely_moving_temperature_is_still_detected_on_the_t95():
    """The slope test must not settle a temperature that is still ramping.

    The counter-test to the one above: if the settle decision ignored
    the temperature, it would report a plateau while the stage was
    still climbing.
    """
    p = FakeT95Programmer()
    p.feed(24.9, rate=2.0)

    import threading

    def ramp():
        # Climb at the programmed rate for 2 s, then hold.
        for i in range(20):
            p.feed(24.9 + i * 0.0033, rate=2.0)
            time.sleep(0.1)
        p.feed(25.0, rate=2.0)

    t = threading.Thread(target=ramp, daemon=True)
    t.start()

    started = time.monotonic()
    p.wait_for_stable_temperature(25.0, duration_s=0.5, settle_s=0.3)
    elapsed = time.monotonic() - started

    # The hold must not have started while the stage was still climbing.
    assert elapsed >= 1.5, f"hold started during the ramp ({elapsed:.2f}s)"


def test_max_hold_drift_is_small():
    """The fallback drift threshold must be tight.

    It is only used when the programmer does not report a rate, so it
    has to be small enough that a slow ramp is still detectable.
    """
    p = LinkamProgrammer("/dev/null")
    assert p.MAX_HOLD_DRIFT_C <= 0.05
