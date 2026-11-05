"""Tests for the multi-step temperature programme engine.

No hardware is needed: a fake programmer stands in for the instrument
and simulates the ramp, so the sequencing, the chaining and the abort
path can all be exercised quickly and deterministically.
"""

from __future__ import annotations

import json

import pytest

from linkam.programme import (
    Programme,
    ProgrammeEvent,
    ProgrammeRunner,
    Step,
)


class FakeProgrammer:
    """A programmer that ramps instantly and records what it was told."""

    def __init__(self, *, refuse_to_ramp: bool = False) -> None:
        self.commands: list[tuple] = []
        self.ramps: list[dict] = []
        self.opened = False
        self.closed = False
        self.refuse_to_ramp = refuse_to_ramp
        self.temperature = 20.0
        self._listeners: list = []

    # -- lifecycle --------------------------------------------------------- #
    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def on_telemetry(self, callback) -> None:
        self._listeners.append(callback)

    # -- commands ---------------------------------------------------------- #
    def programme_ramp(self, row: int, rate: float, limit: float, time_s: float) -> None:
        self.ramps.append({"row": row, "rate": rate, "limit": limit, "time_s": time_s})
        self.commands.append(("programme_ramp", rate, limit, time_s))

    def start(self) -> None:
        self.commands.append(("start",))

    def stop(self) -> None:
        self.commands.append(("stop",))

    # -- waiting ----------------------------------------------------------- #
    def wait_for_temperature(
        self,
        target_c: float,
        tolerance_c: float = 0.5,
        timeout: float = 3600.0,
        *,
        stall_timeout=None,
        min_progress_c: float = 0.2,
    ) -> float:
        if self.refuse_to_ramp:
            raise RuntimeError("the instrument refused to ramp")
        self.temperature = target_c
        for cb in self._listeners:
            cb(_FakeTelemetry(target_c))
        return target_c

    def wait_for_stable_temperature(
        self,
        target_c: float,
        duration_s: float,
        *,
        tolerance_c: float = 0.5,
        settle_s: float = 1.0,
        timeout=None,
        poll_s: float = 0.1,
    ) -> float:
        self.commands.append(("hold", duration_s))
        return target_c


class _FakeTelemetry:
    def __init__(self, temperature: float) -> None:
        self.temperature = temperature
        self.received_at = 0.0


def make_runner(programmer, programme=None, **kwargs):
    """A runner with a no-op sleeper, so the tests do not wait in real time."""
    kwargs.setdefault("sleeper", lambda seconds: None)
    return ProgrammeRunner(programmer, programme or make_programme(), **kwargs)


def make_programme() -> Programme:
    return Programme(
        name="soak",
        steps=[
            Step(target_c=25.0, rate_c_per_min=5.0, hold_s=10.0, label="warm"),
            Step(target_c=60.0, rate_c_per_min=2.0, hold_s=30.0, label="soak"),
        ],
    )


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


def test_a_programme_needs_at_least_one_step():
    with pytest.raises(ValueError):
        Programme(steps=[])


def test_a_step_needs_a_positive_rate():
    with pytest.raises(ValueError):
        Step(target_c=25.0, rate_c_per_min=0.0)


def test_a_step_cannot_have_a_negative_hold():
    with pytest.raises(ValueError):
        Step(target_c=25.0, hold_s=-1.0)


def test_total_steps_counts_the_repeats():
    programme = Programme(steps=[Step(target_c=25.0), Step(target_c=60.0)], repeat=3)
    assert programme.total_steps == 6


def test_repeat_must_be_at_least_one():
    with pytest.raises(ValueError):
        Programme(steps=[Step(target_c=25.0)], repeat=0)


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #


def test_every_step_is_programmed_and_started():
    prog = FakeProgrammer()
    result = make_runner(prog, make_programme()).run()

    assert len(prog.ramps) == 2
    assert prog.ramps[0] == {"row": 1, "rate": 5.0, "limit": 25.0, "time_s": 10.0}
    assert prog.ramps[1] == {"row": 1, "rate": 2.0, "limit": 60.0, "time_s": 30.0}
    assert ("start",) in prog.commands
    assert result.reached_count == 2
    assert result.error is None


def test_the_programmer_is_opened():
    prog = FakeProgrammer()
    make_runner(prog, make_programme()).run()
    assert prog.opened


def test_the_steps_run_in_order():
    prog = FakeProgrammer()
    make_runner(prog, make_programme()).run()
    assert [r["limit"] for r in prog.ramps] == [25.0, 60.0]


def test_the_repeat_runs_the_whole_list_again():
    prog = FakeProgrammer()
    programme = Programme(steps=[Step(target_c=25.0), Step(target_c=60.0)], repeat=2)
    result = make_runner(prog, programme).run()
    assert len(prog.ramps) == 4
    assert len(result.steps) == 4


def test_the_results_carry_the_step_details():
    prog = FakeProgrammer()
    result = make_runner(prog, make_programme()).run()
    step = result.steps[1]
    assert step.index == 1
    assert step.label == "soak"
    assert step.target_c == 60.0
    assert step.rate_c_per_min == 2.0
    assert step.hold_s == 30.0
    assert step.reached is True
    assert step.ramp_seconds is not None


def test_a_refused_ramp_is_reported_not_raised():
    prog = FakeProgrammer(refuse_to_ramp=True)
    result = make_runner(prog, make_programme()).run()
    assert result.error is not None
    assert "refused to ramp" in result.error
    assert ("stop",) in prog.commands, "the programmer should be stopped"


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #


def test_events_are_emitted_for_each_phase():
    prog = FakeProgrammer()
    events: list[ProgrammeEvent] = []
    make_runner(prog, make_programme(), on_event=events.append).run()
    phases = [e.phase for e in events]
    assert phases[0] == "step_start"
    assert "reached" in phases
    assert "hold" in phases
    assert "step_end" in phases
    assert phases[-1] == "programme_end"


def test_events_carry_the_step_index():
    prog = FakeProgrammer()
    events: list[ProgrammeEvent] = []
    make_runner(prog, make_programme(), on_event=events.append).run()
    starts = [e for e in events if e.phase == "step_start"]
    assert [e.step_index for e in starts] == [0, 1]
    assert starts[0].step is not None
    assert starts[0].step.label == "warm"


def test_a_failing_callback_does_not_kill_the_run():
    prog = FakeProgrammer()

    def bad(event):
        raise RuntimeError("boom")

    result = make_runner(prog, make_programme(), on_event=bad).run()
    assert result.error is None
    assert result.reached_count == 2


# --------------------------------------------------------------------------- #
# abort
# --------------------------------------------------------------------------- #


def test_abort_stops_the_run():
    prog = FakeProgrammer()
    runner = make_runner(prog, make_programme())

    def abort_after_first(event):
        if event.phase == "step_end":
            runner.abort()

    runner.on_event = abort_after_first
    result = runner.run()
    assert result.aborted is True
    assert len(prog.ramps) == 1, "the second step must not be programmed"


def test_aborting_before_the_run_stops_immediately():
    prog = FakeProgrammer()
    runner = make_runner(prog, make_programme())
    runner.abort()
    result = runner.run()
    assert result.aborted is True
    assert prog.ramps == []


# --------------------------------------------------------------------------- #
# chaining
# --------------------------------------------------------------------------- #


def test_chaining_does_not_wait_for_the_hold_to_be_stable():
    """The next step is armed early, so the host does not sit in a hold.

    The first step's 10 s hold is chained over, so the host does not
    wait it out.  The *last* step has no successor to chain to, so its
    hold is observed in full.
    """
    prog = FakeProgrammer()
    make_runner(prog, make_programme(), chain_steps=True, chain_lead_s=2.0).run()
    assert ("hold", 10.0) not in prog.commands, (
        "a chained step must not wait for the host's view of the hold"
    )
    assert ("hold", 30.0) in prog.commands, (
        "the last step has no successor, so its hold is observed"
    )


def test_without_chaining_the_host_waits_for_the_hold():
    prog = FakeProgrammer()
    make_runner(prog, make_programme(), chain_steps=False).run()
    assert ("hold", 30.0) in prog.commands


def test_the_last_step_always_waits_out_its_hold():
    """There is no next step to chain to, so the hold is observed."""
    prog = FakeProgrammer()
    make_runner(prog, make_programme(), chain_steps=True).run()
    assert ("hold", 30.0) in prog.commands


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #


def test_the_results_are_written_out(tmp_path):
    prog = FakeProgrammer()
    make_runner(prog, make_programme(), output_dir=tmp_path).run()

    payload = json.loads((tmp_path / "programme.json").read_text())
    assert payload["programme"]["name"] == "soak"
    assert len(payload["programme"]["steps"]) == 2
    assert len(payload["result"]["steps"]) == 2

    csv_text = (tmp_path / "steps.csv").read_text()
    assert "warm" in csv_text
    assert "soak" in csv_text
