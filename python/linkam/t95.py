"""The Linkam T95's USB HID protocol.

This module is now a thin compatibility layer over
:mod:`linkam.t95_protocol`, which carries the protocol proper.

The protocol was recovered from **Linkam's own SDK**
(``libLinkamSDK.so``, the Linux build of the commercial Linkam SDK,
copyright 2018-2023, published in the ``DiamondLightSource/linkam3``
repository).  The SDK is unstripped and carries full DWARF debug
information, so the framing is read out of the shipped code rather than
guessed.

What the earlier, purely empirical notes got right
--------------------------------------------------

The status poll really is the only thing the instrument needs in order
to report its state, and the answer really is three 63-byte blocks
whose first byte is the block index.  The captured answer in
``tests/test_t95.py`` decodes exactly as the SDK says it should::

    01 03 | 00 03 | 88 00 | 00 00 | 00 00 | 3c 80 bb 41 | ...
    ^  ^     ^  ^     ^  ^           ^  ^     ^^^^^^^^^^^
    |  |     |  |     |  |           |  |     temperature
    |  |     |  |     |  |           |  +-- parameter 4
    |  |     |  |     |  |           +----- parameter 3
    |  |     |  |     |  +---------------- payload length = 136
    |  |     |  |     +------------------- command id = 0x0300 (GETLINK)
    |  |     |  +------------------------- command id, high byte
    |  |     +---------------------------- block count
    |  +-------------------------------------- block index
    +----------------------------------------- (block index)

What the earlier notes got wrong
--------------------------------

The earlier revision of this file claimed that "an output report is
accepted if and only if its first two bytes are equal".  That rule does
not hold for the protocol the vendor's own software uses, and it is not
consistent with the notes' own data (the same byte pair is accepted in
one experiment and rejected in another).  The real rule is the SDK's:

* byte 0 of a host-to-device block is a reserved ``0x00``;
* byte 1 is the 1-based block index;
* byte 2 is the number of blocks;
* bytes 3.. are the command stream.

Most likely the older rule came from a T95 running **older firmware**:
Linkam's own support notes say a T9X controller with firmware too old
for the current LINK release will not connect at all, and Linkam
publishes a bootloader specifically for updating it.

What is *not* known
-------------------

Nothing about the temperature-control commands: they are known now.
What is still open is whether the particular controller in hand runs
firmware new enough for this framing.
"""

from __future__ import annotations

from .t95_protocol import (
    BLOCK_SIZE,
    COMMAND_HEADER_SIZE,
    COMMAND_STREAM_SIZE,
    MULTI_COMMAND_SIZE,
    REPLY_SIZE,
    Cmd,
    MultiCommand,
    T95Reply,
    T95Status,
    build_command_frames,
    build_multi_command,
    build_rate_command,
    build_setpoint_command,
    build_start_heating_command,
    build_status_poll,
    build_stop_heating_command,
    parse_reply,
    parse_status,
)

__all__ = [
    "BLOCK_SIZE",
    "COMMAND_HEADER_SIZE",
    "COMMAND_STREAM_SIZE",
    "MULTI_COMMAND_SIZE",
    "REPLY_SIZE",
    "Cmd",
    "MultiCommand",
    "T95_REPORT_SIZE",
    "T95_STATUS_BLOCKS",
    "T95Reply",
    "T95Status",
    "build_command_frames",
    "build_multi_command",
    "build_rate_command",
    "build_setpoint_command",
    "build_start_heating_command",
    "build_status_poll",
    "build_stop_heating_command",
    "parse_reply",
    "parse_status",
]

#: Payload size of every T95 HID report, from the report descriptor
#: (Report Count 63, Report Size 8).  The device's *input* reports are
#: this long; its output reports are written as 64-byte blocks.
T95_REPORT_SIZE = REPLY_SIZE

#: How many blocks the device answers a status poll with.
T95_STATUS_BLOCKS = 3
