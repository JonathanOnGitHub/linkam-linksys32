"""Python implementation of Linkam Linksys32 temperature control + video capture.

Reverse-engineered from the Linksys32 CD.  See ``PROTOCOL_NOTES.md`` for
what was recovered and what remains unverified.

Sub-modules
------------
``linkam.protocol``
    Linkam temperature programmer protocol, and the command/telemetry
    vocabulary.
``linkam.transport``
    The physical-link abstraction (serial today, HID for the T95).
``linkam.hid``
    USB HID transport for the Linkam T95 (``16da:0002``).
``linkam.video``
    Video capture backends (DirectShow on Windows, V4L2 on Linux).
``linkam.experiment``
    Orchestration of a temperature ramp with simultaneous capture.
"""

from .experiment import Experiment, ExperimentResult, IsothermalRun
from .hid import HidLink, HidTransport, find_link
from .protocol import (
    ConnectionError_,
    HidProgrammer,
    LinkamProgrammer,
    ProgrammerNotRamping,
    ProgrammerState,
    ProtocolError,
    Telemetry,
    parse_telemetry,
)
from .transport import SerialTransport, Transport, TransportTimeout
from .video import Frame, VideoSource, open_video

__version__ = "0.1.0"

__all__ = [
    "Experiment",
    "ExperimentResult",
    "IsothermalRun",
    "LinkamProgrammer",
    "HidProgrammer",
    "HidTransport",
    "HidLink",
    "find_link",
    "ProgrammerState",
    "ProtocolError",
    "ConnectionError_",
    "ProgrammerNotRamping",
    "Telemetry",
    "parse_telemetry",
    "Transport",
    "SerialTransport",
    "TransportTimeout",
    "Frame",
    "VideoSource",
    "open_video",
    "__version__",
]
