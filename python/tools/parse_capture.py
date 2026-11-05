#!/usr/bin/env python3
"""Extract the T95's command frames from a USB capture.

The T95's command frame format is the one thing still missing (see
``PROTOCOL_NOTES.md`` §6, question 10).  Capturing what Linkam's own
control software sends would settle it.

This reads a capture and prints the HID reports in each direction, so
the host-to-device reports -- the commands -- can be read directly.

Input
-----

Either:

1. The output of ``tshark`` on a capture taken with USBPcap::

       tshark -r capture.pcapng \
         -Y "usb.idVendor == 0x16da && usb.idProduct == 0x0002" \
         -T fields -e usb.endpoint_address -e usb.capdata

2. A plain text file with one report per line, as ``<endpoint hex>
   <payload hex>``.

Endpoint addresses
------------------

``0x01`` is the OUT endpoint (host -> device) and ``0x81`` is the IN
endpoint (device -> host).  The OUT reports are the commands.

Usage
-----

::

    .venv/bin/python tools/parse_capture.py tshark.txt
    .venv/bin/python tools/parse_capture.py tshark.txt --out frames.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: The T95's OUT endpoint (host -> device).
OUT_ENDPOINT = 0x01

#: The T95's IN endpoint (device -> host).
IN_ENDPOINT = 0x81


def parse_tshark_line(line: str):
    """Parse one ``tshark -T fields`` row into (endpoint, payload)."""
    parts = line.split("\t")
    if len(parts) < 2:
        return None
    endpoint_text = parts[0].strip()
    payload_text = parts[1].strip().replace(":", "")
    try:
        endpoint = int(endpoint_text, 16)
    except ValueError:
        return None
    try:
        payload = bytes.fromhex(payload_text)
    except ValueError:
        return None
    return endpoint, payload


def load(path: Path):
    """Yield (endpoint, payload) pairs from the capture file."""
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = parse_tshark_line(line)
        if parsed is None:
            # Maybe a plain "<endpoint> <payload>" pair, space separated.
            fields = line.split()
            if len(fields) == 2:
                try:
                    endpoint = int(fields[0], 16)
                    payload = bytes.fromhex(fields[1].replace(":", ""))
                except ValueError:
                    continue
                parsed = (endpoint, payload)
        if parsed is not None:
            yield parsed


def hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for offset in range(0, len(data), width):
        chunk = data[offset : offset + width]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"    {offset:04x}  {hexpart:<47}  |{asciipart}|")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("capture", help="the tshark output, or a plain text capture")
    ap.add_argument("--out", default=None, help="write the frames to this file too")
    ap.add_argument(
        "--only",
        choices=("out", "in", "both"),
        default="both",
        help="which direction to print (default: both)",
    )
    args = ap.parse_args(argv)

    path = Path(args.capture)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1

    out_frames = []
    in_frames = []
    for endpoint, payload in load(path):
        if endpoint == OUT_ENDPOINT:
            out_frames.append(payload)
        elif endpoint == IN_ENDPOINT:
            in_frames.append(payload)

    if args.only in ("out", "both"):
        print(f"=== {len(out_frames)} host -> device report(s) (the commands) ===")
        for i, frame in enumerate(out_frames):
            print(f"  [{i}] {len(frame)} bytes")
            print(hexdump(frame))

    if args.only in ("in", "both"):
        print(f"\n=== {len(in_frames)} device -> host report(s) (the answers) ===")
        for i, frame in enumerate(in_frames[:40]):
            print(f"  [{i}] {len(frame)} bytes")
            print(hexdump(frame))
        if len(in_frames) > 40:
            print(f"  ... and {len(in_frames) - 40} more")

    if args.out:
        Path(args.out).write_text(
            "\n".join(frame.hex() for frame in out_frames) + "\n"
        )
        print(f"\nwrote {len(out_frames)} host->device frames to {args.out}")

    if not out_frames:
        print(
            "\nNo host->device reports found.  If the capture was taken with "
            "USBPcap, check that the T95 was plugged into the machine running "
            "the capture, and that the capture was taken while the software "
            "was actually sending a command."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
