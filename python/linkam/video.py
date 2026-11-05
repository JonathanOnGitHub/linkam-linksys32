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
    "open_video",
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


BACKENDS = {
    "dshow": DirectShowBackend,
    "v4l2": V4L2Backend,
    "vimbax": VimbaXBackend,
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
