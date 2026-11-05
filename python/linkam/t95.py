"""The Linkam T95's USB HID protocol.

What was measured against real hardware
---------------------------------------

The T95 is **not** the ASCII, line-oriented programmer that the
Linksys32 software drives over RS-232.  Over USB HID it speaks a
binary block protocol.  The observations that this module encodes:

* The device is **silent until written to**.  A passive listen produces
  no input reports at all.

* An output report is accepted if and only if **its first two bytes are
  equal**.  This was established by mutating one byte of a 63-byte
  report at a time: setting byte 0, 1 or 2 to a different value makes
  the device stop answering, while setting byte 3..62 to anything at
  all changes nothing.  Every equal-byte pair tried (00/00, 01/01,
  02/02, 03/03, 10/10, 41/41, 7f/7f, ff/ff, and the ASCII pairs
  ``MM``, ``??``) is answered; every unequal pair tried is ignored.

* The answer is always **three 63-byte blocks**, delivered together.
  The first byte of each block is its type: 1, 2, 3.  The second byte
  is 3 in all three.

* The answer does **not** depend on the rest of the request.  Every
  byte of the request past the first two is ignored by the device in
  this mode.  So this is a *status poll*, not a request/response
  exchange.

Block 1 (type 1) carries four little-endian float32 values::

    offset 10   temperature   -- confirmed: ~23.6 C in a lab, drifting
                                 smoothly by ~0.01 C per minute
    offset 14   second sensor -- unconnected channel: noise, +/-1.5 C
                                 between polls
    offset 18   constant 0.70
    offset 22   constant 0.35

The last two are read as ``rate`` and ``limit`` by analogy with the
Linksys32 telemetry vocabulary (``Temp``, ``Temp2``, ``Rate``,
``Limit``), but that is **not confirmed** -- they do not move, so it
has not been possible to tell what they are.  They are named
provisionally.

Blocks 2 and 3 are almost entirely zero, with one flag byte in block 2
at offset 34 and a constant 0x48 at offset 51.

What is *not* known
-------------------

The command frames that change the setpoint have not been found.  The
Linksys32 ASCII vocabulary (``Prog rate 5``, ``prog ramp 1 rate 5 limit
30 time 60``, ``MA6``, ...) has no effect whatsoever on this link: it
is neither acknowledged nor does it change any status field.  The
device's command frames are therefore a different, still-unidentified
format.

Safety
------

Nothing in this module can start a ramp.  The only frame it writes is
the status poll.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

__all__ = [
    "T95_REPORT_SIZE",
    "T95_STATUS_BLOCKS",
    "T95Status",
    "build_status_poll",
    "parse_status",
]


#: Payload length of every T95 HID report, from the report descriptor
#: (Report Count 63, Report Size 8).
T95_REPORT_SIZE = 63

#: How many blocks the device answers a status poll with.
T95_STATUS_BLOCKS = 3

#: Offsets of the float32 values inside block 1.
_OFFSET_TEMPERATURE = 10
_OFFSET_SENSOR2 = 14
_OFFSET_RATE = 18
_OFFSET_LIMIT = 22


def build_status_poll(
    marker: int = 0x00,
    *,
    size: int = T95_REPORT_SIZE,
) -> bytes:
    """Build the output report that asks the T95 for its status.

    The device accepts the frame if and only if its first two bytes are
    equal, so the frame is `marker` repeated at offset 0 and 1 with
    the rest zero.  The value of `marker` does not matter -- every
    equal-byte pair tested is answered -- but it must be equal.

    The frame carries no instruction: the T95 treats any accepted frame
    as a poll and replies with its status.
    """
    frame = bytearray(size)
    frame[0] = marker
    frame[1] = marker
    return bytes(frame)


def _f32(block: bytes, offset: int) -> float | None:
    """Read a little-endian float32, or None if the block is too short."""
    if len(block) < offset + 4:
        return None
    (value,) = struct.unpack_from("<f", block, offset)
    # Guard against an unpopulated field: an all-zero float is a
    # denormal that means "not set", not a real measurement.
    if abs(value) < 1e-30:
        return None
    return value


@dataclass
class T95Status:
    """One status poll's answer from the T95."""

    #: Current temperature in degrees Celsius (block 1, offset 10).
    temperature_c: float | None = None

    #: A second temperature channel (block 1, offset 14).  Reads as
    #: noise when nothing is connected to it.
    sensor2_c: float | None = None

    #: Block 1, offset 18.  Provisional name: read as "rate" by
    #: analogy with the Linksys32 vocabulary, but it does not move, so
    #: it is not confirmed.
    rate: float | None = None

    #: Block 1, offset 22.  Provisional name, for the same reason.
    limit: float | None = None

    #: The flag byte in block 2 (offset 34).
    block2_flags: int | None = None

    #: The constant byte in block 2 (offset 51).
    block2_marker: int | None = None

    #: The raw blocks, for forensics.
    blocks: list[bytes] = field(default_factory=list)

    @property
    def block_types(self) -> list[int]:
        """The type byte of each block (1, 2, 3 for the T95)."""
        return [b[0] for b in self.blocks if b]


def parse_status(blocks: list[bytes]) -> T95Status:
    """Turn the T95's answer to a status poll into a :class:`T95Status`.

    `blocks` is the list of raw 63-byte input reports, in the order they
    arrived.  Blocks are identified by their type byte rather than by
    position, so a device that sends them in another order still
    parses.
    """
    status = T95Status(blocks=list(blocks))

    for block in blocks:
        if not block:
            continue
        kind = block[0]
        if kind == 1:
            status.temperature_c = _f32(block, _OFFSET_TEMPERATURE)
            status.sensor2_c = _f32(block, _OFFSET_SENSOR2)
            status.rate = _f32(block, _OFFSET_RATE)
            status.limit = _f32(block, _OFFSET_LIMIT)
        elif kind == 2:
            if len(block) > 34:
                status.block2_flags = block[34]
            if len(block) > 51:
                status.block2_marker = block[51]
    return status
