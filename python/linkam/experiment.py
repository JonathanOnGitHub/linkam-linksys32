"""Run an isothermal experiment: temperature ramp + simultaneous video capture.

This is the composition the original Linksys32 does in its Capture Module,
but split into two independent threads so that a slow disk, a slow encoder
or a hiccup in the video path cannot stall the temperature control loop.

Design notes
------------

* The **temperature link** is a hard real-time-ish path.  The hardware
  pushes telemetry every 0.2977 s and the ramp controller lives in the
  programmer itself, so once you have issued ``Prog start`` the hardware
  ramps on its own.  Python only has to (a) issue commands and (b) log
  telemetry.  A stalled Python thread therefore cannot stop or overshoot
  the ramp.

* The **video link** is a best-effort path.  Frames are timestamped from
  the wall clock and written as they arrive.  If a frame is late it is
  recorded late, but the temperature record stays correct.

* The two records are correlated by timestamp, not by index.
"""

from __future__ import annotations

import csv
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .protocol import LinkamProgrammer, Telemetry
from .video import Frame, VideoSource

log = logging.getLogger(__name__)

__all__ = ["Experiment", "ExperimentResult", "IsothermalRun"]


@dataclass
class ExperimentResult:
    """Outcome of a run."""

    started_at: float
    finished_at: float
    target_c: float
    rate_c_per_min: float
    hold_s: float
    frames_captured: int
    telemetry_samples: int
    final_temperature_c: float | None
    output_dir: Path

    @property
    def duration_s(self) -> float:
        return self.finished_at - self.started_at

    def summary(self) -> str:
        return (
            f"duration={self.duration_s:.1f}s "
            f"frames={self.frames_captured} "
            f"telemetry={self.telemetry_samples} "
            f"final_T={self.final_temperature_c}"
        )


@dataclass
class Experiment:
    """Configuration for a temperature + video run."""

    target_c: float
    rate_c_per_min: float = 10.0
    hold_s: float = 0.0
    output_dir: Path = field(default_factory=lambda: Path("linkam_run"))
    serial_port: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    #: Use the USB HID link (Linkam T95) instead of the legacy RS-232
    #: serial link.  When true, `hid_node` selects the hidraw device.
    use_hid: bool = False
    #: Explicit hidraw node, e.g. ``/dev/hidraw1``.  ``None`` means
    #: "discover the T95 automatically".
    hid_node: str | None = None
    #: HID report framing; see :mod:`linkam.hid`.
    hid_framing: str = "text_padded"
    #: Output report size in bytes; ``None`` means "discover it".
    hid_report_size: int | None = None
    video_backend: str = "auto"
    video_device: str | int = 0
    video_width: int = 640
    video_height: int = 480
    video_fps: float = 25.0
    save_video: bool = True
    save_frames: bool = False
    strict_framing: bool = False
    #: Hold at the target indefinitely when `hold_s` is 0.  Set False to
    #: return as soon as the ramp completes.
    hold_forever: bool = True
    #: Give up if the programmer reports a temperature but does not move for
    #: this many seconds.  This is what turns "the instrument silently
    #: refused to ramp" into a clear error.  Set to None to wait for the
    #: full ramp timeout instead.
    stall_timeout: float | None = 120.0
    #: Smallest temperature change that counts as progress toward the
    #: target.  Should exceed the sensor noise.
    min_progress_c: float = 0.2
    #: How close to the target the temperature must be for the hold to
    #: count as "at temperature".  This is the band within which the
    #: temperature is considered stable.
    stable_tolerance_c: float = 0.5

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)


class IsothermalRun:
    """Orchestrates a Linkam temperature program alongside video capture.

    Example
    -------
    >>> run = IsothermalRun(Experiment(target_c=95.0, rate_c_per_min=2.0,
    ...                                output_dir="out"))   # doctest: +SKIP
    >>> result = run.start()                                # doctest: +SKIP
    """

    def __init__(
        self,
        experiment: Experiment,
        *,
        programmer: LinkamProgrammer | None = None,
        video: VideoSource | None = None,
    ) -> None:
        self.exp = experiment
        self.programmer = programmer
        self.video = video
        self._stop = threading.Event()
        self._telemetry: list[Telemetry] = []
        self._frames: list[Frame] = []
        self._lock = threading.Lock()

    # -- setup ------------------------------------------------------------ #
    def _open(self) -> None:
        exp = self.exp
        exp.output_dir.mkdir(parents=True, exist_ok=True)

        if self.programmer is None:
            if exp.use_hid:
                from .protocol import HidProgrammer

                self.programmer = HidProgrammer(
                    exp.hid_node,
                    framing=exp.hid_framing,
                    report_size=exp.hid_report_size,
                )
            else:
                self.programmer = LinkamProgrammer(
                    exp.serial_port,
                    exp.baudrate,
                    strict_framing=exp.strict_framing,
                )
        # Always open the programmer, whether we created it or it was
        # supplied by the caller.  A caller-supplied programmer (e.g. the
        # simulator, or one the user opened themselves) may still need its
        # reader thread started.
        self.programmer.open()

        if self.video is None:
            from .video import open_video

            self.video = open_video(
                exp.video_backend,
                exp.video_device,
                width=exp.video_width,
                height=exp.video_height,
                fps=exp.video_fps,
            )
        else:
            self.video.open()

    def _close(self) -> None:
        if self.video is not None:
            try:
                self.video.close()
            except Exception:  # pragma: no cover
                log.exception("error closing video")
        if self.programmer is not None:
            try:
                self.programmer.close()
            except Exception:  # pragma: no cover
                log.exception("error closing programmer")

    # -- threads ---------------------------------------------------------- #
    def _telemetry_thread(self) -> None:
        prog = self.programmer
        assert prog is not None

        def on_data(tlm: Telemetry) -> None:
            with self._lock:
                self._telemetry.append(tlm)

        prog.on_telemetry(on_data)
        while not self._stop.is_set():
            time.sleep(0.05)

    def _video_thread(self) -> None:
        video = self.video
        assert video is not None
        while not self._stop.is_set():
            frame = video.read()
            if frame is None:
                time.sleep(0.005)
                continue
            with self._lock:
                self._frames.append(frame)

    # -- persistence ------------------------------------------------------ #
    def _snapshot_telemetry(self) -> list[Telemetry]:
        """Take an atomic copy of the telemetry received so far.

        The reader thread appends to ``self._telemetry`` continuously, so
        reading that list more than once can yield *different* lengths on
        two successive reads.  Taking one copy under the lock and using it
        for both the CSV and the run summary guarantees the two always
        describe the same set of samples.
        """
        with self._lock:
            return list(self._telemetry)

    def _write_telemetry_csv(self, telemetry: list[Telemetry]) -> Path:
        path = self.exp.output_dir / "telemetry.csv"
        keys = sorted({k for t in telemetry for k in t.values})
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["timestamp", "elapsed_s", *keys])
            if telemetry:
                t0 = telemetry[0].received_at
                for t in telemetry:
                    w.writerow(
                        [f"{t.received_at:.6f}",
                         f"{t.received_at - t0:.3f}",
                         *[(f"{t.values[k]:g}" if k in t.values else "")
                           for k in keys]]
                    )
        return path

    def _write_video(self) -> Path | None:
        if not self.exp.save_video or not self._frames:
            return None
        import cv2
        import numpy as np

        path = self.exp.output_dir / "capture.mp4"
        first = self._frames[0].image
        h, w = first.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(path), fourcc, self.exp.video_fps, (w, h))
        try:
            for f in self._frames:
                writer.write(f.image)
        finally:
            writer.release()
        return path

    def _write_frames(self) -> Path | None:
        if not self.exp.save_frames or not self._frames:
            return None
        import cv2

        d = self.exp.output_dir / "frames"
        d.mkdir(exist_ok=True)
        for f in self._frames:
            cv2.imwrite(str(d / f"frame_{f.index:06d}.jpg"), f.image)
        return d

    def _write_meta(self, result: ExperimentResult) -> Path:
        path = self.exp.output_dir / "run.json"
        path.write_text(json.dumps(asdict(result), indent=2, default=str))
        return path

    # -- run -------------------------------------------------------------- #
    def start(self) -> ExperimentResult:
        """Open the hardware, run the program, and collect results."""
        self._stop.clear()
        self._open()
        started = time.time()
        log.info("experiment start: target=%.2f C rate=%.2f C/min",
                 self.exp.target_c, self.exp.rate_c_per_min)

        tel_thread = threading.Thread(target=self._telemetry_thread, daemon=True)
        vid_thread = threading.Thread(target=self._video_thread, daemon=True)
        tel_thread.start()
        vid_thread.start()

        prog = self.programmer
        assert prog is not None
        try:
            # 1. programme the ramp.
            #
            # We send this as ONE compound command (as the original Linksys32
            # does when it writes a profile row) rather than as separate
            # rate/limit/time commands, so the programmer can never act on a
            # half-applied programme.
            prog.programme_ramp(
                row=1,
                rate=self.exp.rate_c_per_min,
                limit=self.exp.target_c,
                time_s=self.exp.hold_s,
            )
            prog.start()

            # 2. wait for the limit to be reached
            #
            # `stall_timeout` makes a refused ramp fail fast with a clear
            # message instead of hanging until the ramp timeout expires.
            prog.wait_for_temperature(
                self.exp.target_c,
                tolerance_c=0.5,
                stall_timeout=self.exp.stall_timeout,
                min_progress_c=self.exp.min_progress_c,
            )

            # 3. hold at the target
            #
            # The hardware runs the hold itself: the compound command
            # above included `time <hold_s>`, so the programmer ramps to
            # the limit and then holds there for that long on its own.
            #
            # The host therefore does *not* sleep for `hold_s` and then
            # stop -- that would double-count the hold and, worse, would
            # cut the real hold short whenever the tolerance check above
            # returned late.  Instead we wait until the measured
            # temperature has been *stable* at the target for `hold_s`,
            # which is what "hold at temperature" means physically, and
            # works whether or not the hardware honours its own `time`.
            if self.exp.hold_s:
                prog.wait_for_stable_temperature(
                    self.exp.target_c,
                    duration_s=self.exp.hold_s,
                    tolerance_c=self.exp.stable_tolerance_c,
                )
                prog.stop()

            elif self.exp.hold_forever:
                # No hold time given: hold at the limit indefinitely, which
                # is the normal "isothermal" experiment.  The hardware ramps
                # on its own, so we simply keep collecting until interrupted.
                log.info("holding at %.2f C (no hold time set); Ctrl-C to stop",
                         self.exp.target_c)
                while not self._stop.is_set():
                    time.sleep(0.2)

        except KeyboardInterrupt:
            log.info("interrupted; stopping programmer")
            try:
                prog.stop()
            except Exception:  # pragma: no cover
                log.exception("failed to stop programmer")
        finally:
            finished = time.time()
            self._stop.set()
            tel_thread.join(timeout=2.0)
            vid_thread.join(timeout=2.0)
            # Take one snapshot and use it for every output, so that
            # telemetry.csv and run.json always describe the same samples.
            telemetry = self._snapshot_telemetry()
            self._write_telemetry_csv(telemetry)
            self._write_video()
            self._write_frames()
            self._close()

        final_t = telemetry[-1].temperature if telemetry else None
        result = ExperimentResult(
            started_at=started,
            finished_at=finished,
            target_c=self.exp.target_c,
            rate_c_per_min=self.exp.rate_c_per_min,
            hold_s=self.exp.hold_s,
            frames_captured=len(self._frames),
            telemetry_samples=len(telemetry),
            final_temperature_c=final_t,
            output_dir=self.exp.output_dir,
        )
        self._write_meta(result)
        log.info("experiment done: %s", result.summary())
        return result
