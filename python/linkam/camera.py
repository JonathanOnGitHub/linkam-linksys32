"""The camera: a live feed for the browser, and a recorder.

The web GUI shows the microscope's picture next to the temperature, and
records it while a temperature programme runs.

Three things are worth knowing about how this is put together.

**The numbers are burned into the frames.**  The time goes in the lower
left corner and the temperature in the upper right, drawn onto the frame
before it is sent to the browser *and* before it is written to the
recording.  A recorded file therefore carries its own numbers: any frame
of it can be read on its own, without a sidecar, and without the
browser.  That matters for a temperature ramp, where the video and the
temperature have to be correlated later.

**The recording is written as it happens.**  The frames go to the
encoder as they arrive; they are not collected in memory and written at
the end.  A long programme therefore costs no more memory than a short
one, and a crash costs only the last few frames.

**The camera is best-effort.**  The temperature link is the important
one: the instrument ramps on its own once it has been told to, so a
slow frame or a full disk cannot stall or overshoot the ramp.  If the
camera stops, the temperature programme carries on and the UI says so.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from .video import VideoSource, annotate, draw_scale_bar

log = logging.getLogger(__name__)

__all__ = ["CameraFeed"]


class CameraFeed:
    """The camera: frames for the browser, and a recording of them.

    Parameters
    ----------
    source:
        The camera to use -- anything with the
        :class:`~linkam.video.VideoSource` interface.  ``None`` means
        there is no camera, and the feed says so.
    state:
        A callable returning the temperature and the setpoint, so the
        numbers on the frames are the ones the instrument is reporting.
        It is called once per frame, so it must be cheap.
    output_dir:
        Where recordings are written.  ``None`` disables recording.
    feed_width:
        How wide the picture sent to the browser is.  The recording keeps
        the camera's own resolution; only the feed is scaled down, or
        encoding a full-resolution frame for the browser would cost more
        CPU than the capture itself.
    record_fps:
        The frame rate to tag the recording with.  ``None`` uses the
        camera's own rate.
    jpeg_quality:
        The quality of the JPEG sent to the browser.
    """

    def __init__(
        self,
        source: VideoSource | None = None,
        *,
        state: Callable[[], tuple] | None = None,
        output_dir: Path | None = None,
        feed_width: int = 1280,
        record_fps: float | None = None,
        jpeg_quality: int = 80,
    ) -> None:
        self._source = source
        self._state = state or (lambda: (None, None))
        self._output_dir = Path(output_dir) if output_dir else None
        self._feed_width = feed_width
        self._record_fps = record_fps
        self._jpeg_quality = jpeg_quality

        self._lock = threading.RLock()
        self._frame_cv = threading.Condition(self._lock)
        self._jpeg: bytes | None = None
        self._seq = 0
        self._captured = 0
        self._error: str | None = None

        self._writer = None
        self._recording_path: Path | None = None
        self._recording_started: float | None = None
        self._recording_frames = 0
        self._recording_range: list[float | None] = [None, None]

        #: How many micrometres one pixel is, once the user has said so.
        self._um_per_px: float | None = None
        #: The two points the user clicked, in pixels of the picture.
        self._scale_points: tuple | None = None

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------- #
    def set_output_dir(self, output_dir: Path) -> None:
        """Where the recordings go.

        The controller decides this -- it is the directory the telemetry
        CSV goes in as well -- so the video and the numbers it carries
        end up in the same place.
        """
        with self._lock:
            self._output_dir = Path(output_dir)

    def set_state_provider(self, provider: Callable[[], tuple]) -> None:
        """Say where the temperature and the setpoint come from.

        The provider is called once per frame, so it must be cheap; it
        returns ``(temperature_c, target_c)``.
        """
        with self._lock:
            self._state = provider

    def open(self) -> None:
        """Open the camera and start capturing."""
        if self._thread is not None:
            return
        if self._source is None:
            self._set_error("no camera is configured")
            return
        try:
            self._source.open()
        except Exception as exc:
            self._set_error(str(exc))
            log.warning("could not open the camera: %s", exc)
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="camera", daemon=True
        )
        self._thread.start()
        log.info("the camera is open")

    def close(self) -> None:
        """Stop capturing and close the camera."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.stop_recording()
        if self._source is not None:
            try:
                self._source.close()
            except Exception:  # pragma: no cover - closing must not raise
                log.exception("could not close the camera")

    @property
    def is_open(self) -> bool:
        return self._thread is not None and self._error is None

    # -- the capture loop --------------------------------------------------- #
    def _run(self) -> None:
        """The capture thread: read, annotate, record, publish."""
        while not self._stop.is_set():
            try:
                frame = self._source.read()
            except Exception as exc:
                self._set_error(f"the camera stopped: {exc}")
                log.exception("the camera failed")
                return
            if frame is None:
                continue

            image = frame.image
            temperature, target = self._read_state()
            annotate(
                image,
                temperature_c=temperature,
                target_c=target,
                when=frame.timestamp,
            )
            if self._um_per_px:
                draw_scale_bar(image, self._um_per_px)
            self._captured += 1
            self._write_frame(image, temperature)
            self._publish(image)

    def _read_state(self) -> tuple:
        try:
            temperature, target = self._state()
        except Exception:  # pragma: no cover - a bad callback must not stop
            log.exception("the state callback failed")
            return (None, None)
        return (temperature, target)

    # -- the browser feed --------------------------------------------------- #
    def _publish(self, image) -> None:
        """Encode the frame for the browser and publish it."""
        import cv2

        h, w = image.shape[:2]
        if self._feed_width and w > self._feed_width:
            scale = self._feed_width / float(w)
            small = cv2.resize(
                image,
                (max(1, int(w * scale)), max(1, int(h * scale))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            small = image
        try:
            ok, buf = cv2.imencode(
                ".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality]
            )
        except Exception:  # pragma: no cover - defensive
            log.exception("could not encode the frame")
            return
        if not ok:
            return
        with self._frame_cv:
            self._jpeg = buf.tobytes()
            self._seq += 1
            self._frame_cv.notify_all()

    def wait_for_frame(self, seq: int, timeout: float = 10.0):
        """Wait for a frame newer than `seq`.

        Returns ``(jpeg, seq)``, or ``(None, seq)`` if nothing new
        arrived in time.
        """
        with self._frame_cv:
            if self._seq == seq:
                self._frame_cv.wait(timeout)
            if self._seq == seq:
                return (None, seq)
            return (self._jpeg, self._seq)

    def latest(self):
        """The most recent frame, as ``(jpeg, seq)``."""
        with self._frame_cv:
            return (self._jpeg, self._seq)

    # -- the recording ------------------------------------------------------ #
    def start_recording(self) -> Path:
        """Start recording.  Returns the file the recording will go to."""
        with self._lock:
            if self._recording_path is not None:
                return self._recording_path
            if self._output_dir is None:
                raise RuntimeError("there is nowhere to record to")
            if not self.is_open:
                raise RuntimeError(self._error or "the camera is not open")
            self._output_dir.mkdir(parents=True, exist_ok=True)
            path = self._next_video_path()
            self._recording_path = path
            self._recording_started = time.time()
            self._recording_frames = 0
            self._recording_range = [None, None]
            # Open the file now if the size of the picture is known, so
            # the recording exists from the moment it starts -- a
            # programme that ends before the first frame still leaves a
            # file behind, rather than a path to nothing.
            size = getattr(self._source, "size", None)
            if size:
                writer = self._open_writer(path, size)
                if writer is not None:
                    self._writer = writer
        log.info("recording video to %s", path)
        return path

    def stop_recording(self) -> Path | None:
        """Stop recording.  Returns the file that was written, if any."""
        with self._lock:
            if self._recording_path is None:
                return None
            path = self._recording_path
            writer = self._writer
            self._writer = None
            self._recording_path = None
            started = self._recording_started
            frames = self._recording_frames
            span = list(self._recording_range)
        if writer is not None:
            try:
                writer.release()
            except Exception:  # pragma: no cover - defensive
                log.exception("could not close the video file")
        if started is not None:
            self._write_sidecar(path, started, frames, span)
        log.info("the video recording is %s (%d frames)", path, frames)
        return path

    def _next_video_path(self) -> Path:
        """The next free video file name, so a second run does not lose
        the first."""
        assert self._output_dir is not None
        candidate = self._output_dir / "video.avi"
        index = 2
        while candidate.exists():
            candidate = self._output_dir / f"video-{index}.avi"
            index += 1
        return candidate

    def _write_frame(self, image, temperature) -> None:
        """Write a frame into the recording, opening the file if needed."""
        with self._lock:
            path = self._recording_path
            writer = self._writer
            if path is None:
                return
            if writer is None:
                # The size of the picture was not known when the
                # recording started, so it is taken from the first frame.
                h, w = image.shape[:2]
                writer = self._open_writer(path, (w, h))
                if writer is None:
                    return
                self._writer = writer
            writer.write(image)
            self._recording_frames += 1
            if temperature is not None:
                low, high = self._recording_range
                self._recording_range = [
                    temperature if low is None else min(low, temperature),
                    temperature if high is None else max(high, temperature),
                ]

    def _open_writer(self, path: Path, size):
        """Open the video file, at the size of the frames going into it."""
        import cv2

        w, h = size
        fps = self._record_fps
        if not fps:
            fps = getattr(self._source, "fps", None) or 25.0
        try:
            writer = cv2.VideoWriter(
                str(path), cv2.VideoWriter_fourcc(*"MJPG"), float(fps), (w, h)
            )
        except Exception:  # pragma: no cover - defensive
            log.exception("could not open %s for writing", path)
            return None
        if not writer.isOpened():
            log.error("could not open %s for writing", path)
            return None
        log.info("recording %s at %dx%d, %.2f fps", path, w, h, fps)
        return writer

    def _write_sidecar(
        self, path: Path, started: float, frames: int, span: list
    ) -> None:
        """Write what was recorded next to the video, so the numbers can
        be trusted later."""
        low, high = span
        meta = {
            "started": started,
            "finished": time.time(),
            "duration_s": round(time.time() - started, 3),
            "frames": frames,
            "camera": getattr(self._source, "name", None),
            "temperature_c": {
                "first": low,
                "highest": high,
            },
        }
        try:
            path.with_suffix(".json").write_text(json.dumps(meta, indent=2))
        except Exception:  # pragma: no cover - a sidecar must not be fatal
            log.exception("could not write the video's sidecar")

    # -- the scale bar ------------------------------------------------------ #
    def set_scale(self, x1: float, y1: float, x2: float, y2: float, microns: float):
        """Set the scale from two points the user clicked.

        The points are in pixels of the picture, and the distance between
        them is however many micrometres the user says it is.  From that
        the scale bar is worked out, and it is drawn on every frame from
        then on -- including the recorded ones.
        """
        distance = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        if distance <= 0:
            raise ValueError("the two points are in the same place")
        if microns <= 0:
            raise ValueError("the distance has to be more than nothing")
        with self._lock:
            self._um_per_px = microns / distance
            self._scale_points = (x1, y1, x2, y2)
            value = self._um_per_px
        log.info("the scale is %.6g micrometres a pixel", value)
        return self.scale()

    def clear_scale(self) -> None:
        """Take the scale bar away."""
        with self._lock:
            self._um_per_px = None
            self._scale_points = None

    def scale(self) -> dict:
        """The scale, as the UI wants it."""
        with self._lock:
            um_per_px = self._um_per_px
            points = self._scale_points
        if not um_per_px:
            return {"set": False}
        distance = 0.0
        if points:
            distance = (
                (points[2] - points[0]) ** 2 + (points[3] - points[1]) ** 2
            ) ** 0.5
        return {
            "set": True,
            "um_per_px": um_per_px,
            "measured_px": distance,
        }

    # -- the state the UI shows --------------------------------------------- #
    def state(self) -> dict:
        """Everything the UI needs to know about the camera."""
        with self._lock:
            recording = self._recording_path
            return {
                "open": self.is_open,
                "error": self._error,
                "frames": self._captured,
                "recording": recording is not None,
                "recording_path": str(recording) if recording else None,
                "recorded_frames": self._recording_frames,
                "camera": getattr(self._source, "name", None),
                "scale": self.scale(),
            }

    def _set_error(self, message: str) -> None:
        with self._frame_cv:
            self._error = message
            self._frame_cv.notify_all()
