"""The Linkam T95/T96 USB HID wire protocol.

Everything in this module was recovered from **Linkam's own SDK**,
``libLinkamSDK.so`` (the Linux build of the commercial "Linkam SDK",
copyright 2018-2023), which was published in the
``DiamondLightSource/linkam3`` repository.  The SDK is *not stripped* and
carries full DWARF debug information, so the framing below is read
directly out of the shipped code rather than guessed.

The functions that matter, and where they live in the SDK:

============================================  =======================  ==================================
SDK function                                  address                 what it does
============================================  =======================  ==================================
``USBTxThread::sendPacket``                   0x185680                 writes 64-byte blocks with ``hid_write``
``USBTxThread::writePacket``                  0x18543c                writes one block header + payload
``USBTxThread::makeCommand``                  0x1854b6                splits a command into 64-byte blocks
``USBTxThread::sendCommand``                  0x18474a                queues a command
``USBRxThread::readPacket``                   0x1841ba                reads 63-byte reply blocks
``USBRxThread::readReply``                    0x1839b6                reassembles the reply
``Stage::setValue``                           0x1c0b44                sets the rate / the setpoint
``Stage::startHeating``                       0x1c1082                starts the ramp
``Stage::stopHeating``                        0x1c1186                stops the ramp
``DataPollThread::run``                       0x17c74c                polls the controller
``StandardStage::parseReply``                 0x1c27da                unpacks the status payload
``TemperatureControlledStage::getValue``      0x1c2f76                maps status fields to values
============================================  =======================  ==================================

Wire format
-----------

The link is **not** the ASCII, line-oriented protocol that Linksys32
uses over RS-232.  It is a binary block protocol carried in 64-byte
HID output reports.

**Host to device.**  One *command* is split into one or more 64-byte
blocks; each block is one ``hid_write``::

    offset  size  meaning
    ------  ----  ------------------------------------------------------
    0       1     reserved, always 0x00
    1       1     block index, 1-based
    2       1     number of blocks in this command
    3       2     command id, uint16 little endian
    5       2     payload length, uint16 little endian
    7       2     parameter 3, uint16 little endian
    9       2     parameter 4, uint16 little endian
    11      52    payload bytes 1..52
    63      1     padding

    Blocks 2..N carry 60 payload bytes each, at offset 3, so every block
    holds exactly 60 bytes of the "command stream" (the 8-byte header
    above followed by the payload).

**Device to host.**  The device answers with 63-byte blocks (the size
its HID report descriptor declares)::

    offset  size  meaning
    ------  ----  ------------------------------------------------------
    0       1     block index, 1-based
    1       1     number of blocks
    2       60    command stream: header as above, then payload
    62      1     padding

The only difference between the two directions is that the host's block
carries a leading ``0x00`` that the device's does not.

Setting the temperature
-----------------------

Setting a setpoint is a **single** command, ``eCMDT95_SETMULT``
(0x0301), whose payload is a 96-byte ``MultiCommand``::

    struct MultiCommand {          // 96 bytes
        uint16_t cmd[16];          // offset 0
        float    value[16];        // offset 32
    };

``cmd`` is a list of sub-commands and ``value`` their arguments; a zero
``cmd`` entry terminates the list.  The sub-commands that matter for
temperature control are::

    0x0380  eCMDT95MULTI_START     start the ramp
    0x0382  eCMDT95MULTI_SETRATE   value[0] = ramp rate in C/min
    0x0383  eCMDT95MULTI_SETLIMIT  value[0] = setpoint in C
    0x0384  eCMDT95MULTI_SETTIME   value[0] = hold time in seconds

This is exactly what the SDK's ``Stage::startHeating`` sends: rate,
limit and start in one atomic command, so the instrument can never act
on a half-applied programme.

Reading the temperature
-----------------------

The SDK polls with ``eCMDT95_GETLINK`` (0x0300) and no payload.  The
answer's payload is 136 bytes, laid out as::

    offset  type     meaning
    ------  -------  --------------------------------------------------
    0       float32  HEATER1_TEMP      -- the stage temperature, in C
    4       float32  second thermocouple channel
    8       float32  HEATER_RATE       -- the programmed rate, in C/min
    12      float32  HEATER_SETPOINT   -- the programmed setpoint, in C

Those offsets are the SDK's own: ``StandardStage::parseReply`` copies the
payload to ``stage + 0x80`` and ``TemperatureControlledStage::getValue``
reads ``stage + 0x80`` for ``HEATER1_TEMP``, ``stage + 0x88`` for
``HEATER_RATE`` and ``stage + 0x8c`` for ``HEATER_SETPOINT``.

This layout is what the earlier, purely empirical notes in
``PROTOCOL_NOTES.md`` §5.6 observed in the raw blocks (temperature at
block offset 10, rate at 18, limit at 22) -- the block offsets are the
payload offsets plus the 10 bytes of reply header.

Safety
------

Nothing in this module writes to the instrument; it only builds byte
strings.  Sending them is the caller's decision.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

__all__ = [
    "BLOCK_SIZE",
    "REPLY_SIZE",
    "COMMAND_STREAM_SIZE",
    "COMMAND_HEADER_SIZE",
    "MULTI_COMMAND_SIZE",
    "Cmd",
    "MultiCommand",
    "T95Reply",
    "T95Status",
    "build_command_frames",
    "build_multi_command",
    "build_rate_command",
    "build_setpoint_command",
    "build_start_heating_command",
    "build_stop_heating_command",
    "build_status_poll",
    "assemble_reply",
    "parse_reply",
    "parse_status",
    "status_from_reply",
]


# --------------------------------------------------------------------------- #
# Sizes
# --------------------------------------------------------------------------- #

#: Size of one HID output report the SDK writes (``hid_write(..., 64)``).
BLOCK_SIZE = 64

#: Size of one HID input report the device sends (its report descriptor
#: declares Report Count 63).
REPLY_SIZE = 63

#: Bytes of the "command stream" carried by each block.  The stream is the
#: 8-byte command header followed by the payload.
COMMAND_STREAM_SIZE = 60

#: Size of the command header inside the stream.
COMMAND_HEADER_SIZE = 8

#: Size of the ``MultiCommand`` payload structure.
MULTI_COMMAND_SIZE = 96

#: Offset of the first payload byte inside a host-to-device block.
_TX_PAYLOAD_OFFSET = 3

#: Offset of the first payload byte inside a device-to-host block.
_RX_STREAM_OFFSET = 2


# --------------------------------------------------------------------------- #
# Command ids  (from the SDK's DWARF enumerations)
# --------------------------------------------------------------------------- #


class Cmd:
    """The T95/T96 command ids.

    Named after the SDK's own enumerators so the two can be compared
    directly.  The ``eCMDT95*`` family is the temperature-programmer
    command set; the ``eCMDT95MULTI*`` family is the sub-command list
    carried inside a ``MultiCommand``.
    """

    # -- top level commands ------------------------------------------------
    GETLINK = 0x0300  # eCMDT95_GETLINK / eCMDT95_START -- the status poll
    SETMULT = 0x0301  # eCMDT95_SETMULT -- carry a MultiCommand
    SETRESET = 0x0302
    SETSTOP = 0x0303
    SETHEAT = 0x0304
    SETCOOL = 0x0305
    SETHOLD = 0x0306
    SETRESN = 0x0307
    SETMAINSFREQ = 0x0308

    # -- MultiCommand sub-commands ----------------------------------------
    MULTI_START = 0x0380
    MULTI_SETNONE = 0x0381
    MULTI_SETRATE = 0x0382
    MULTI_SETLIMIT = 0x0383
    MULTI_SETTIME = 0x0384
    MULTI_SETPULSE = 0x0385
    MULTI_SETLNPSPEED = 0x0388
    MULTI_SETRH = 0x039A
    MULTI_SETRHDRYTIME = 0x039B
    MULTI_SETRHSWAPTIME = 0x039C
    MULTI_SETRHTPIPE = 0x039D
    MULTI_SETRHTWATER = 0x039E


# --------------------------------------------------------------------------- #
# The MultiCommand payload
# --------------------------------------------------------------------------- #


@dataclass
class MultiCommand:
    """The payload of an ``eCMDT95_SETMULT`` command.

    Mirrors the SDK's ``LinkamSDK::MultiCommand``: 16 command ids
    followed by 16 float arguments.
    """

    entries: list[tuple[int, float]] = field(default_factory=list)

    def add(self, command: int, value: float = 0.0) -> "MultiCommand":
        self.entries.append((command, value))
        return self

    def to_bytes(self) -> bytes:
        """Serialise to the SDK's 96-byte layout."""
        if len(self.entries) > 16:
            raise ValueError("a MultiCommand holds at most 16 entries")
        out = bytearray(MULTI_COMMAND_SIZE)
        for index, (command, value) in enumerate(self.entries):
            struct.pack_into("<H", out, index * 2, command)
            struct.pack_into("<f", out, 32 + index * 4, value)
        return bytes(out)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "MultiCommand":
        """Parse a 96-byte payload back into entries."""
        out = cls()
        for index in range(16):
            (command,) = struct.unpack_from("<H", raw, index * 2)
            if command == 0:
                break
            (value,) = struct.unpack_from("<f", raw, 32 + index * 4)
            out.entries.append((command, value))
        return out


def build_multi_command(entries: list[tuple[int, float]]) -> bytes:
    """Build the 96-byte payload of an ``eCMDT95_SETMULT`` command."""
    return MultiCommand(entries=list(entries)).to_bytes()


# --------------------------------------------------------------------------- #
# Building host-to-device frames
# --------------------------------------------------------------------------- #


def build_command_frames(
    command: int,
    payload: bytes = b"",
    *,
    param3: int = 0,
    param4: int = 0,
) -> list[bytes]:
    """Build the 64-byte HID output reports for one command.

    Parameters
    ----------
    command:
        The command id, e.g. :attr:`Cmd.GETLINK`.
    payload:
        The command payload.  May be empty.
    param3, param4:
        The two 16-bit parameters that follow the payload length in the
        command header.  The SDK uses them for the heater channel and
        similar small selectors.

    Returns
    -------
    list of bytes
        One 64-byte report per block.  Write them to the hidraw node in
        order, one ``write()`` per report -- the SDK does exactly that
        (``USBTxThread::sendPacket``).

    Notes
    -----
    The block count is computed the way the SDK computes it:
    ``ceil((payload length + 8) / 60)``, and never less than one.
    """
    if len(payload) > 0xFFFF:
        raise ValueError("payload is too long for a 16-bit length field")
    if not 0 <= command <= 0xFFFF:
        raise ValueError("command id must fit in 16 bits")

    stream = struct.pack("<HHHH", command, len(payload), param3, param4) + payload
    blocks = max(1, math.ceil((len(payload) + COMMAND_HEADER_SIZE) / COMMAND_STREAM_SIZE))

    frames: list[bytes] = []
    for index in range(blocks):
        chunk = stream[index * COMMAND_STREAM_SIZE : (index + 1) * COMMAND_STREAM_SIZE]
        frame = bytearray(BLOCK_SIZE)
        frame[0] = 0x00  # reserved
        frame[1] = index + 1  # 1-based block index
        frame[2] = blocks  # number of blocks
        frame[_TX_PAYLOAD_OFFSET : _TX_PAYLOAD_OFFSET + len(chunk)] = chunk
        frames.append(bytes(frame))
    return frames


def build_status_poll() -> list[bytes]:
    """Build the frames that ask the T95 for its status.

    This is ``eCMDT95_GETLINK`` with an empty payload -- one 64-byte
    report.
    """
    return build_command_frames(Cmd.GETLINK)


def build_rate_command(rate_c_per_min: float) -> list[bytes]:
    """Build the frames that set the ramp rate.

    A ``SETMULT`` carrying a one-entry ``MultiCommand``:
    ``eCMDT95MULTI_SETRATE`` with the rate in C/min.
    """
    payload = build_multi_command([(Cmd.MULTI_SETRATE, rate_c_per_min)])
    return build_command_frames(Cmd.SETMULT, payload)


def build_setpoint_command(setpoint_c: float) -> list[bytes]:
    """Build the frames that set the setpoint.

    A ``SETMULT`` carrying a one-entry ``MultiCommand``:
    ``eCMDT95MULTI_SETLIMIT`` with the setpoint in C.  The instrument
    will *not* move to it until a start command follows -- this only
    arms the setpoint.
    """
    payload = build_multi_command([(Cmd.MULTI_SETLIMIT, setpoint_c)])
    return build_command_frames(Cmd.SETMULT, payload)


def build_start_heating_command(
    rate_c_per_min: float,
    setpoint_c: float,
) -> list[bytes]:
    """Build the frames that start a ramp to `setpoint_c` at `rate_c_per_min`.

    This reproduces the SDK's ``Stage::startHeating`` exactly: rate,
    limit and start go out as **one** command, so the instrument cannot
    act on a half-applied programme.
    """
    payload = build_multi_command(
        [
            (Cmd.MULTI_SETRATE, rate_c_per_min),
            (Cmd.MULTI_SETLIMIT, setpoint_c),
            (Cmd.MULTI_START, 0.0),
        ]
    )
    return build_command_frames(Cmd.SETMULT, payload)


def build_stop_heating_command() -> list[bytes]:
    """Build the frames that stop the ramp (``eCMDT95_SETSTOP``)."""
    return build_command_frames(Cmd.SETSTOP)


# --------------------------------------------------------------------------- #
# Parsing device-to-host replies
# --------------------------------------------------------------------------- #


@dataclass
class T95Reply:
    """One reassembled reply from the controller."""

    #: The command id the reply answers.
    command: int = 0

    #: The payload length the device reported.
    length: int = 0

    #: The two 16-bit parameters from the reply header.
    param3: int = 0
    param4: int = 0

    #: The payload.
    payload: bytes = b""

    #: The raw blocks, for forensics.
    blocks: list[bytes] = field(default_factory=list)

    @property
    def is_status(self) -> bool:
        """True if this is the answer to a status poll."""
        return self.command == Cmd.GETLINK


def assemble_reply(blocks: list[bytes]) -> T95Reply | None:
    """Assemble the device's reply blocks into a :class:`T95Reply`.

    Returns ``None`` when the blocks do not yet form a **complete**
    reply.

    Why the blocks have to be placed by index
    ------------------------------------------
    A reply is split into blocks that each carry 60 bytes of the command
    stream, and each block states its own index in byte 0 and the total
    number of blocks in byte 1.  The blocks need not arrive in order.

    Sorting them by index and concatenating their slices is *not*
    enough, because a **missing block 1 silently shifts the stream**.
    The reply to a status poll is three blocks, and a USB transfer can
    drop one; if block 1 is the one that goes missing, the header is
    then read out of block 2 -- which is all zeros -- and the reply
    decodes as "no command, no payload" rather than as a broken reply.
    That is exactly the failure seen on real hardware, where a poll
    occasionally answered with the block indices ``2, 3, 2`` and the
    temperature silently went missing.

    So the blocks are placed by index, and a reply is only accepted when
    every block from 1 to the stated count is present.
    """
    by_index: dict[int, bytes] = {}
    for block in blocks:
        if len(block) < 2:
            continue
        index = block[0]
        if index == 0:
            continue
        # First one wins: a duplicate means a leftover block from an
        # earlier reply, not a second copy of this one.
        by_index.setdefault(index, block)

    if not by_index:
        return None

    # The block count is in byte 1 of every block of a reply.
    count = by_index[min(by_index)][1]
    if count <= 0:
        count = len(by_index)

    if 1 not in by_index:
        return None
    if any(index not in by_index for index in range(1, count + 1)):
        return None

    stream = b"".join(
        by_index[index][_RX_STREAM_OFFSET : _RX_STREAM_OFFSET + COMMAND_STREAM_SIZE]
        for index in range(1, count + 1)
    )

    reply = T95Reply(blocks=list(blocks))
    if len(stream) < COMMAND_HEADER_SIZE:
        return reply

    reply.command, reply.length, reply.param3, reply.param4 = struct.unpack_from(
        "<HHHH", stream
    )
    reply.payload = stream[COMMAND_HEADER_SIZE : COMMAND_HEADER_SIZE + reply.length]
    return reply


def parse_reply(reports: list[bytes]) -> T95Reply:
    """Reassemble the device's reply blocks into a :class:`T95Reply`.

    `reports` is the list of raw 63-byte input reports, in the order
    they arrived.  Blocks are ordered by their own index byte rather than
    by arrival order, so a device that interleaves blocks still parses.
    """
    reply = assemble_reply(reports)
    if reply is not None:
        return reply

    # An incomplete set of blocks: report whatever can be read, which is
    # better than silently claiming there is no data at all.
    indexed: list[tuple[int, bytes]] = []
    for report in reports:
        if len(report) < 2:
            continue
        indexed.append((report[0], report))
    indexed.sort(key=lambda item: item[0])

    stream = b"".join(
        report[_RX_STREAM_OFFSET : _RX_STREAM_OFFSET + COMMAND_STREAM_SIZE]
        for _, report in indexed
    )
    reply = T95Reply(blocks=list(reports))
    if len(stream) < COMMAND_HEADER_SIZE:
        return reply

    reply.command, reply.length, reply.param3, reply.param4 = struct.unpack_from(
        "<HHHH", stream
    )
    reply.payload = stream[COMMAND_HEADER_SIZE : COMMAND_HEADER_SIZE + reply.length]
    return reply


def _f32(payload: bytes, offset: int) -> float | None:
    """Read a little-endian float32, or None if the payload is too short."""
    if len(payload) < offset + 4:
        return None
    (value,) = struct.unpack_from("<f", payload, offset)
    # Guard against an unpopulated field: an all-zero float is a
    # denormal that means "not set", not a real measurement.
    if abs(value) < 1e-30:
        return None
    return value


@dataclass
class T95Status:
    """The contents of a status reply."""

    #: Current temperature in degrees Celsius (payload offset 0).
    temperature_c: float | None = None

    #: The second thermocouple channel (payload offset 4).  Reads as
    #: noise when nothing is connected to it.
    sensor2_c: float | None = None

    #: The programmed ramp rate in C/min (payload offset 8).
    rate: float | None = None

    #: The programmed setpoint in C (payload offset 12).
    setpoint_c: float | None = None

    #: The raw blocks, for forensics.
    blocks: list[bytes] = field(default_factory=list)

    @property
    def block_types(self) -> list[int]:
        """The index byte of each block (1, 2, 3 ...)."""
        return [b[0] for b in self.blocks if b]


def parse_status(reports: list[bytes]) -> T95Status:
    """Decode the device's answer to a status poll.

    `reports` is the list of raw 63-byte input reports.
    """
    return status_from_reply(parse_reply(reports))


def status_from_reply(reply: T95Reply) -> T95Status:
    """Decode an assembled status reply into a :class:`T95Status`."""
    status = T95Status(blocks=list(reply.blocks))
    status.temperature_c = _f32(reply.payload, 0)
    status.sensor2_c = _f32(reply.payload, 4)
    status.rate = _f32(reply.payload, 8)
    status.setpoint_c = _f32(reply.payload, 12)
    return status
