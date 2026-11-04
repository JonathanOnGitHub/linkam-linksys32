"""Tests for stall detection in wait_for_temperature.

The point of this feature is that a *refused* ramp (unlicensed instrument,
demonstration mode, OEM mode, zero rate) should surface as a clear error
quickly, rather than as a silent hang until the ramp timeout expires.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from linkam.protocol import (
    LinkamProgrammer,
    ProgrammerNotRamping,
    Telemetry,
    parse_telemetry,
)


class StaticProgrammer(LinkamProgrammer):
    """A programmer that reports a fixed temperature and never moves.

    This models the refused-ramp case: telemetry flows, the temperature is
    readable, but the instrument will not heat or cool.
    """

    def __init__(self, temp_c: float = 23.4, noise_c: float = 0.05):
        super().__init__("/dev/null")
        self.temp_c = temp_c
        self.noise_c = noise_c

    def open(self):
        self._state.connected = True

    def close(self):
        self._state.connected = False

    def send(self, command):
        pass

    def _tick(self):
        import random

        t = self.temp_c + random.uniform(-self.noise_c, self.noise_c)
        tlm = parse_telemetry(f"Temp {t:.2f} °C~Rate 10.0 °C/min")
        self._absorb(tlm)


class SlowProgrammer(StaticProgrammer):
    """A programmer that ramps, but only slowly.

    The temperature advances at the *reported* rate, so the simulation is
    consistent with what the instrument claims to be doing.  This matters:
    the stall detector compares observed movement against the reported
    rate, so a simulation that runs faster than it claims would be
    self-contradictory.
    """

    def __init__(self, start_c: float = 20.0, rate_c_per_min: float = 60.0):
        super().__init__(temp_c=start_c, noise_c=0.0)
        self.rate = rate_c_per_min
        self.target = 40.0
        self._last = time.monotonic()

    def _tick(self):
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self.temp_c = min(
            self.target, self.temp_c + self.rate / 60.0 * elapsed
        )
        tlm = parse_telemetry(f"Temp {self.temp_c:.2f} °C~Rate {self.rate} °C/min")
        self._absorb(tlm)


def _pump(prog, seconds, step=0.05):
    """Drive a programmer's telemetry for a while, as its reader thread would."""
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        prog._tick()
        time.sleep(step)


class TestStallDetection:
    def test_static_programmer_raises_rather_than_hanging(self):
        p = StaticProgrammer(temp_c=23.4)

        # Feed telemetry in the background, as the reader thread would.
        import threading

        stop = threading.Event()

        def feeder():
            while not stop.is_set():
                p._tick()
                stop.wait(0.05)

        t = threading.Thread(target=feeder, daemon=True)
        t.start()
        try:
            with pytest.raises(ProgrammerNotRamping) as exc:
                p.wait_for_temperature(
                    95.0, timeout=30.0, stall_timeout=0.5, min_progress_c=0.2
                )
        finally:
            stop.set()
            t.join(timeout=1.0)

        msg = str(exc.value)
        assert "not moving" in msg
        assert "95.00" in msg
        assert "23." in msg  # the temperature it was actually reporting

    def test_slow_but_real_ramp_does_not_trip_the_stall_detector(self):
        """A genuine slow ramp must not be mistaken for a refused one.

        This is the case that a naive absolute-threshold implementation
        gets wrong: at a slow rate the temperature barely moves, so the
        detector must judge progress against the *reported* rate.
        """
        p = SlowProgrammer(start_c=20.0, rate_c_per_min=10.0)

        import threading

        stop = threading.Event()

        def feeder():
            while not stop.is_set():
                p._tick()
                stop.wait(0.05)

        t = threading.Thread(target=feeder, daemon=True)
        t.start()
        try:
            # 20 C at 10 C/min takes 2 minutes of simulated time, which is
            # far longer than we want a test to run.  The point of this test
            # is only that the stall detector does not fire, so we use a
            # short target and a generous stall window.
            p.wait_for_temperature(
                21.0, tolerance_c=0.05, timeout=30.0,
                stall_timeout=3.0, min_progress_c=0.2,
            )
        except TimeoutError:
            pass
        finally:
            stop.set()
            t.join(timeout=1.0)

        # The important assertion: it did not raise ProgrammerNotRamping,
        # and the temperature did move toward the target.
        assert p.temp_c > 20.05

    def test_very_slow_ramp_is_not_mistaken_for_a_stall(self):
        """Even a 1 C/min ramp (0.017 C/s) must be tolerated.

        The stall window is 2 s, during which such a ramp closes the gap
        by only ~0.03 C — less than `min_progress_c`.  Only the rate-aware
        test can tell this apart from a frozen instrument.
        """
        p = SlowProgrammer(start_c=20.0, rate_c_per_min=1.0)

        import threading

        stop = threading.Event()

        def feeder():
            while not stop.is_set():
                p._tick()
                stop.wait(0.05)

        t = threading.Thread(target=feeder, daemon=True)
        t.start()
        try:
            # Target is reachable but we only need to prove it does not
            # raise ProgrammerNotRamping; give it a generous timeout.
            p.wait_for_temperature(
                20.2, tolerance_c=0.05, timeout=30.0,
                stall_timeout=2.0, min_progress_c=0.2,
            )
        except TimeoutError:
            # Reaching it exactly is not the point; not raising
            # ProgrammerNotRamping is.
            pass
        finally:
            stop.set()
            t.join(timeout=1.0)

    def test_stall_detection_is_off_by_default(self):
        """Without stall_timeout the old behaviour must be unchanged."""
        p = StaticProgrammer(temp_c=23.4)
        with pytest.raises(TimeoutError):
            p.wait_for_temperature(95.0, timeout=0.5)


class TestTimeoutStillWorks:
    def test_timeout_is_raised_when_the_target_is_never_reached(self):
        """A moving-but-too-slow ramp should still time out, not stall."""

        class CrawlingProgrammer(StaticProgrammer):
            def _tick(self):
                # Moves a little each tick, but never enough to finish.
                self.temp_c += 0.001
                tlm = parse_telemetry(f"Temp {self.temp_c:.2f} °C")
                self._absorb(tlm)

        p = CrawlingProgrammer(temp_c=20.0)

        import threading

        stop = threading.Event()

        def feeder():
            while not stop.is_set():
                p._tick()
                stop.wait(0.05)

        t = threading.Thread(target=feeder, daemon=True)
        t.start()
        try:
            with pytest.raises(TimeoutError):
                p.wait_for_temperature(
                    95.0, timeout=1.0, stall_timeout=None
                )
        finally:
            stop.set()
            t.join(timeout=1.0)


class TestPackageExports:
    def test_exception_is_exported(self):
        import linkam

        assert linkam.ProgrammerNotRamping is ProgrammerNotRamping
        assert "ProgrammerNotRamping" in linkam.__all__
