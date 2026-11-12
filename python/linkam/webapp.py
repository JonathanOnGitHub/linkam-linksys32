"""A small web GUI for the Linkam hot stage.

What it does
------------
* shows the stage temperature live, on screen, while it is being
  recorded;
* records every sample to a CSV;
* runs ramps and holds, single-step or multi-step, and reports what the
  programme is doing;
* shows the microscope's camera live, and records it while a
  temperature programme runs.

Why a web app
-------------
Because of one property of the hardware: **the T95's reply blocks are
indistinguishable from one poll to the next.**  Two processes polling
the same hidraw node steal each other's blocks and both read garbage.
So exactly one process may talk to the instrument.

This server is that process.  The browser is only a view: it receives
telemetry over a server-sent-events stream and sends commands back over
HTTP, and it never touches the hardware.  That also means the app works
from any browser on the network, and the CSV is written next to the
instrument rather than being downloaded from it.

The parts are the ones the package already has:

* :class:`linkam.programme.ProgrammeRunner` runs the programmes, with
  its step chaining and its abort;
* :class:`linkam.protocol.HidProgrammer` (or
  :class:`linkam.protocol.LinkamProgrammer` for the serial link) owns
  the link and its background telemetry thread;
* :class:`linkam.camera.CameraFeed` owns the camera: it captures the
  frames, writes the time and the temperature onto them, serves them to
  the browser and records them;
* the CSV is written here, because the server is the only thing that
  sees every sample.

Usage
-----
::

    python -m linkam.webapp --hid
    python -m linkam.webapp --hid --http-port 8765 --out results/run7
    python -m linkam.webapp --hid --camera aravis

Then open http://localhost:8765/ .

Safety
------
**Starting a ramp moves the stage.**  The buttons are labelled with
what they will do, the browser asks for confirmation before a ramp
starts, and there is a single, always-visible Stop button.

Two limits are worth knowing, because this package cannot check them:
the T95's cable defines a maximum ramp rate and a minimum and maximum
setpoint, and the SDK reads those from the controller before
programming.  This package does not, so a setpoint outside the cable's
range is accepted here and may be refused by the instrument.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import queue
import socket
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .camera import CameraFeed
from .programme import Programme, ProgrammeRunner, Step
from .protocol import HidProgrammer, LinkamProgrammer

log = logging.getLogger(__name__)

__all__ = [
    "StageController",
    "CameraFeed",
    "parse_steps",
    "make_server",
    "main",
]


#: How many events to keep for the UI's log.
EVENT_LOG_LIMIT = 500

#: How many samples to keep for the UI's sparkline.
TREND_LIMIT = 600

#: The largest ramp rate that will be sent without a warning.
FAST_RATE_C_PER_MIN = 50.0

#: Bounds on a target.  These are the instrument's broad range, not the
#: cable's -- the cable's limits come from the controller and are not
#: checked here.
TARGET_MIN_C = -200.0
TARGET_MAX_C = 500.0


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def parse_steps(spec: str) -> list[Step]:
    """Parse a step specification into :class:`~linkam.programme.Step`\\ s.

    The format is the same as the command line's: ``target:rate:hold``
    triples, comma separated, with the rate and the hold optional.
    """
    steps: list[Step] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if not parts[0]:
            raise ValueError(f"bad step {chunk!r}: no target")
        target = float(parts[0])
        rate = float(parts[1]) if len(parts) > 1 and parts[1] else 10.0
        hold = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
        steps.append(Step(target_c=target, rate_c_per_min=rate, hold_s=hold))
    if not steps:
        raise ValueError("no steps given")
    return steps


def _validate_step(step: Step) -> None:
    """Reject a step the instrument could not carry out sensibly."""
    if not TARGET_MIN_C <= step.target_c <= TARGET_MAX_C:
        raise ValueError(
            f"target {step.target_c:g} C is outside the instrument's range "
            f"({TARGET_MIN_C:g} to {TARGET_MAX_C:g} C)"
        )
    if step.rate_c_per_min <= 0:
        raise ValueError("the ramp rate must be positive")
    if step.hold_s < 0:
        raise ValueError("the hold time cannot be negative")


# --------------------------------------------------------------------------- #
# The controller
# --------------------------------------------------------------------------- #


class _BorrowedProgrammer:
    """A view of a programmer the controller has already opened.

    :meth:`linkam.programme.ProgrammeRunner.run` opens the programmer
    it is given.  The controller owns the only connection to the
    instrument and has already opened it, so opening it a second time
    would open a second file descriptor and start a second reader
    thread -- and two reader threads on one hidraw node steal each
    other's reply blocks.  The runner is given this proxy instead, whose
    :meth:`open` does nothing.
    """

    def __init__(self, programmer) -> None:
        self._programmer = programmer

    def open(self) -> None:
        """Do nothing: the controller owns the link and it is open."""

    def close(self) -> None:
        """Do nothing: the controller owns the link's lifetime."""

    def __getattr__(self, name):
        return getattr(self._programmer, name)


class StageController:
    """Owns the instrument, the recording and the state the UI shows.

    Exactly one controller exists per server, and it is the only thing
    that touches the hardware.

    Parameters
    ----------
    programmer:
        The programmer to use.  If omitted, the controller opens one
        itself (see :meth:`open`).
    output_dir:
        Where the CSV and the programme results are written.  Defaults
        to ``results/webui/<timestamp>``.
    camera:
        The camera feed -- a :class:`~linkam.camera.CameraFeed`.  It is
        optional: without one the GUI still runs, and the temperature
        programme is unaffected.
    """

    def __init__(
        self,
        programmer=None,
        *,
        output_dir=None,
        device="hid",
        serial_port="/dev/ttyUSB0",
        baud=9600,
        camera: CameraFeed | None = None,
    ) -> None:
        self._programmer = programmer
        self._device = device
        self._serial_port = serial_port
        self._baud = baud
        self._camera = camera
        self._record_with_programme = False

        self._lock = threading.RLock()
        self._subscribers: list[queue.Queue] = []
        self._events: list[dict] = []
        self._trend: list[tuple[float, float | None]] = []

        self._csv_file = None
        self._csv_writer = None
        self._csv_path: Path | None = None
        self._recording = True
        self._session_started = time.time()

        self._runner: ProgrammeRunner | None = None
        self._runner_thread: threading.Thread | None = None
        self._phase = "idle"
        self._phase_detail = ""
        self._last_temperature: float | None = None
        self._last_sensor2: float | None = None
        self._programmed_rate: float | None = None
        self._programmed_setpoint: float | None = None

        if output_dir is not None:
            self.output_dir = Path(output_dir)
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.output_dir = Path("results") / "webui" / stamp

    # -- lifecycle --------------------------------------------------------- #
    def open(self) -> None:
        """Open the instrument and start recording."""
        if self._programmer is None:
            if self._device == "hid":
                self._programmer = HidProgrammer()
            else:
                self._programmer = LinkamProgrammer(self._serial_port, self._baud)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._csv_path = self.output_dir / "telemetry.csv"
        self._csv_file = self._csv_path.open("w", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(
            [
                "timestamp",
                "elapsed_s",
                "temperature_c",
                "sensor2_c",
                "programmed_rate_c_per_min",
                "programmed_setpoint_c",
                "phase",
                "event",
            ]
        )
        self._csv_file.flush()

        self._programmer.on_telemetry(self._on_telemetry)
        self._programmer.open()
        self._session_started = time.time()
        self._emit(
            "session",
            message=f"connected to {self._programmer.port}",
        )
        log.info("recording to %s", self._csv_path)

        if self._camera is not None:
            # The camera is best-effort: it opens in its own thread and a
            # camera that will not open must not stop the temperature link.
            self._camera.set_output_dir(self.output_dir)
            self._camera.set_state_provider(self._temperature_and_setpoint)
            self._camera.open()

    def close(self) -> None:
        """Stop everything and close the instrument."""
        self.stop(running_only=True)
        if self._camera is not None:
            try:
                self._camera.close()
            except Exception:  # pragma: no cover - closing must not raise
                log.exception("could not close the camera")
        try:
            if self._programmer is not None:
                self._programmer.close()
        except Exception:  # pragma: no cover - closing must not raise
            log.exception("could not close the programmer")
        if self._csv_file is not None:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception:  # pragma: no cover
                log.exception("could not close the CSV file")
            self._csv_file = None

    # -- subscriptions ------------------------------------------------------ #
    def subscribe(self) -> queue.Queue:
        """Return a queue that receives every event from now on."""
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Stop sending events to `q`."""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self, event: dict) -> None:
        """Send an event to every subscriber.  Never blocks."""
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # A slow client must not stall the instrument.  Drop the
                # oldest event; the next one carries the state anyway.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):  # pragma: no cover
                    pass

    def _emit(self, phase: str, **fields) -> dict:
        """Record an event and send it to the UI."""
        event = {"type": "event", "phase": phase, "time": time.time(), **fields}
        with self._lock:
            self._events.append(event)
            if len(self._events) > EVENT_LOG_LIMIT:
                del self._events[: len(self._events) - EVENT_LOG_LIMIT]
        self._broadcast(event)
        return event

    # -- telemetry ---------------------------------------------------------- #
    def _on_telemetry(self, tlm) -> None:
        """Called from the programmer's reader thread for every sample."""
        values = tlm.values
        temperature = values.get("temperature")
        sensor2 = values.get("temperature2")
        rate = values.get("rate")
        setpoint = values.get("limit")

        now = time.time()
        with self._lock:
            self._last_temperature = temperature
            self._last_sensor2 = sensor2
            if rate is not None:
                self._programmed_rate = rate
            if setpoint is not None:
                self._programmed_setpoint = setpoint
            self._trend.append((now, temperature))
            if len(self._trend) > TREND_LIMIT:
                del self._trend[: len(self._trend) - TREND_LIMIT]
            phase = self._phase
            recording = self._recording
            writer = self._csv_writer

        if writer is not None and recording and temperature is not None:
            try:
                writer.writerow(
                    [
                        datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
                        f"{now - self._session_started:.3f}",
                        f"{temperature:.4f}",
                        "" if sensor2 is None else f"{sensor2:.4f}",
                        "" if rate is None else f"{rate:g}",
                        "" if setpoint is None else f"{setpoint:g}",
                        phase,
                        "",
                    ]
                )
                # The csv module has no flush of its own, and the file
                # object buffers: without this the row would sit in the
                # buffer and the download would not show it.
                self._csv_file.flush()
            except Exception:  # pragma: no cover - a full disk must not kill the link
                log.exception("could not write a telemetry row")

        self._broadcast(
            {
                "type": "telemetry",
                "time": now,
                "temperature_c": temperature,
                "sensor2_c": sensor2,
                "programmed_rate_c_per_min": rate,
                "programmed_setpoint_c": setpoint,
                "phase": phase,
                "phase_detail": self._phase_detail,
                "camera": self._camera_state(),
            }
        )

    # -- the camera ---------------------------------------------------------- #
    def _temperature_and_setpoint(self) -> tuple:
        """The temperature and the setpoint, for the numbers on the frames."""
        with self._lock:
            return (self._last_temperature, self._programmed_setpoint)

    def _camera_state(self) -> dict | None:
        """What the UI needs to know about the camera."""
        if self._camera is None:
            return None
        try:
            state = self._camera.state()
        except Exception:  # pragma: no cover - defensive
            log.exception("could not read the camera's state")
            return None
        state["record_with_programme"] = self._record_with_programme
        return state

    def set_record_with_programme(self, enabled: bool) -> bool:
        """Record the video while a programme runs, or do not.

        The recording starts when the programme starts and stops when it
        ends, so the video covers the ramp and the hold and nothing else.
        """
        with self._lock:
            self._record_with_programme = bool(enabled)
            value = self._record_with_programme
        self._emit(
            "recording",
            message=(
                "the video will be recorded with the programme"
                if value
                else "the video will not be recorded with the programme"
            ),
        )
        return value

    def start_video_recording(self) -> dict:
        """Start recording the video now, whatever the programme is doing."""
        if self._camera is None:
            raise RuntimeError("there is no camera")
        path = self._camera.start_recording()
        self._emit("recording", message=f"recording video to {path}")
        return {"ok": True, "recording_path": str(path)}

    def stop_video_recording(self) -> dict:
        """Stop recording the video."""
        if self._camera is None:
            raise RuntimeError("there is no camera")
        path = self._camera.stop_recording()
        if path is not None:
            self._emit("recording", message=f"the video is in {path}")
        return {"ok": True, "recording_path": str(path) if path else None}

    def _stop_video(self) -> None:
        """Stop the recording, if one is running."""
        if self._camera is None:
            return
        try:
            path = self._camera.stop_recording()
        except Exception:  # pragma: no cover - defensive
            log.exception("could not stop the video recording")
            return
        if path is not None:
            self._emit("recording", message=f"the video is in {path}")

    # -- state -------------------------------------------------------------- #
    def state(self) -> dict:
        """A snapshot of everything the UI shows."""
        with self._lock:
            return {
                "type": "state",
                "connected": bool(
                    self._programmer is not None
                    and getattr(self._programmer, "_state", None) is not None
                    and self._programmer._state.connected
                ),
                "temperature_c": self._last_temperature,
                "sensor2_c": self._last_sensor2,
                "programmed_rate_c_per_min": self._programmed_rate,
                "programmed_setpoint_c": self._programmed_setpoint,
                "phase": self._phase,
                "phase_detail": self._phase_detail,
                "recording": self._recording,
                "csv_path": str(self._csv_path) if self._csv_path else None,
                "output_dir": str(self.output_dir),
                "running": self._runner_thread is not None
                and self._runner_thread.is_alive(),
                "trend": [[t, v] for t, v in self._trend],
                "camera": self._camera_state(),
            }

    def events(self) -> list[dict]:
        """The recent event log."""
        with self._lock:
            return list(self._events)

    # -- recording ---------------------------------------------------------- #
    def set_recording(self, recording: bool) -> bool:
        """Turn recording on or off.  The file is kept either way."""
        with self._lock:
            self._recording = bool(recording)
            value = self._recording
        self._emit("recording", message="recording on" if value else "recording paused")
        return value

    # -- commands ----------------------------------------------------------- #
    def start_ramp(self, target_c: float, rate_c_per_min: float, hold_s: float) -> dict:
        """Ramp to `target_c` and hold there for `hold_s` seconds."""
        step = Step(target_c=target_c, rate_c_per_min=rate_c_per_min, hold_s=hold_s)
        _validate_step(step)
        return self._start_programme(
            Programme(name="ramp", steps=[step]),
            [step],
        )

    def start_programme(self, spec: str, repeat: int = 1) -> dict:
        """Run a multi-step programme from a ``target:rate:hold`` spec."""
        steps = parse_steps(spec)
        for step in steps:
            _validate_step(step)
        if repeat < 1:
            raise ValueError("repeat must be at least 1")
        return self._start_programme(
            Programme(name="programme", steps=steps, repeat=repeat),
            steps,
        )

    def _start_programme(self, programme: Programme, steps: list[Step]) -> dict:
        if self._runner_thread is not None and self._runner_thread.is_alive():
            raise RuntimeError("a programme is already running -- stop it first")

        self._phase = "starting"
        self._phase_detail = ""
        self._emit(
            "programme_start",
            message=(
                "ramping to "
                + ", ".join(f"{s.target_c:g} C" for s in steps)
            ),
        )
        self._start_video()
        thread = threading.Thread(
            target=self._run_programme,
            args=(programme,),
            name="programme",
            daemon=True,
        )
        self._runner_thread = thread
        thread.start()
        return {"ok": True, "steps": programme.total_steps}

    def _start_video(self) -> None:
        """Start recording the video, if it is armed to.

        The recording covers the programme and nothing else: it starts
        with the ramp and stops when the programme ends.
        """
        if self._camera is None or not self._record_with_programme:
            return
        try:
            path = self._camera.start_recording()
        except Exception as exc:
            self._emit("error", message=f"could not record the video: {exc}")
            log.warning("could not start the video recording: %s", exc)
            return
        self._emit("recording", message=f"recording video to {path}")

    def _run_programme(self, programme: Programme) -> None:
        """Run the programme.  This is the runner's thread."""
        runner = ProgrammeRunner(
            _BorrowedProgrammer(self._programmer),
            programme,
            on_event=self._on_programme_event,
            output_dir=self.output_dir,
        )
        self._runner = runner
        try:
            result = runner.run()
            self._phase = "idle" if not result.aborted else "stopped"
            self._phase_detail = result.summary()
            self._emit("programme_end", message=result.summary())
        except Exception as exc:  # pragma: no cover - defensive
            self._phase = "error"
            self._phase_detail = str(exc)
            self._emit("error", message=f"{type(exc).__name__}: {exc}")
            log.exception("the programme failed")
        finally:
            self._runner = None
            self._runner_thread = None
            self._stop_video()

    def _on_programme_event(self, event) -> None:
        """Receive a :class:`linkam.programme.ProgrammeEvent`."""
        phase = {
            "step_start": "ramping",
            "reached": "holding",
            "hold": "holding",
            "step_end": "holding",
            "programme_end": "idle",
            "error": "error",
        }.get(event.phase, event.phase)
        if event.phase != "programme_end":
            self._phase = phase
        self._phase_detail = event.message
        self._emit(
            event.phase,
            message=event.message,
            step_index=event.step_index,
            temperature_c=event.temperature_c,
            error=event.is_error,
        )

    def stop(self, running_only: bool = False) -> dict:
        """Stop the ramp and abort any programme."""
        stopped = False
        runner = self._runner
        if runner is not None:
            runner.abort()
            stopped = True
        try:
            if self._programmer is not None:
                self._programmer.stop()
                stopped = True
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("could not stop the programmer")
            self._emit("error", message=f"could not stop: {exc}")
        if stopped:
            self._phase = "stopped"
            self._phase_detail = "stopped by the operator"
            self._emit("stopped", message="stopped")
        # A programme that was stopped stops its recording with it.
        self._stop_video()
        return {"ok": True, "stopped": stopped}


# --------------------------------------------------------------------------- #
# The HTTP server
# --------------------------------------------------------------------------- #


def _sse(event: dict) -> bytes:
    """Format one event for a server-sent-events stream."""
    return f"data: {json.dumps(event, default=str)}\n\n".encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    """The HTTP routes.

    ``GET /``              the page
    ``GET /api/stream``    the live event stream (server-sent events)
    ``GET /api/state``     a snapshot of the state
    ``GET /api/events``    the event log
    ``GET /api/data.csv``  the recording
    ``POST /api/ramp``     ramp to a target and hold
    ``POST /api/programme``  run a multi-step programme
    ``POST /api/stop``     stop everything
    ``POST /api/recording``  turn recording on or off
    """

    server_version = "LinkamWebUI/1.0"
    protocol_version = "HTTP/1.1"

    #: Set by :func:`make_server`.
    controller: StageController = None  # type: ignore[assignment]

    # -- helpers ------------------------------------------------------------ #
    def log_message(self, fmt, *args) -> None:  # pragma: no cover - noise
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_bytes(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass

    def _send_json(self, code: int, payload) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send_bytes(code, "application/json", body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"bad request body: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("the request body must be a JSON object")
        return payload

    # -- routes ------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        path = urlparse(self.path).path
        controller = self.controller
        if path in ("/", "/index.html"):
            self._send_bytes(200, "text/html; charset=utf-8", _index_html())
        elif path == "/api/stream":
            self._stream()
        elif path == "/api/state":
            self._send_json(200, controller.state())
        elif path == "/api/events":
            self._send_json(200, controller.events())
        elif path == "/api/data.csv":
            self._send_csv()
        elif path == "/api/video":
            self._video_stream()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        path = urlparse(self.path).path
        controller = self.controller
        try:
            body = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        try:
            if path == "/api/ramp":
                result = controller.start_ramp(
                    float(body["target_c"]),
                    float(body.get("rate_c_per_min", 10.0)),
                    float(body.get("hold_s", 0.0)),
                )
                self._send_json(200, result)
            elif path == "/api/programme":
                result = controller.start_programme(
                    str(body.get("steps", "")),
                    int(body.get("repeat", 1)),
                )
                self._send_json(200, result)
            elif path == "/api/stop":
                self._send_json(200, controller.stop())
            elif path == "/api/recording":
                self._send_json(
                    200, {"ok": True, "recording": controller.set_recording(bool(body.get("recording", True)))}
                )
            elif path == "/api/video":
                self._send_json(200, self._video_command(body, controller))
            else:
                self._send_json(404, {"error": "not found"})
        except KeyError as exc:
            self._send_json(400, {"error": f"missing field {exc}"})
        except (ValueError, RuntimeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("the command failed")
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    # -- the event stream --------------------------------------------------- #
    def _stream(self) -> None:
        controller = self.controller
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        subscription = controller.subscribe()
        try:
            # The current state first, so the page is never blank.
            self.wfile.write(_sse(controller.state()))
            self.wfile.flush()
            while True:
                try:
                    event = subscription.get(timeout=10.0)
                except queue.Empty:
                    # A comment line keeps the connection alive through
                    # proxies that time out idle streams.
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(_sse(event))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the browser went away
        finally:
            controller.unsubscribe(subscription)

    # -- the camera ---------------------------------------------------------- #
    def _video_stream(self) -> None:
        """The live picture, as a stream of JPEG frames.

        The browser gets the frames one after another in a
        ``multipart/x-mixed-replace`` response, which is what an
        ``<img>`` tag can display.  The numbers are already burned into
        the frames, so what the browser shows is what is recorded.
        """
        camera = getattr(self.controller, "_camera", None)
        if camera is None:
            self._send_json(404, {"error": "there is no camera"})
            return
        if not camera.is_open:
            self._send_json(503, {"error": camera.state().get("error") or "the camera is not open"})
            return

        self.send_response(200)
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=frame"
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        seq = -1
        try:
            while True:
                jpeg, seq = camera.wait_for_frame(seq, timeout=10.0)
                if jpeg is None:
                    continue
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii"))
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the browser went away

    def _video_command(self, body: dict, controller) -> dict:
        """The video's controls: what to record, and when."""
        if "record" in body:
            # Armed: the video is recorded while a programme runs.
            enabled = controller.set_record_with_programme(bool(body["record"]))
            return {"ok": True, "record_with_programme": enabled}
        if "manual" in body:
            if body["manual"]:
                return controller.start_video_recording()
            return controller.stop_video_recording()
        raise ValueError("say what to do: 'record' or 'manual'")

    # -- the recording ------------------------------------------------------ #
    def _send_csv(self) -> None:
        controller = self.controller
        path = controller._csv_path
        if path is None or not path.exists():
            self._send_json(404, {"error": "nothing has been recorded yet"})
            return
        try:
            body = path.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": str(exc)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{path.name}"'
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass


def _index_html() -> bytes:
    """The page."""
    return (Path(__file__).parent / "static" / "index.html").read_bytes()


def _bind_addresses(host: str) -> list[tuple[int, str]]:
    """The address families and addresses to listen on.

    ``localhost`` is not one address.  On this machine ``getent ahosts
    localhost`` returns **``::1`` first**, and a server that listens
    only on ``127.0.0.1`` is unreachable under that name -- ``curl -6
    http://localhost:8765/`` is refused, and a browser that tries
    IPv6 first may show nothing at all.  So the loopback case listens
    on **both** ``127.0.0.1`` and ``::1``.
    """
    if host in ("", "localhost", "loopback"):
        return [
            (socket.AF_INET, "127.0.0.1"),
            (socket.AF_INET6, "::1"),
        ]
    if host == "0.0.0.0":
        return [(socket.AF_INET, "0.0.0.0")]
    if host == "::":
        return [(socket.AF_INET6, "::")]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return [(socket.AF_INET, host)]
    out: list[tuple[int, str]] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        address = sockaddr[0]
        if family == socket.AF_INET6 and address.startswith("::ffff:"):
            continue  # an IPv4 address in IPv6 form; the IPv4 socket covers it
        if (family, address) not in out:
            out.append((family, address))
    return out or [(socket.AF_INET, host)]


def make_servers(
    controller: StageController,
    host: str = "localhost",
    port: int = 8765,
) -> list[ThreadingHTTPServer]:
    """Build the HTTP servers for `controller`.

    Returns one server per address to listen on.  On the loopback
    addresses that is two -- an IPv4 and an IPv6 one -- because
    ``localhost`` may resolve to either.
    """
    handler = type("_BoundHandler", (_Handler,), {"controller": controller})
    servers: list[ThreadingHTTPServer] = []
    for family, address in _bind_addresses(host):
        # Port 0 means "any free port", and each socket would then pick
        # a *different* one -- so the first port chosen is reused for the
        # rest, which keeps the pair answering on the same address.
        bind_port = port
        if family == socket.AF_INET6:
            class _V6Server(ThreadingHTTPServer):
                address_family = socket.AF_INET6

            server = _V6Server((address, bind_port), handler, bind_and_activate=False)
            try:
                server.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                server.server_bind()
                server.server_activate()
            except OSError:
                # No IPv6 on this machine; the IPv4 listener is enough.
                server.server_close()
                continue
        else:
            server = ThreadingHTTPServer((address, bind_port), handler)
        servers.append(server)
        if port == 0:
            port = server.server_address[1]
    return servers


def make_server(controller: StageController, host: str = "localhost", port: int = 8765):
    """Build a single HTTP server for `controller`.

    Kept for the tests and for anyone who wants one server rather than
    a pair.
    """
    return make_servers(controller, host, port)[0]


def _urls(servers, host: str, port: int) -> list[str]:
    """The URLs a browser can actually use."""
    out = []
    for server in servers:
        address = server.server_address[0]
        if address == "0.0.0.0":
            out.append(f"http://127.0.0.1:{port}/")
            out.append(f"http://[::1]:{port}/")
            out.append(f"http://<this machine>:{port}/")
        elif address == "::":
            out.append(f"http://[::1]:{port}/")
        elif ":" in address:
            out.append(f"http://[{address}]:{port}/")
        else:
            out.append(f"http://{address}:{port}/")
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _resolve_mode(args) -> tuple[bool, str]:
    """Whether to simulate the stage, and which camera to use.

    ``--demo`` means "nothing attached": the stage is simulated and,
    unless a camera was asked for by name, the camera's frames are
    drawn.  So the whole page can be looked at with no hardware.
    """
    simulate = args.simulate or args.demo
    camera = args.camera
    if args.demo and camera == "none":
        camera = "synthetic"
    return simulate, camera


def _make_camera(args, backend: str) -> CameraFeed | None:
    """Build the camera feed, if one was asked for.

    The camera is optional, and a camera that will not open is not a
    reason to refuse to start: the temperature programme is the
    important one, and it must not be held up by the microscope.  So the
    feed is built, and if the camera cannot be opened the feed says so
    and the GUI carries on without it.
    """
    if backend == "none":
        return None

    from .video import AravisBackend, SyntheticBackend, maybe_reexec_for_aravis

    if backend == "aravis":
        # The dynamic linker reads LD_LIBRARY_PATH once, when the process
        # starts, so a local Aravis can only be found by starting again
        # with the right environment.  This replaces the process, so it
        # never returns when it succeeds.
        maybe_reexec_for_aravis()
        source = AravisBackend(
            args.camera_device,
            exposure_us=args.camera_exposure,
            gain_db=args.camera_gain,
            roi=args.camera_roi,
            fps=args.camera_fps,
            pixel_format=args.camera_pixel_format,
        )
    else:
        source = SyntheticBackend(width=640, height=480, fps=25.0)

    return CameraFeed(
        source,
        state=lambda: (None, None),  # replaced by the controller
        feed_width=args.video_width,
        record_fps=args.video_fps,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="A web GUI for the Linkam hot stage.",
    )
    parser.add_argument("--hid", action="store_true",
                        help="use the T95's USB HID link (no serial adapter needed)")
    parser.add_argument("--port", dest="serial_port", default="/dev/ttyUSB0",
                        help="serial port for the RS-232 programmers")
    parser.add_argument("--baud", type=int, default=9600,
                        help="baud rate for the serial link")
    parser.add_argument("--host", default="localhost",
                        help="address to listen on.  The default listens on "
                             "both 127.0.0.1 and ::1, because 'localhost' "
                             "may resolve to either.  Use 0.0.0.0 to reach "
                             "it from another machine")
    parser.add_argument("--http-port", type=int, default=8765,
                        help="port to listen on")
    parser.add_argument("--out", default=None,
                        help="directory for the CSV and the programme results")
    parser.add_argument("--simulate", action="store_true",
                        help="run against the software simulator instead of "
                             "an instrument (nothing is moved)")
    parser.add_argument("--demo", action="store_true",
                        help="open the GUI with nothing attached at all: the "
                             "stage and the camera are both simulated, so "
                             "the page can be looked at with no hardware")
    parser.add_argument("--camera", default="none",
                        choices=("none", "aravis", "synthetic"),
                        help="the camera to show: 'aravis' is a GenICam "
                             "camera (the microscope's Alvium), 'synthetic' "
                             "draws the frames instead of capturing them")
    parser.add_argument("--camera-device", default=None,
                        help="the camera's id, or its index among the "
                             "cameras that are connected")
    parser.add_argument("--camera-roi", default=None,
                        help="the camera's region of interest, e.g. 1280x1024")
    parser.add_argument("--camera-exposure", type=float, default=None,
                        help="exposure time in microseconds")
    parser.add_argument("--camera-gain", type=float, default=None,
                        help="gain in dB")
    parser.add_argument("--camera-fps", type=float, default=None,
                        help="the camera's frame rate")
    parser.add_argument("--camera-pixel-format", default=None,
                        help="a GenICam pixel format, e.g. Mono8")
    parser.add_argument("--video-width", type=int, default=1280,
                        help="how wide the picture sent to the browser is "
                             "(the recording keeps the camera's own size)")
    parser.add_argument("--video-fps", type=float, default=None,
                        help="the frame rate to tag the recording with")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # --demo means "nothing attached": the stage is simulated and, unless
    # a camera was asked for by name, the camera's frames are drawn.
    simulate, camera_backend = _resolve_mode(args)

    camera = _make_camera(args, camera_backend)

    if simulate:
        from .simulator import SimulatedProgrammer

        controller = StageController(
            SimulatedProgrammer(start_c=20.0, ambient_c=20.0),
            output_dir=args.out,
            camera=camera,
        )
    else:
        controller = StageController(
            output_dir=args.out,
            device="hid" if args.hid else "serial",
            serial_port=args.serial_port,
            baud=args.baud,
            camera=camera,
        )

    try:
        controller.open()
    except Exception as exc:
        print(f"could not open the instrument: {exc}", flush=True)
        return 1

    servers = make_servers(controller, args.host, args.http_port)
    if not servers:
        print("could not listen on any address", flush=True)
        controller.close()
        return 1

    print("Linkam web UI:", flush=True)
    for url in _urls(servers, args.host, args.http_port):
        print(f"    {url}", flush=True)
    if args.host in ("", "localhost", "127.0.0.1", "::1"):
        print(
            "    (reachable from another machine with --host 0.0.0.0)",
            flush=True,
        )
    print(f"recording to  {controller._csv_path}", flush=True)
    print("press Ctrl-C to stop", flush=True)

    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in servers
    ]
    for thread in threads:
        thread.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
