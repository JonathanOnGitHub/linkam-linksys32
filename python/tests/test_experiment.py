"""End-to-end tests using a simulated programmer and video source.

These validate the orchestration logic (threading, CSV output, video
writing, ramp sequencing) without any hardware.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from linkam.experiment import Experiment, IsothermalRun
from linkam.protocol import LinkamProgrammer, Telemetry, parse_telemetry
from linkam.video import Frame


class FakeProgrammer(LinkamProgrammer):
    """A programmer that simulates a ramp without any serial hardware."""

    def __init__(self, start_c=20.0, target_c=95.0, rate_c_per_min=600.0):
        super().__init__("/dev/null")
        self.commands: list[str] = []
        self.temp = start_c
        self.target = target_c
        self.rate = rate_c_per_min
        self.limit = None
        self.hold_s = 0.0
        self._t = 0.0

    # -- no serial I/O ---------------------------------------------------- #
    def open(self):
        self._state.connected = True

    def close(self):
        self._state.connected = False

    def send(self, command):
        """Parse the command grammar the real programmer understands."""
        self.commands.append(command)
        parts = command.split()
        low = command.lower()

        if low.startswith("prog ramp"):
            # compound: prog ramp <row> rate <r> limit <lim> time <t>
            # skip "prog ramp <row>" = 3 tokens, then read keyword/value pairs
            kv = {}
            for i in range(3, len(parts) - 1, 2):
                kv[parts[i].lower()] = parts[i + 1]
            if "rate" in kv:
                self.rate = float(kv["rate"])
            if "limit" in kv:
                self.limit = float(kv["limit"])
            if "time" in kv:
                self.hold_s = float(kv["time"])
        elif low.startswith("prog rate"):
            self.rate = float(parts[2])
        elif low.startswith("prog limit"):
            self.limit = float(parts[2])
        elif low.startswith("prog time"):
            self.hold_s = float(parts[2])
        elif low.startswith("prog start"):
            self._state.status = "heating"

    def _step(self):
        """Advance the simulated temperature by one telemetry tick."""
        self._t += 0.2977
        if self.limit is None:
            return
        target = self.limit
        rate_per_tick = self.rate / 60.0 * 0.2977
        if self.temp < target:
            self.temp = min(target, self.temp + rate_per_tick)
        elif self.temp > target:
            self.temp = max(target, self.temp - rate_per_tick)

    def wait_for_temperature(self, target_c, tolerance_c=0.5, timeout=3600.0):
        """Simulate the ramp, emitting telemetry as we go."""
        while abs(self.temp - target_c) > tolerance_c:
            self._step()
            tlm = parse_telemetry(
                f"Temp {self.temp:.2f} °C~Rate {self.rate:.1f} °C/min"
                f"~Limit {self.limit:.2f} °C"
            )
            self._absorb(tlm)
            for cb in list(self._listeners):
                cb(tlm)
        return self.temp


class FakeVideo:
    """A video source that yields blank frames."""

    def __init__(self, n=30, size=(64, 48)):
        self.n = n
        self.size = size
        self.index = 0
        self.opened = False
        self.closed = False

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True

    def read(self):
        if self.index >= self.n:
            return None
        import numpy as np

        img = np.zeros((self.size[1], self.size[0], 3), dtype=np.uint8)
        img[:, :, 0] = self.index % 255
        f = Frame(image=img, timestamp=0.0, index=self.index)
        self.index += 1
        return f


def test_experiment_runs_and_writes_outputs(tmp_path):
    prog = FakeProgrammer(start_c=20.0, target_c=95.0, rate_c_per_min=600.0)
    video = FakeVideo(n=40)

    exp = Experiment(
        target_c=95.0,
        rate_c_per_min=600.0,
        hold_s=0.0,
        hold_forever=False,
        output_dir=tmp_path / "run",
    )
    run = IsothermalRun(exp, programmer=prog, video=video)
    result = run.start()

    # the ramp was programmed in the documented order
    assert prog.commands == [
        "prog ramp 1 rate 600 limit 95 time 0",
        "Prog start",
    ]

    assert result.final_temperature_c == pytest.approx(95.0, abs=0.5)
    assert result.telemetry_samples > 0

    out = tmp_path / "run"
    assert (out / "telemetry.csv").exists()
    assert (out / "run.json").exists()
    assert (out / "capture.mp4").exists()

    # telemetry CSV must be parseable and monotonic in time
    lines = (out / "telemetry.csv").read_text().strip().splitlines()
    assert lines[0].startswith("timestamp,elapsed_s")
    assert len(lines) > 1
    elapsed = [float(l.split(",")[1]) for l in lines[1:]]
    assert elapsed == sorted(elapsed)


def test_hold_time_is_programmed(tmp_path):
    prog = FakeProgrammer(start_c=20.0, target_c=30.0, rate_c_per_min=600.0)
    video = FakeVideo(n=5)

    exp = Experiment(
        target_c=30.0,
        rate_c_per_min=600.0,
        hold_s=12.0,
        output_dir=tmp_path / "hold",
    )
    run = IsothermalRun(exp, programmer=prog, video=video)
    result = run.start()

    assert "prog ramp 1 rate 600 limit 30 time 12" in prog.commands
    assert "Prog stop" in prog.commands
    assert result.hold_s == 12.0


def test_video_is_closed_after_run(tmp_path):
    prog = FakeProgrammer(target_c=25.0, rate_c_per_min=600.0)
    video = FakeVideo(n=3)
    exp = Experiment(
        target_c=25.0,
        rate_c_per_min=600.0,
        hold_forever=False,
        output_dir=tmp_path / "c",
    )
    IsothermalRun(exp, programmer=prog, video=video).start()
    assert video.closed


def test_telemetry_roundtrip_preserves_values():
    text = "Temp 23.4 °C~Vac 0.1 mBar;Rate 10 °C/min~Limit 25.4 °C"
    tlm = parse_telemetry(text)
    assert isinstance(tlm, Telemetry)
    assert tlm.raw == text
    assert tlm.temperature == pytest.approx(23.4)
    assert tlm.rate == pytest.approx(10.0)
    assert tlm.limit == pytest.approx(25.4)
    assert tlm.vacuum == pytest.approx(0.1)
