"""Tests for the software simulator of the Linkam programmer.

The simulator exists so the whole workflow can be rehearsed without
hardware, and so that the wire format we *believe* the hardware uses is
exercised by the test suite.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from linkam.simulator import SimulatedProgrammer


def test_simulator_parses_compound_ramp_command():
    p = SimulatedProgrammer()
    p.send("prog ramp 1 rate 2.5 limit 95 time 60")
    assert p.ramp_row == 1
    assert p.rate == pytest.approx(2.5)
    assert p.limit == pytest.approx(95.0)
    assert p.hold_s == pytest.approx(60.0)


def test_simulator_parses_single_value_commands():
    p = SimulatedProgrammer()
    p.send("prog rate 10")
    assert p.rate == pytest.approx(10.0)
    p.send("prog limit 120")
    assert p.limit == pytest.approx(120.0)
    p.send("prog time 30")
    assert p.hold_s == pytest.approx(30.0)


def test_simulator_ramps_towards_limit():
    p = SimulatedProgrammer(start_c=20.0, ambient_c=20.0)
    p.open()
    try:
        p.programme_ramp(row=1, rate=600.0, limit=95.0, time_s=0)
        p.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if p.temp_c >= 94.5:
                break
            time.sleep(0.05)
        assert p.temp_c >= 94.5
        assert p.temp_c <= 95.5
    finally:
        p.close()


def test_simulator_emits_telemetry_in_wire_format():
    p = SimulatedProgrammer(start_c=42.0)
    seen = []
    p.on_telemetry(seen.append)
    p.open()
    try:
        p.programme_ramp(row=1, rate=60.0, limit=50.0, time_s=0)
        p.start()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(seen) < 3:
            time.sleep(0.05)
    finally:
        p.close()

    assert len(seen) >= 3
    tlm = seen[0]
    assert "Temp" in tlm.raw
    assert "~" in tlm.raw
    assert "°C" in tlm.raw
    assert tlm.temperature == pytest.approx(42.0, abs=0.5)


def test_simulator_telemetry_cadence_matches_hardware():
    """The real hardware reports every 0.2977 s."""
    p = SimulatedProgrammer(start_c=42.0)
    seen = []
    p.on_telemetry(seen.append)
    p.open()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(seen) < 4:
            time.sleep(0.02)
    finally:
        p.close()

    assert len(seen) >= 4
    gaps = [
        seen[i + 1].received_at - seen[i].received_at for i in range(len(seen) - 1)
    ]
    for g in gaps:
        assert 0.2 < g < 0.45


def test_simulator_stop_halts_ramp():
    p = SimulatedProgrammer(start_c=20.0, ambient_c=20.0)
    p.open()
    try:
        p.programme_ramp(row=1, rate=600.0, limit=95.0, time_s=0)
        p.start()
        time.sleep(0.5)
        p.stop()
        assert p.running is False
        t_at_stop = p.temp_c
        time.sleep(0.5)
        # with the programmer stopped the stage drifts back to ambient
        assert p.temp_c < t_at_stop
    finally:
        p.close()


def test_simulator_enters_hold_when_the_limit_is_reached():
    """The ramp must transition to holding once the limit is reached.

    Regression test: the transition to "holding" used to be tested only
    in the "standby" state, so once the mode became "heating" the
    branch was never taken again.  The ramp would sit at the limit
    forever and the hold would never start.
    """
    p = SimulatedProgrammer(start_c=20.0, ambient_c=20.0, noise_c=0.0)
    p.open()
    try:
        p.programme_ramp(row=1, rate=600.0, limit=25.0, time_s=2.0)
        p.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if p.mode == "holding":
                break
            time.sleep(0.02)
        assert p.mode == "holding", f"never entered holding (mode={p.mode})"
        assert p.hold_remaining > 0
    finally:
        p.close()


def test_simulator_hold_counts_down_and_ends():
    """The hold must count down and then stop, exactly once."""
    p = SimulatedProgrammer(start_c=20.0, ambient_c=20.0, noise_c=0.0)
    p.open()
    try:
        p.programme_ramp(row=1, rate=600.0, limit=25.0, time_s=1.0)
        p.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and p.mode != "holding":
            time.sleep(0.02)
        assert p.mode == "holding"

        # The hold should finish and the mode should settle at standby.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and p.mode == "holding":
            time.sleep(0.02)
        assert p.mode == "standby"

        # And it must not start again.
        time.sleep(1.0)
        assert p.mode == "standby"
        assert p.hold_remaining == 0.0
    finally:
        p.close()


def test_simulator_hold_reports_the_temperature_as_stable():
    """During a hold the temperature must stop changing."""
    p = SimulatedProgrammer(start_c=20.0, ambient_c=20.0, noise_c=0.0)
    p.open()
    try:
        p.programme_ramp(row=1, rate=600.0, limit=25.0, time_s=3.0)
        p.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and p.mode != "holding":
            time.sleep(0.02)
        assert p.mode == "holding"

        t_hold = p.temp_c
        time.sleep(1.0)
        assert abs(p.temp_c - t_hold) < 0.01
    finally:
        p.close()


def test_simulator_prog_hold_command_forces_a_hold():
    """``Prog Hold`` forces the current temperature to be held."""
    p = SimulatedProgrammer(start_c=42.0, ambient_c=20.0, noise_c=0.0)
    p.open()
    try:
        p.send("prog time 5")
        p.hold()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and p.mode != "holding":
            time.sleep(0.02)
        assert p.mode == "holding"
        t_hold = p.temp_c
        time.sleep(0.8)
        assert abs(p.temp_c - t_hold) < 0.05
    finally:
        p.close()
