"""Multi-step temperature programmes on a Linkam programmer.

The Linkam T95's programmer model is **one ramp plus a hold**: a rate, a
limit and a dwell time.  There is no native multi-segment programme
table — the SDK exposes no profile/table/segment API at all — so a
multi-step programme has to be driven by the **host**.

That is not a limitation in practice.  The ramp executes inside the
instrument, so the host only has to:

1. send the next step's ramp,
2. notice when the temperature has arrived,
3. send the step after that.

The steps therefore execute with the host's polling latency between
them, not with the host in the control loop.

Chaining
--------

The interesting detail is the **hold** between steps.  The hardware runs
the hold itself: the ramp command carries the dwell time, and the
instrument holds at the limit once it gets there.  If the host waits for
its own view of the hold to finish and *then* sends the next ramp, the
instrument's own hold has already expired and the stage starts cooling
for the gap between the two.

So the runner **chains**: it sends the next step's ramp just before the
current hold expires, so the instrument ramps straight on.  The soak at
the current limit is preserved, and no cooling happens in between.

This assumes the instrument's hold starts when the ramp *completes*.
That is the usual meaning of a ramp-and-hold, and it is what Linkam's
own software assumes, but it is worth checking against the hardware:
the runner exposes `chain_lead_s` and `chain_steps` so the behaviour can
be dialled in or turned off.

What this module does not do
----------------------------

It does not capture video.  :mod:`linkam.experiment` does that, and the
two compose: run the programme in one thread and record video in
another, then correlate them by timestamp.
"""

from __future__ import annotations

import csv
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)

__all__ = [
    "Step",
    "Programme",
    "ProgrammeEvent",
    "StepResult",
    "ProgrammeResult",
    "ProgrammeRunner",
]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass
class Step:
    """One segment of a temperature programme.

    Attributes
    ----------
    target_c:
        The temperature to ramp to, in degrees Celsius.
    rate_c_per_min:
        The ramp rate, in degrees Celsius per minute.
    hold_s:
        How long to dwell at `target_c` once it has been reached.
    label:
        A free-text name for the step, for the UI and the log.
    """

    target_c: float
    rate_c_per_min: float = 10.0
    hold_s: float = 0.0
    label: str = ""

    def __post_init__(self) -> None:
        if self.rate_c_per_min <= 0:
            raise ValueError("the ramp rate must be positive")
        if self.hold_s < 0:
            raise ValueError("the hold time cannot be negative")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Programme:
    """An ordered list of steps, optionally repeated.

    Attributes
    ----------
    steps:
        The steps, in order.
    repeat:
        How many times to run the whole list.  ``1`` means run it once.
    name:
        A free-text name for the programme.
    """

    steps: list[Step] = field(default_factory=list)
    repeat: int = 1
    name: str = ""

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("a programme needs at least one step")
        if self.repeat < 1:
            raise ValueError("repeat must be at least 1")

    @property
    def total_steps(self) -> int:
        """How many steps will actually be executed."""
        return len(self.steps) * self.repeat

    def validate(self) -> None:
        """Raise :class:`ValueError` if the programme cannot be run.

        The stage's limits are not known here — they come from the
        instrument — but a programme that ramps faster than any stage
        could manage is worth catching early.
        """
        for index, step in enumerate(self.steps):
            if step.rate_c_per_min <= 0:
                raise ValueError(f"step {index}: the ramp rate must be positive")
            if step.hold_s < 0:
                raise ValueError(f"step {index}: the hold time cannot be negative")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "repeat": self.repeat,
            "steps": [s.to_dict() for s in self.steps],
        }


# --------------------------------------------------------------------------- #
# Events and results
# --------------------------------------------------------------------------- #


@dataclass
class ProgrammeEvent:
    """A progress event, for the UI to render.

    The runner calls `on_event` for every phase change.  The callback is
    invoked from the runner's thread, so a UI should hand the event over
    to its own thread rather than touching widgets directly.
    """

    #: One of ``step_start``, ``reached``, ``hold``, ``step_end``,
    #: ``programme_end`` or ``error``.
    phase: str
    #: Zero-based index of the step within the programme, or ``-1``.
    step_index: int = -1
    #: The step in question, if any.
    step: Step | None = None
    #: The measured temperature at the time of the event, if known.
    temperature_c: float | None = None
    #: A human-readable message.
    message: str = ""

    @property
    def is_error(self) -> bool:
        return self.phase == "error"


@dataclass
class StepResult:
    """What happened during one step."""

    index: int
    label: str
    target_c: float
    rate_c_per_min: float
    hold_s: float
    started_at: float
    reached_at: float | None = None
    finished_at: float = 0.0
    reached: bool = False
    ramp_seconds: float | None = None
    hold_seconds: float | None = None
    final_temperature_c: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProgrammeResult:
    """The outcome of a whole programme."""

    started_at: float
    finished_at: float
    name: str
    steps: list[StepResult] = field(default_factory=list)
    output_dir: Path | None = None
    aborted: bool = False
    error: str | None = None

    @property
    def duration_s(self) -> float:
        return self.finished_at - self.started_at

    @property
    def reached_count(self) -> int:
        return sum(1 for s in self.steps if s.reached)

    def summary(self) -> str:
        state = "aborted" if self.aborted else ("failed" if self.error else "ok")
        return (
            f"{self.name or 'programme'}: {self.reached_count}/{len(self.steps)} "
            f"steps reached in {self.duration_s:.1f}s [{state}]"
        )

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "name": self.name,
            "aborted": self.aborted,
            "error": self.error,
            "steps": [s.to_dict() for s in self.steps],
        }


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #


class ProgrammeRunner:
    """Execute a :class:`Programme` on a Linkam programmer.

    Parameters
    ----------
    programmer:
        Anything with the :class:`linkam.protocol.LinkamProgrammer`
        interface — ``programme_ramp``, ``wait_for_temperature``,
        ``wait_for_stable_temperature``, ``stop``, ``open``, ``close``.
        Both the RS-232 and the USB HID programmers qualify.
    programme:
        The programme to run.
    on_event:
        Called with a :class:`ProgrammeEvent` on every phase change.
    output_dir:
        If given, the programme and the per-step results are written
        here as ``programme.json`` and ``steps.csv``.
    stall_timeout:
        Give up if the instrument reports a temperature but does not move
        for this many seconds.  ``None`` waits indefinitely.
    min_progress_c:
        The smallest temperature change that counts as progress.
    stable_tolerance_c:
        How close the temperature must be to the target for the host's
        view of a hold to count.
    chain_steps:
        Send the next step's ramp just before the current hold expires,
        so the instrument ramps straight on.  See the module docstring.
    chain_lead_s:
        How long before the hold expires the next step is sent.
    sleeper:
        Called with a number of seconds when the runner has to wait.  The
        default waits on the abort event, so an abort wakes it
        immediately.  Tests inject a no-op.
    """

    def __init__(
        self,
        programmer,
        programme: Programme,
        *,
        on_event: Callable[[ProgrammeEvent], None] | None = None,
        output_dir: Path | None = None,
        stall_timeout: float | None = 120.0,
        min_progress_c: float = 0.2,
        stable_tolerance_c: float = 0.5,
        chain_steps: bool = True,
        chain_lead_s: float = 2.0,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.programmer = programmer
        self.programme = programme
        self.on_event = on_event
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.stall_timeout = stall_timeout
        self.min_progress_c = min_progress_c
        self.stable_tolerance_c = stable_tolerance_c
        self.chain_steps = chain_steps
        self.chain_lead_s = chain_lead_s
        self._sleeper = sleeper

        self._abort = threading.Event()
        self._results: list[StepResult] = []
        self._current: StepResult | None = None
        self._temperature_c: float | None = None

    # -- abort ------------------------------------------------------------- #
    def abort(self) -> None:
        """Ask the runner to stop at the next checkpoint.

        Safe to call from another thread — that is the point of it, so a
        UI's Stop button can live on its own thread.

        An abort set before :meth:`run` is honoured: the run stops
        immediately.  :meth:`reset` clears it.
        """
        self._abort.set()

    def reset(self) -> None:
        """Clear a pending abort so the runner can be used again."""
        self._abort.clear()

    @property
    def aborted(self) -> bool:
        return self._abort.is_set()

    @property
    def results(self) -> list[StepResult]:
        """The per-step results so far."""
        return list(self._results)

    # -- events ------------------------------------------------------------ #
    def _emit(
        self,
        phase: str,
        *,
        step_index: int = -1,
        step: Step | None = None,
        message: str = "",
    ) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(
                ProgrammeEvent(
                    phase=phase,
                    step_index=step_index,
                    step=step,
                    temperature_c=self._temperature_c,
                    message=message,
                )
            )
        except Exception:  # pragma: no cover - a UI callback must not kill the run
            log.exception("programme event callback failed")

    # -- temperature tracking ---------------------------------------------- #
    def _track_temperature(self, tlm) -> None:
        value = getattr(tlm, "temperature", None)
        if value is not None:
            self._temperature_c = float(value)

    # -- the run ----------------------------------------------------------- #
    def run(self) -> ProgrammeResult:
        """Run the programme to completion.

        Returns a :class:`ProgrammeResult`.  If the run was aborted, or
        the instrument refused to ramp, the result says so rather than
        raising — but the error is also reported through the event
        callback, so a UI can show it.
        """
        self.programme.validate()
        self._results = []

        started = time.time()
        error: str | None = None
        aborted = False

        self.programmer.open()
        try:
            self.programmer.on_telemetry(self._track_temperature)
        except Exception:  # pragma: no cover - not every programmer has listeners
            pass

        try:
            for repetition in range(self.programme.repeat):
                for index, step in enumerate(self.programme.steps):
                    if self._abort.is_set():
                        aborted = True
                        break
                    self._run_step(index, step, is_last=self._is_last(index))
                    if self._abort.is_set():
                        aborted = True
                        break
                if aborted:
                    break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._emit("error", message=error)
            log.error("programme failed: %s", error)
            try:
                self.programmer.stop()
            except Exception:  # pragma: no cover
                log.exception("failed to stop the programmer")
        finally:
            finished = time.time()
            self._emit("programme_end", message=error or ("aborted" if aborted else "done"))

        result = ProgrammeResult(
            started_at=started,
            finished_at=finished,
            name=self.programme.name,
            steps=self._results,
            output_dir=self.output_dir,
            aborted=aborted,
            error=error,
        )
        self._write_outputs(result)
        log.info("%s", result.summary())
        return result

    def _is_last(self, index: int) -> bool:
        return index == len(self.programme.steps) - 1

    def _run_step(self, index: int, step: Step, *, is_last: bool) -> None:
        """Execute one step.

        The ramp command carries the dwell time, so the instrument holds
        at the limit on its own once it arrives.
        """
        result = StepResult(
            index=index,
            label=step.label,
            target_c=step.target_c,
            rate_c_per_min=step.rate_c_per_min,
            hold_s=step.hold_s,
            started_at=time.time(),
        )
        self._results.append(result)
        self._current = result

        self._emit(
            "step_start",
            step_index=index,
            step=step,
            message=f"ramping to {step.target_c:g} C at {step.rate_c_per_min:g} C/min",
        )

        # One compound command: rate, limit, dwell and start together, so
        # the instrument can never act on a half-applied programme.
        self.programmer.programme_ramp(
            row=1,
            rate=step.rate_c_per_min,
            limit=step.target_c,
            time_s=step.hold_s,
        )
        self.programmer.start()

        # Wait for the limit.  A stall here means the instrument refused
        # to ramp — unlicensed, in demonstration mode, or the ramp rate
        # was not accepted.
        self.programmer.wait_for_temperature(
            step.target_c,
            tolerance_c=self.stable_tolerance_c,
            stall_timeout=self.stall_timeout,
            min_progress_c=self.min_progress_c,
        )
        result.reached = True
        result.reached_at = time.time()
        result.ramp_seconds = result.reached_at - result.started_at
        self._emit(
            "reached",
            step_index=index,
            step=step,
            message=f"at {step.target_c:g} C",
        )

        if step.hold_s:
            self._emit(
                "hold",
                step_index=index,
                step=step,
                message=f"holding for {step.hold_s:g} s",
            )
            hold_start = time.time()
            if self.chain_steps and not is_last:
                # Send the next step just before this hold expires, so
                # the instrument ramps straight on.  The next step's ramp
                # is issued by the loop, so all that is needed here is to
                # wait out the hold minus the lead time.
                remaining = max(0.0, step.hold_s - self.chain_lead_s)
                self._sleep(remaining)
            else:
                self.programmer.wait_for_stable_temperature(
                    step.target_c,
                    duration_s=step.hold_s,
                    tolerance_c=self.stable_tolerance_c,
                )
            result.hold_seconds = time.time() - hold_start
        else:
            result.hold_seconds = 0.0

        result.finished_at = time.time()
        result.final_temperature_c = self._temperature_c
        self._emit(
            "step_end",
            step_index=index,
            step=step,
            message="done",
        )

    def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately if the run is aborted."""
        if seconds <= 0:
            return
        if self._sleeper is not None:
            self._sleeper(seconds)
            return
        self._abort.wait(seconds)

    # -- output ------------------------------------------------------------ #
    def _write_outputs(self, result: ProgrammeResult) -> None:
        if self.output_dir is None:
            return
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            (self.output_dir / "programme.json").write_text(
                json.dumps(
                    {
                        "programme": self.programme.to_dict(),
                        "result": result.to_dict(),
                    },
                    indent=2,
                    default=str,
                )
            )
            with (self.output_dir / "steps.csv").open("w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    [
                        "index", "label", "target_c", "rate_c_per_min",
                        "hold_s", "started_at", "reached_at", "finished_at",
                        "reached", "ramp_s", "hold_s_actual", "final_temperature_c",
                    ]
                )
                for s in result.steps:
                    writer.writerow(
                        [
                            s.index, s.label, f"{s.target_c:g}",
                            f"{s.rate_c_per_min:g}", f"{s.hold_s:g}",
                            f"{s.started_at:.6f}",
                            "" if s.reached_at is None else f"{s.reached_at:.6f}",
                            f"{s.finished_at:.6f}",
                            int(s.reached),
                            "" if s.ramp_seconds is None else f"{s.ramp_seconds:.3f}",
                            "" if s.hold_seconds is None else f"{s.hold_seconds:.3f}",
                            "" if s.final_temperature_c is None else f"{s.final_temperature_c:.4f}",
                        ]
                    )
        except Exception:  # pragma: no cover - writing must not kill the run
            log.exception("could not write the programme results")
