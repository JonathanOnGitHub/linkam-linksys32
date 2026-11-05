"""Transport abstraction for Linkam programmers.

The Linkam Linksys32 application layer is **line-oriented ASCII text**,
whatever the physical link underneath it.  This was established
statically from the vendor binaries (see ``../PROTOCOL_NOTES.md``):

* ``LinksysActXLib.ocx`` reads replies with VB's ``__vbaLineInputStr``;
* the telemetry frame is a plain string such as
  ``Temp 23.4 °C~Rate 10 °C/min~Limit 25.4 °C``;
* commands are ASCII keywords such as ``Prog start`` and
  ``prog ramp 1 rate 5 limit 40 time 60``.

Only the *framing* differs between links:

===============  ==========================================================
Link             Framing
===============  ==========================================================
RS-232 (legacy)  ASCII line, terminated CR / LF / CRLF, at a baud rate.
USB HID (T95)    ASCII line carried inside an HID report envelope.
===============  ==========================================================

A :class:`Transport` therefore has exactly two responsibilities:

``write_line(text)``
    Put one command on the wire.

``read_line(timeout)``
    Return the next complete payload line, or raise
    :class:`TimeoutError`.

Everything above this layer -- telemetry parsing, ramp sequencing, stall
detection, CSV output -- is transport agnostic and is shared between the
two links.  :class:`linkam.protocol.LinkamProgrammer` is written against
this interface, so the HID programmer is a few lines of code rather than
a parallel implementation.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

__all__ = [
    "Transport",
    "SerialTransport",
    "TransportTimeout",
    "CommandTooLong",
]


class TransportTimeout(TimeoutError):
    """No complete line arrived within the requested timeout."""


class CommandTooLong(ValueError):
    """A command does not fit in the link's fixed-size message envelope.

    Raised instead of silently truncating the command.  Truncation is
    not a safe default: a command cut short can still be a *valid*
    command with a different meaning -- ``limit 95`` cut to ``limit 9``
    asks the instrument to hold at 9 degrees -- and nothing downstream
    would notice the discrepancy.
    """


@runtime_checkable
class Transport(Protocol):
    """Minimal interface a Linkam programmer link must provide."""

    #: Human-readable description of the link, for logs and diagnostics.
    description: str

    def open(self) -> None:
        """Establish the link.  Raises on failure."""

    def close(self) -> None:
        """Tear the link down.  Must be safe to call more than once."""

    def write_line(self, text: str, terminator: bytes) -> None:
        """Send one command line.

        `terminator` is the line ending to append, e.g. ``b"\\r\\n"``.
        It is passed in rather than fixed so that the framing probe can
        try CR, LF and CRLF against the same transport.
        """

    def read_line(self, timeout: float) -> str:
        """Return the next complete line.

        Raises :class:`TransportTimeout` if nothing complete arrives within
        `timeout` seconds.  Partial data must not be returned.
        """


class SerialTransport:
    """RS-232 transport for the legacy Linksys32 serial link.

    This is a thin wrapper over pyserial and preserves the historical
    behaviour of :class:`linkam.protocol.LinkamProgrammer` exactly:
    one byte at a time, CR or LF terminates a line, empty lines are
    skipped, and the decoder is lenient (``ascii`` with ``replace``).
    """

    def __init__(self, port: str, baudrate: int = 9600, *, timeout: float = 2.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial = None
        self.description = f"serial {port}@{baudrate}"

    def open(self) -> None:
        import serial  # pyserial, imported lazily

        self._serial = serial.Serial(
            self.port, self.baudrate, timeout=0.2, write_timeout=self.timeout
        )
        log.info("opened %s at %d baud", self.port, self.baudrate)

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def write_line(self, text: str, terminator: bytes) -> None:
        if self._serial is None:
            raise ConnectionError("port is not open")
        payload = text.encode("ascii", "replace") + terminator
        log.debug("-> %r", payload)
        self._serial.write(payload)
        self._serial.flush()

    def read_line(self, timeout: float) -> str:
        if self._serial is None:
            raise ConnectionError("port is not open")
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self._serial.read(1)
            if not chunk:
                continue
            buf += chunk
            if chunk in (b"\r", b"\n"):
                if buf[-1:] == chunk and len(buf) == 1:
                    continue  # swallow a bare CR or LF
                line = buf[:-1].decode("ascii", "replace").strip()
                if line:
                    log.debug("<- %r", line)
                    return line
                buf.clear()
        raise TransportTimeout(f"no line within {timeout}s")
