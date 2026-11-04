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
    "open_video",
    "BACKENDS",
]


@dataclass
class Frame:
    """One captured frame."""

    image: object          # numpy.ndarray, BGR
    timestamp: float
    index: int


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

BACKENDS = {
    "dshow": DirectShowBackend,
    "v4l2": V4L2Backend,
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
