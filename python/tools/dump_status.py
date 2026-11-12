"""Dump the T95's raw status reply, field by field (read-only).

Sends one status poll (eCMDT95_GETLINK) and prints the raw blocks, the
reassembled payload and every float32 in it, so the field layout can be
checked against the values the CLI reports.
"""

from __future__ import annotations

import struct
import sys

from linkam.hid import HidTransport, find_link
from linkam.t95_protocol import parse_reply


def main() -> int:
    link = find_link()
    if link is None:
        print("T95 not found")
        return 1

    transport = HidTransport(link.node)
    transport.open()
    try:
        from linkam.t95_protocol import build_status_poll

        transport.write_blocks(build_status_poll())
        blocks = transport.read_reports(3, 3.0)
    finally:
        transport.close()

    if not blocks:
        print("no reply")
        return 1

    for i, b in enumerate(blocks):
        print(f"block[{i}] len={len(b)} : {b.hex()}")

    reply = parse_reply(blocks)
    print()
    print(f"command   : 0x{reply.command:04x}")
    print(f"length    : {reply.length}")
    print(f"param3/4  : {reply.param3} / {reply.param4}")
    print(f"payload   : {reply.payload.hex()}")
    print()

    names = {
        0: "HEATER1_TEMP  (temperature)",
        4: "channel 2     (second thermocouple)",
        8: "HEATER_RATE   (programmed rate)",
        12: "HEATER_SETPOINT (programmed setpoint)",
    }
    for off in sorted(names):
        if len(reply.payload) >= off + 4:
            (v,) = struct.unpack_from("<f", reply.payload, off)
            raw = reply.payload[off : off + 4].hex()
            print(f"  payload+{off:<3d} {names[off]:<40s} raw={raw}  value={v!r}")
        else:
            print(f"  payload+{off:<3d} {names[off]:<40s} <payload too short>")

    print()
    print("every float32 in the payload:")
    for off in range(0, len(reply.payload) - 3, 4):
        (v,) = struct.unpack_from("<f", reply.payload, off)
        print(f"  +{off:<3d} {reply.payload[off:off+4].hex()}  {v!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
