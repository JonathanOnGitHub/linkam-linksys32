"""Video capture for the Linkam VideoFusion (Conexant BT848) card and friends.

Evidence from the shipped files:

* ``VideoFusion/Bt848.inf`` registers the capture device under the **standard
  DirectShow Video Capture filter CLSID**::

      {17CCA71B-ECD7-11D0-B908-00A0C9223196}

  with a KS (Kernel Streaming) driver ``BT848.sys`` for
  ``PCI\\VEN_109E&DEV_0350/0351/036E/036F`` (Conexant/Brooktree BT848).

* ``Linksys32/lnkFusion.dll`` builds a DirectShow graph using the standard
  ``ICaptureGraphBuilder2`` interface and locates the device by
  ``FriendlyName == "Conexant's BtPCI Capture"``.  It adds the standard
  ``SampleGrabber`` filter and a ``NullRenderer``.

Consequences:

* **On Windows**, the card is an ordinary DirectShow device.  Any of
  PyAV/ffmpeg, ``opencv-python`` (DSHOW backend), or ``pygrabber`` can open
  it.  We do **not** need to talk to BT848.sys directly.
* **On Linux**, the card needs a `v4l2` bridge.  The BT848 is a PCI video
  device; the practical options are (a) a real Linux BT848 driver, or
  (b) capture on a Windows host and stream over the network.  There is no
  Linux driver for the Conexant BT848 in mainline, so this usually means
  option (b) or a dedicated capture box.

The class below therefore provides two backends:

``DirectShowBackend``
    Windows: uses ``opencv-python``'s DSHOW capture, or PyAV if present.

``V4L2Backend``
    Linux: uses V4L2 via ``opencv-python``'s V4L2 backend or raw ioctls.

Both expose the same ``open()`` / ``read()`` / ``close()`` surface so the
recording loop does not care which is in use.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Iterator

log = logging.getLogger(__name__)

__all__ = [
    "Frame",
    "VideoSource",
    "DirectShowBackend",
    "V4L2Backend",
    "VimbaXBackend",
    "AravisBackend",
    "SyntheticBackend",
    "annotate",
    "open_video",
    "maybe_reexec_for_aravis",
    "BACKENDS",
]


@dataclass
class Frame:
    """One captured frame."""

    image: object          # numpy.ndarray, BGR
    timestamp: float
    index: int
    #: The camera's own timestamp, when the backend can supply one.
    #: ``None`` for backends with no device clock.  It is *not* on the
    #: same time base as `timestamp` unless the backend says so, so it
    #: is for diagnostics rather than for correlation.
    device_timestamp: float | None = None


class VideoSource:
    """Abstract video source."""

    name: str = "video"

    def open(self) -> None:
        raise NotImplementedError

    def read(self) -> Frame | None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> "VideoSource":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def frames(self) -> Iterator[Frame]:
        """Yield frames until the source is exhausted or closed."""
        idx = 0
        while True:
            f = self.read()
            if f is None:
                return
            yield f
            idx += 1


# --------------------------------------------------------------------------- #
# Windows / DirectShow
# --------------------------------------------------------------------------- #

#: The FriendlyName the Linkam driver registers, and the CLSID it answers to.
VIDEOFUSION_FRIENDLY_NAME = "Conexant's BtPCI Capture"
VIDEOFUSION_CAPTURE_CLSID = "{17CCA71B-ECD7-11D0-B908-00A0C9223196}"

#: DirectShow Video Capture filter CLSID (standard).
DSHOW_CAPTURE_CLSID = "{17CCA71B-ECD7-11D0-B908-00A0C9223196}"


class DirectShowBackend(VideoSource):
    """Capture from a DirectShow device by friendly name or index.

    Uses ``opencv-python``'s ``cv2.CAP_DSHOW`` backend, which internally
    builds the same ``ICaptureGraphBuilder2`` graph the original
    ``lnkFusion.dll`` builds, so the Linkam driver works unmodified.
    """

    def __init__(
        self,
        device: str | int = 0,
        *,
        width: int = 640,
        height: int = 480,
        fps: float = 25.0,
        fourcc: str | None = "YUY2",
    ) -> None:
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.fourcc = fourcc
        self.name = f"dshow:{device}"
        self._cap = None
        self._index = 0

    def open(self) -> None:
        import cv2

        if self._cap is not None:
            return  # already open
        backend = getattr(cv2, "CAP_DSHOW", cv2.CAP_ANY)
        cap = cv2.VideoCapture(self.device, backend)
        if not cap.isOpened():
            raise RuntimeError(
                f"could not open DirectShow device {self.device!r}. "
                f"Check that the '{VIDEOFUSION_FRIENDLY_NAME}' driver is "
                f"installed, or try a different index."
            )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        if self.fourcc:
            fourcc = cv2.VideoWriter_fourcc(*self.fourcc)
            cap.set(cv2.CAP_PROP_FOURCC, fourcc)
        self._cap = cap
        log.info(
            "opened %s at %dx%d @ %.1f fps",
            self.name, self.width, self.height, self.fps,
        )

    def read(self) -> Frame | None:
        if self._cap is None:
            raise RuntimeError("video source is not open")
        ok, img = self._cap.read()
        if not ok or img is None:
            return None
        frame = Frame(image=img, timestamp=time.time(), index=self._index)
        self._index += 1
        return frame

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


# --------------------------------------------------------------------------- #
# Linux / V4L2
# --------------------------------------------------------------------------- #

class V4L2Backend(VideoSource):
    """Capture from a V4L2 device (``/dev/video0`` etc.).

    Note: there is no mainline Linux driver for the Conexant BT848, so this
    backend suits other cameras (e.g. a USB capture stick or the PixeLINK
    via a converted driver) rather than the Linkam VideoFusion card itself.
    """

    def __init__(
        self,
        device: str = "/dev/video0",
        *,
        width: int = 640,
        height: int = 480,
        fps: float = 25.0,
    ) -> None:
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.name = f"v4l2:{device}"
        self._cap = None
        self._index = 0

    def open(self) -> None:
        import cv2

        if self._cap is not None:
            return  # already open
        cap = cv2.VideoCapture(self.device, getattr(cv2, "CAP_V4L2", cv2.CAP_ANY))
        if not cap.isOpened():
            raise RuntimeError(f"could not open V4L2 device {self.device!r}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        self._cap = cap
        log.info("opened %s at %dx%d @ %.1f fps",
                 self.name, self.width, self.height, self.fps)

    def read(self) -> Frame | None:
        if self._cap is None:
            raise RuntimeError("video source is not open")
        ok, img = self._cap.read()
        if not ok or img is None:
            return None
        frame = Frame(image=img, timestamp=time.time(), index=self._index)
        self._index += 1
        return frame

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Allied Vision / Vimba X
# --------------------------------------------------------------------------- #

#: Bayer patterns the Vimba X pixel formats use, and the OpenCV colour
#: conversion that debayers them.
_BAYER_CONVERSIONS = {
    "bayerbg": "COLOR_BayerBG2BGR",
    "bayergr": "COLOR_BayerGR2BGR",
    "bayergb": "COLOR_BayerGB2BGR",
    "bayerrg": "COLOR_BayerRG2BGR",
}


class VimbaXBackend(VideoSource):
    """Capture from an Allied Vision camera through the Vimba X SDK.

    Vimba X is Allied Vision's current, GenICam-compliant SDK — the
    successor to Vimba — and it is what the Alvium cameras (including
    the Alvium 1800 U-508c) speak.  The Python binding is ``vmbpy``:

    .. code-block:: bash

        pip install vmbpy

    On Linux the ``vmbpy`` wheel bundles the VmbC libraries but **not**
    the transport layer, so Vimba X itself has to be installed as well
    and the GenTL found through the ``GENICAM_GENTL64_PATH`` environment
    variable.  Without it the SDK finds no devices and this backend
    raises with an explanation.

    Parameters
    ----------
    device:
        The camera id (a string such as ``"0"``) or an index into the
        list of connected cameras.
    exposure_us:
        Exposure time in microseconds.  ``None`` leaves the camera's
        setting alone.
    frame_rate:
        Frame rate in frames per second.  ``None`` leaves it alone.
    pixel_format:
        A Vimba X ``PixelFormat`` name such as ``"Mono8"``.  ``None``
        leaves the camera's setting alone.
    timeout_ms:
        How long to wait for each frame.
    """

    def __init__(
        self,
        device: str | int = 0,
        *,
        exposure_us: float | None = None,
        frame_rate: float | None = None,
        pixel_format: str | None = None,
        timeout_ms: int = 2000,
    ) -> None:
        self.device = device
        self.exposure_us = exposure_us
        self.frame_rate = frame_rate
        self.pixel_format = pixel_format
        self.timeout_ms = timeout_ms
        self.name = f"vimbax:{device}"
        self._stack = None
        self._camera = None
        self._index = 0

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _import_vmbpy():
        try:
            import vmbpy
        except ImportError as exc:  # pragma: no cover - depends on the host
            raise RuntimeError(
                "the Vimba X Python binding is not installed. Install it "
                "with 'pip install vmbpy'. On Linux the Vimba X transport "
                "layer has to be installed as well, and the GenTL found "
                "through the GENICAM_GENTL64_PATH environment variable -- "
                "the vmbpy wheel alone contains no device drivers."
            ) from exc
        return vmbpy

    @staticmethod
    def list_cameras() -> list[dict]:
        """List the Allied Vision cameras the SDK can see.

        Returns one dict per camera with its id, name, model and serial
        number.  Useful for picking the right camera when more than one
        is attached.
        """
        vmbpy = VimbaXBackend._import_vmbpy()
        import contextlib

        out: list[dict] = []
        with contextlib.ExitStack() as stack:
            vmb = stack.enter_context(vmbpy.VmbSystem.get_instance())
            for cam in vmb.get_all_cameras():
                out.append(
                    {
                        "id": cam.get_id(),
                        "name": cam.get_name(),
                        "model": cam.get_model(),
                        "serial": cam.get_serial(),
                    }
                )
        return out

    def _select(self, vmb) -> object:
        if isinstance(self.device, str):
            return vmb.get_camera_by_id(self.device)
        cameras = vmb.get_all_cameras()
        if not cameras:
            raise RuntimeError(
                "no Allied Vision camera was found. Check that the camera is "
                "powered and connected, that the Vimba X transport layer is "
                "installed, and that GENICAM_GENTL64_PATH points at it."
            )
        try:
            return cameras[int(self.device)]
        except IndexError as exc:
            raise RuntimeError(
                f"camera index {self.device} is out of range; "
                f"{len(cameras)} camera(s) are connected"
            ) from exc

    def _set_features(self, cam) -> None:
        def apply(name: str, value) -> None:
            try:
                feature = cam.get_feature_by_name(name)
            except Exception:
                log.info("the camera has no %s feature; leaving it alone", name)
                return
            if not feature.is_available():
                log.info("%s is not available on this camera; leaving it alone", name)
                return
            feature.set(value)
            log.info("%s = %s", name, value)

        if self.exposure_us is not None:
            apply("ExposureTime", float(self.exposure_us))
        if self.frame_rate is not None:
            apply("AcquisitionFrameRate", float(self.frame_rate))
        if self.pixel_format is not None:
            apply("PixelFormat", self.pixel_format)

    # -- the VideoSource interface ----------------------------------------- #
    def open(self) -> None:
        import contextlib

        vmbpy = self._import_vmbpy()
        if self._stack is not None:
            return  # already open

        stack = contextlib.ExitStack()
        try:
            vmb = stack.enter_context(vmbpy.VmbSystem.get_instance())
            cam = self._select(vmb)
            stack.enter_context(cam)
            self._set_features(cam)
        except Exception:
            stack.close()
            raise

        self._stack = stack
        self._camera = cam
        self._index = 0
        log.info("opened %s", self.name)

    def read(self) -> Frame | None:
        if self._camera is None:
            raise RuntimeError("video source is not open")
        frame = self._camera.get_frame(timeout_ms=self.timeout_ms)
        if frame is None:
            return None
        image = self._to_bgr(frame)
        if image is None:
            return None
        out = Frame(
            image=image,
            timestamp=time.time(),
            index=self._index,
            device_timestamp=self._device_timestamp(frame),
        )
        self._index += 1
        return out

    @staticmethod
    def _device_timestamp(frame) -> float | None:
        try:
            return float(frame.get_timestamp())
        except Exception:
            return None

    @staticmethod
    def _to_bgr(frame):
        """Turn a Vimba X frame into an OpenCV-style BGR array."""
        import numpy as np

        # `as_opencv_image` does the conversion, including debayering,
        # but needs the optional `opencv` extra.
        try:
            image = frame.as_opencv_image()
            if image is not None:
                return image
        except Exception:
            pass

        array = frame.as_numpy_ndarray()
        if array is None:
            return None

        name = str(getattr(frame, "get_pixel_format", lambda: "")() or "")
        key = name.lower().replace("_", "")
        if key.startswith("mono"):
            return np.repeat(array[:, :, None], 3, axis=2) if array.ndim == 2 else array
        for prefix, conversion in _BAYER_CONVERSIONS.items():
            if key.startswith(prefix):
                import cv2

                return cv2.cvtColor(array, getattr(cv2, conversion))
        if array.ndim == 2:
            return np.repeat(array[:, :, None], 3, axis=2)
        return array

    def close(self) -> None:
        if self._stack is not None:
            try:
                self._stack.close()
            except Exception:  # pragma: no cover
                log.exception("error closing the Vimba X camera")
            self._stack = None
            self._camera = None


# --------------------------------------------------------------------------- #
# Allied Vision / Aravis (GenICam over USB3 Vision)
# --------------------------------------------------------------------------- #

#: Where a locally unpacked Aravis lives, when it is not installed
#: system-wide.  The same variable the microscope's own capture tool uses.
ARAVIS_PREFIX = "/tmp/opencode/aravis-root"

#: The Aravis pixel formats, and what each one is made of:
#: name -> (numpy dtype, bytes per pixel, is_bayer).
_ARAVIS_PIXEL_FORMATS = {
    "Mono8": ("uint8", 1, False),
    "Mono8Signed": ("int8", 1, False),
    "Mono10": ("uint16", 2, False),
    "Mono10Packed": ("uint16", 2, False),
    "Mono12": ("uint16", 2, False),
    "Mono12Packed": ("uint16", 2, False),
    "Mono14": ("uint16", 2, False),
    "Mono16": ("uint16", 2, False),
    "BayerRG8": ("uint8", 1, True),
    "BayerBG8": ("uint8", 1, True),
    "BayerGR8": ("uint8", 1, True),
    "BayerGB8": ("uint8", 1, True),
    "RGB8": ("uint8", 3, False),
    "BGR8": ("uint8", 3, False),
    "RGBA8": ("uint8", 4, False),
}


def _aravis_prefix() -> str:
    """Where a locally unpacked Aravis is, if there is one.

    ``ARAVIS_PREFIX`` says so if it is set.  Otherwise the usual
    places are tried: an ``aravis-root`` beside the program or in the
    working directory, and the scratch directory the microscope's own
    tool uses.
    """
    if os.environ.get("ARAVIS_PREFIX"):
        return os.environ["ARAVIS_PREFIX"]
    package = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(os.getcwd(), "aravis-root"),
        os.path.join(os.path.dirname(package), "aravis-root"),
        ARAVIS_PREFIX,
    ]
    for candidate in candidates:
        if os.path.isdir(os.path.join(candidate, "usr", "lib")):
            return candidate
    return ARAVIS_PREFIX


def _import_aravis():
    """Import the Aravis GObject-introspection bindings.

    Aravis is the open-source GenICam / USB3 Vision stack, and it is
    driven from Python through its GObject-introspection bindings.  The
    binding (``gi``) comes from the system Python -- it is a compiled
    module and cannot be pip-installed on its own -- and the Aravis
    typelib and shared library come either from the system
    (``apt install libaravis-0.8-0 gir1.2-aravis-0.8``) or from a local
    prefix unpacked from the Debian packages.
    """
    import gi

    gi.require_version("Aravis", "0.8")
    from gi.repository import Aravis

    return Aravis


def maybe_reexec_for_aravis() -> bool:
    """Point the loader at a local Aravis and re-exec this process.

    The dynamic linker reads ``LD_LIBRARY_PATH`` once, when the process
    starts, so a running process cannot find a shared library that was
    unpacked after it began.  The only way in is to start again with the
    right environment -- which is what this does, replacing the process.
    It never returns if it re-executes.

    Returns True if no re-exec was needed (or could not be done), in
    which case the caller carries on and reports the import error itself.
    """
    if os.environ.get("_ARAVIS_BOOTSTRAPPED"):
        return True

    prefix = _aravis_prefix()
    libdir = os.path.join(prefix, "usr", "lib", "x86_64-linux-gnu")
    typelib = os.path.join(libdir, "girepository-1.0")
    if not os.path.isdir(typelib):
        return True

    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = libdir + ":" + env.get("LD_LIBRARY_PATH", "")
    env["GI_TYPELIB_PATH"] = typelib + ":" + env.get("GI_TYPELIB_PATH", "")
    env["_ARAVIS_BOOTSTRAPPED"] = "1"
    try:
        os.execve(sys.executable, _restart_argv(), env)
    except OSError as exc:  # pragma: no cover - the exec may be refused
        log.warning("could not re-exec with Aravis from %s: %s", prefix, exc)
    return True


def _restart_argv() -> list[str]:
    """The command line to start this program again with.

    A program started with ``python -m package.module`` has to be
    restarted the same way, or it is run as a loose script and its
    relative imports break.
    """
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if spec is not None and getattr(spec, "name", None):
        return [sys.executable, "-m", spec.name, *sys.argv[1:]]
    return [sys.executable, *sys.argv]


class AravisBackend(VideoSource):
    """Capture from a GenICam camera -- an Allied Vision Alvium -- via Aravis.

    Aravis is the open-source GenICam / USB3 Vision stack.  It is the
    stack the microscope's own capture tool uses, and the Alvium cameras
    (Alvium 1800 U-508c and the rest) speak it.

    Two things have to be got right, and both are done here:

    * **The buffer pool.**  ``create_stream()`` returns an *empty* stream.
      Aravis streams into buffers that the **application** supplies, so
      with none pushed in the stream thread underruns in a tight loop and
      never submits a single bulk transfer -- the camera appears to work
      while nothing reaches the wire.  A pool of buffers is pushed in
      here, and each one is handed back once it has been consumed.

    * **The link throughput limit.**  The camera throttles itself to the
      speed of whatever link it enumerated on, and remembers that value.
      A camera that has been moved from USB 2.0 to USB 3.x therefore
      stays pinned at 50 MB/s until the limit is rewritten, so it is
      raised here to what the link can carry.

    Parameters
    ----------
    device:
        The camera's id, or its index in the list of cameras.  ``None``
        means "the first one".
    exposure_us:
        Exposure time in microseconds.  ``None`` leaves the camera's
        setting alone.
    gain_db:
        Gain in dB.  ``None`` leaves it alone.
    roi:
        Region of interest as ``"WIDTHxHEIGHT"``.  ``None`` means the
        camera's own.
    fps:
        Frame rate.  ``None`` leaves the camera's setting alone.
    pixel_format:
        A GenICam pixel format name such as ``"Mono8"``.  ``None``
        leaves it alone.
    buffers:
        How many buffers to keep in the pool.
    throughput:
        The link throughput limit in MB/s: ``"auto"`` for the highest the
        link supports, ``"off"`` to leave the camera's value alone, or a
        number.
    timeout_ms:
        How long to wait for each frame.
    """

    def __init__(
        self,
        device: str | int | None = None,
        *,
        exposure_us: float | None = None,
        gain_db: float | None = None,
        roi: str | None = None,
        fps: float | None = None,
        pixel_format: str | None = None,
        buffers: int = 20,
        throughput: str | float = "auto",
        timeout_ms: int = 10000,
    ) -> None:
        self.device = device
        self.exposure_us = exposure_us
        self.gain_db = gain_db
        self.roi = roi
        self.fps = fps
        self.pixel_format = pixel_format
        self.buffers = max(2, buffers)
        self.throughput = throughput
        self.timeout_ms = timeout_ms
        self.name = f"aravis:{device if device is not None else 0}"
        self._aravis = None
        self._camera = None
        self._stream = None
        self._format = "Mono8"
        self._index = 0

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def list_cameras() -> list[dict]:
        """List the GenICam cameras the SDK can see."""
        Aravis = _import_aravis()
        Aravis.update_device_list()
        out = []
        for i in range(Aravis.get_n_devices()):
            out.append(
                {
                    "id": Aravis.get_device_id(i),
                    "vendor": Aravis.get_device_vendor(i),
                    "model": Aravis.get_device_model(i),
                    "serial": Aravis.get_device_serial_nbr(i),
                    "protocol": Aravis.get_device_protocol(i),
                }
            )
        return out

    def _resolve_device(self, Aravis) -> str:
        if self.device is None:
            return Aravis.get_device_id(0)
        if isinstance(self.device, int):
            cameras = [
                Aravis.get_device_id(i) for i in range(Aravis.get_n_devices())
            ]
            if not cameras:
                raise RuntimeError("no GenICam camera is connected")
            try:
                return cameras[self.device]
            except IndexError as exc:
                raise RuntimeError(
                    f"camera index {self.device} is out of range; "
                    f"{len(cameras)} camera(s) are connected"
                ) from exc
        return str(self.device)

    def _configure(self, camera) -> None:
        """Apply the settings that were asked for, tolerating the ones
        the camera may not have."""

        def try_set(feature, value, setter="set_string"):
            try:
                if not camera.is_feature_available(feature):
                    return False
                if not camera.is_feature_implemented(feature):
                    return False
                getattr(camera, setter)(feature, value)
                return True
            except Exception:
                return False

        try_set("AcquisitionMode", "Continuous")
        try_set("TriggerMode", "Off")

        if self.exposure_us is not None:
            try_set("ExposureTime", float(self.exposure_us), "set_float")
            try_set("ExposureTimeAuto", "Off")
        if self.gain_db is not None:
            if not try_set("Gain", float(self.gain_db), "set_float"):
                try_set("GainRaw", int(self.gain_db), "set_integer")

        if self.roi:
            try:
                w, h = (int(v) for v in self.roi.lower().split("x"))
                try:
                    camera.set_region(w, h, 0, 0)
                except Exception:
                    # Some firmware rejects the combined call; write the
                    # registers one at a time instead.
                    for feat, val in (("Width", w), ("Height", h),
                                      ("OffsetX", 0), ("OffsetY", 0)):
                        try_set(feat, val, "set_integer")
            except Exception as exc:
                log.warning("could not set the region of interest: %s", exc)

        if self.fps is not None:
            try:
                camera.set_frame_rate_enable(True)
                camera.set_frame_rate(float(self.fps))
            except Exception as exc:
                log.warning("could not set the frame rate: %s", exc)

        if self.pixel_format:
            try_set("PixelFormat", self.pixel_format)
            try:
                camera.set_pixel_format_from_string(self.pixel_format)
            except Exception:
                pass

    def _optimise_throughput(self, camera) -> None:
        """Push the camera's link throughput limit up to what the link has.

        The camera remembers the speed it enumerated at, so one that has
        been moved from USB 2.0 to USB 3.x is still pinned at 50 MB/s
        until this is rewritten.
        """
        mode = self.throughput
        if mode == "off":
            return
        if isinstance(mode, str):
            try:
                candidates = [int(float(mode) * 1e6)]
            except ValueError:
                candidates = [400_000_000, 300_000_000, 200_000_000, 100_000_000]
        else:
            candidates = [int(float(mode) * 1e6)]
        for value in candidates:
            try:
                camera.set_integer("DeviceLinkThroughputLimit", value)
                actual = camera.get_integer("DeviceLinkThroughputLimit")
                if actual:
                    log.info("link throughput limit: %d MB/s", actual // 1000000)
                    return
            except Exception:
                continue

    @staticmethod
    def _buffer_to_array(buf, format_name: str):
        """Turn an Aravis buffer into a BGR image OpenCV can work with."""
        import cv2
        import numpy as np

        width = buf.get_image_width()
        height = buf.get_image_height()
        dtype, bpp, is_bayer = _ARAVIS_PIXEL_FORMATS.get(
            format_name, ("uint8", 1, False)
        )
        data = buf.get_image_data()
        if isinstance(data, (bytes, bytearray)):
            arr = np.frombuffer(bytes(data), dtype=np.dtype(dtype))
        else:
            arr = np.frombuffer(data, dtype=np.dtype(dtype))

        if bpp > 1:
            arr = arr[: width * height * bpp].reshape(height, width, bpp)
        else:
            arr = arr[: width * height].reshape(height, width)

        if is_bayer:
            arr = cv2.cvtColor(arr, cv2.COLOR_BayerRG2BGR)
        elif bpp == 3:
            arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        elif bpp == 4:
            arr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
        elif bpp == 2 and format_name.startswith("Mono"):
            # 16-bit mono -> 8-bit, for the display and the encoder.
            arr = (arr >> 8).astype(np.uint8)
        return np.ascontiguousarray(arr)

    # -- the VideoSource interface ----------------------------------------- #
    def open(self) -> None:
        if self._camera is not None:
            return  # already open
        try:
            Aravis = _import_aravis()
        except (ImportError, ValueError) as exc:
            raise RuntimeError(
                "the Aravis GenICam bindings are not available. Install them "
                "with 'sudo apt install libaravis-0.8-0 gir1.2-aravis-0.8' and "
                "run the interpreter with the system site-packages (a venv "
                "made with --system-site-packages), or unpack them under "
                f"{_aravis_prefix()} and set ARAVIS_PREFIX to it"
            ) from exc

        Aravis.update_device_list()
        if Aravis.get_n_devices() == 0:
            raise RuntimeError(
                "no GenICam camera is connected. Check the USB cable and "
                "port -- a USB3 Vision camera needs a SuperSpeed port, and "
                "over USB 2.0 the control interface works while the stream "
                "does not"
            )

        device_id = self._resolve_device(Aravis)
        device = Aravis.open_device(device_id)
        if device is None:
            raise RuntimeError(f"could not open the camera {device_id!r}")
        camera = Aravis.Camera.new_with_device(device)
        if camera is None:
            raise RuntimeError("could not create the camera interface")

        self._configure(camera)
        self._optimise_throughput(camera)

        try:
            self._format = camera.get_pixel_format_as_string()
        except Exception:
            self._format = "Mono8"

        stream = camera.create_stream(None, None)
        if stream is None:
            raise RuntimeError("could not create the camera stream")
        payload = camera.get_payload()
        for _ in range(self.buffers):
            stream.push_buffer(Aravis.Buffer.new_allocate(payload))

        camera.start_acquisition()
        self._aravis = Aravis
        self._camera = camera
        self._stream = stream
        self._index = 0
        log.info("opened %s (%s, %s)", self.name, device_id, self._format)

    @property
    def size(self):
        """The size of the picture, as ``(width, height)``."""
        if self._camera is None:
            return None
        try:
            width = self._camera.get_integer("Width")
            height = self._camera.get_integer("Height")
        except Exception:  # pragma: no cover - not every camera says
            return None
        if not width or not height:
            return None
        return (int(width), int(height))

    def read(self) -> Frame | None:
        if self._stream is None:
            raise RuntimeError("video source is not open")
        buf = self._stream.timeout_pop_buffer(self.timeout_ms * 1000)
        if buf is None:
            return None
        try:
            image = self._buffer_to_array(buf, self._format)
        finally:
            # Hand the buffer back, or the pool runs dry and the stream
            # stops receiving frames.
            try:
                self._stream.push_buffer(buf)
            except Exception:  # pragma: no cover - the stream may be gone
                log.exception("could not return the buffer to the stream")
        frame = Frame(image=image, timestamp=time.time(), index=self._index)
        self._index += 1
        return frame

    def close(self) -> None:
        if self._camera is not None:
            try:
                self._camera.stop_acquisition()
            except Exception:  # pragma: no cover
                log.exception("could not stop the camera's acquisition")
            try:
                if self._stream is not None:
                    self._stream.timeout_pop_buffer(0)
            except Exception:  # pragma: no cover
                pass
        self._camera = None
        self._stream = None


# --------------------------------------------------------------------------- #
# A camera that is not there
# --------------------------------------------------------------------------- #


class SyntheticBackend(VideoSource):
    """A camera that is not there: the frames are drawn instead.

    It exists so the whole video path -- the live feed, the numbers
    written onto the frames and the recording -- can be exercised with no
    camera attached, which is the only way to develop it on a bench with
    no microscope.
    """

    def __init__(
        self,
        device: str | int = 0,
        *,
        width: int = 640,
        height: int = 480,
        fps: float = 25.0,
        label: str = "synthetic camera",
    ) -> None:
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.label = label
        self.name = f"synthetic:{device}"
        self._index = 0
        self._open = False

    def open(self) -> None:
        self._open = True
        log.info("opened %s (%dx%d)", self.name, self.width, self.height)

    @property
    def size(self):
        """The size of the picture, as ``(width, height)``."""
        return (self.width, self.height)

    def read(self) -> Frame | None:
        if not self._open:
            raise RuntimeError("video source is not open")
        time.sleep(1.0 / max(self.fps, 0.1))
        image = self._draw()
        frame = Frame(image=image, timestamp=time.time(), index=self._index)
        self._index += 1
        return frame

    def _draw(self):
        """A moving picture: a gradient with a box travelling across it."""
        import numpy as np

        h, w = self.height, self.width
        ys = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
        xs = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[:, :, 0] = ys
        frame[:, :, 1] = xs
        frame[:, :, 2] = ((ys.astype(np.uint16) // 2
                           + xs.astype(np.uint16) // 2) % 256).astype(np.uint8)

        # A box that travels, so a still frame is obviously a still frame.
        span = max(1, w // 6)
        x = int((self._index * 7) % max(1, w - span))
        frame[h // 4 : h // 4 + max(2, h // 6), x : x + span] = (255, 255, 255)
        return frame

    def close(self) -> None:
        self._open = False


# --------------------------------------------------------------------------- #
# Writing the numbers onto a frame
# --------------------------------------------------------------------------- #


def annotate(image, *, temperature_c=None, target_c=None, when=None):
    """Write the time and the temperature onto a frame.

    The **time goes in the lower left corner** and the **temperature in
    the upper right**, both with an outline so they read against any
    image.  They are burned into the frame rather than drawn over it in
    the browser, so a recording carries its own numbers and does not
    depend on a sidecar to be read.

    Parameters
    ----------
    image:
        The frame, as a BGR array.  It is modified in place.
    temperature_c:
        The temperature to write.  ``None`` writes nothing.
    target_c:
        The setpoint, written under the temperature when it is known.
    when:
        The time to write, as a UNIX timestamp.  ``None`` uses now.
    """
    if image is None:
        return image

    import cv2
    from datetime import datetime

    h, w = image.shape[:2]
    scale = max(0.4, min(2.0, h / 720.0))
    thickness = max(1, int(round(scale * 2)))
    font = cv2.FONT_HERSHEY_SIMPLEX
    margin = max(8, int(round(14 * scale)))
    white = (255, 255, 255)
    black = (0, 0, 0)

    def put(text, origin, colour=white, text_scale=None):
        """The text, with a dark outline behind it."""
        size = scale if text_scale is None else text_scale
        cv2.putText(image, text, origin, font, size, black, thickness + 2)
        cv2.putText(image, text, origin, font, size, colour, thickness)

    if when is None:
        when = time.time()

    # -- the time, lower left --------------------------------------------- #
    stamp = datetime.fromtimestamp(when).strftime("%H:%M:%S")
    (tw, th), _ = cv2.getTextSize(stamp, font, scale, thickness)
    put(stamp, (margin, h - margin))

    # -- the temperature, upper right --------------------------------------- #
    if temperature_c is not None:
        text = f"{temperature_c:.2f}"
        (tw, th), _ = cv2.getTextSize(text, font, scale, thickness)
        # The degree sign is a small ring: Hershey fonts have no glyph
        # for it, so it is drawn rather than typed.
        radius = max(2, int(round(th * 0.32)))
        gap = max(2, int(round(th * 0.18)))
        ring_w = 2 * radius + gap
        (cw, ch), _ = cv2.getTextSize("C", font, scale, thickness)
        total = tw + ring_w + cw

        x = w - margin - total
        y = margin + th
        put(text, (x, y))
        ring_x = x + tw + gap + radius
        ring_y = y - th // 2
        cv2.circle(image, (ring_x, ring_y), radius, black, thickness + 2)
        cv2.circle(image, (ring_x, ring_y), radius, white, thickness)
        put("C", (x + tw + ring_w, y))

        # -- the setpoint, under the temperature ---------------------------- #
        if target_c is not None:
            small_scale = scale * 0.7
            small = f"target {target_c:.1f} C"
            (sw, sh), _ = cv2.getTextSize(small, font, small_scale, thickness)
            put(
                small,
                (w - margin - sw, y + int(sh * 1.6)),
                colour=(160, 200, 255),
                text_scale=small_scale,
            )

    return image


BACKENDS = {
    "dshow": DirectShowBackend,
    "v4l2": V4L2Backend,
    "vimbax": VimbaXBackend,
    "aravis": AravisBackend,
    "synthetic": SyntheticBackend,
}


def open_video(backend: str = "auto", device: str | int = 0, **kwargs) -> VideoSource:
    """Open a video source.

    ``backend="auto"`` picks ``dshow`` on Windows and ``v4l2`` elsewhere.
    """
    if backend == "auto":
        import sys

        backend = "dshow" if sys.platform == "win32" else "v4l2"
    try:
        cls = BACKENDS[backend]
    except KeyError:
        raise ValueError(
            f"unknown backend {backend!r}; choose from {sorted(BACKENDS)}"
        ) from None
    src = cls(device, **kwargs)
    src.open()
    return src
