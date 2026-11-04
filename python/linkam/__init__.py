"""Python implementation of Linkam Linksys32 temperature control + video capture.

Reverse-engineered from the Linksys32 CD.  See ``PROTOCOL_NOTES.md`` for
what was recovered and what remains unverified.

Sub-modules
------------
``linkam.protocol``
    Linkam temperature programmer serial protocol.
``linkam.video``
    Video capture backends (DirectShow on Windows, V4L2 on Linux).
``linkam.experiment``
    Orchestration of a temperature ramp with simultaneous capture.
"""

from .experiment import Experiment, ExperimentResult, IsothermalRun
from .protocol import (
    ConnectionError_,
    LinkamProgrammer,
    ProgrammerNotRamping,
    ProgrammerState,
    ProtocolError,
    Telemetry,
    parse_telemetry,
)
from .video import Frame, VideoSource, open_video

__version__ = "0.1.0"

__all__ = [
    "Experiment",
    "ExperimentResult",
    "IsothermalRun",
    "LinkamProgrammer",
    "ProgrammerState",
    "ProtocolError",
    "ConnectionError_",
    "ProgrammerNotRamping",
    "Telemetry",
    "parse_telemetry",
    "Frame",
    "VideoSource",
    "open_video",
    "__version__",
]
