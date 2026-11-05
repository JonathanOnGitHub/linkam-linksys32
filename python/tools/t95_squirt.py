#!/usr/bin/env python3
"""Raw 'squirt' probe for the Linkam T95 HID link.

The framing probe in ``t95_hid_probe.py`` sends a command through
:class:`linkam.hid.HidTransport` and reports the *decoded* answer.  When
nothing answers it is hard to tell whether the device was silent, the
command was wrong, or the transport mangled the bytes on the way out.

This tool goes underneath all of that.  It writes raw bytes to the hidraw
node and dumps whatever comes back as hex, so the exchange can be seen
directly.  It is the right tool when the instrument is present but the
high-level probe reports ``NO RESPONSE``.

Read-only by default: it only sends the commands you name, and the
built-in list contains register/telemetry queries.  It never sends
``Prog start``, ``prog ramp`` or any stage-motion command.

Usage
-----

::

    # sweep command x terminator x report size, dumping raw replies
    .venv/bin/python tools/t95_squirt.py

    # one specific command, one specific size
    .venv/bin/python tools/t95_squirt.py --commands MA6 --sizes 63 --terminators crlf

    # send arbitrary bytes (hex) to see what the device makes of them
    .venv/bin/python tools/t95_squirt.py --raw-hex '4d41360d0a'
"""

from __future__ import annotations

import argparse
import os
import select
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from linkam.hid import find_link  # noqa: E402

#: Terminators to try, mapped to the bytes actually written.
TERMINATORS = {
    "crlf": b"\r\n",
    "cr": b"\r",
    "lf": b"\n",
    "none": b"",
}

#: Commands that only *read* from the instrument.  `MA*` are stage
#: register reads, `MC`/`MF*`/`MS*` read stage state.  None of them can
#: start a ramp or move anything.
DEFAULT_COMMANDS = ("MA6", "MC", "MF1", "MSX")


def hexdump(data: bytes, *, width: int = 16) -> str:
    """Render bytes as hex + ASCII, the way ``hexdump -C`` would."""
    out = []
    for offset in range(0, len(data), width):
        chunk = data[offset : offset + width]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        hexpart = hexpart.ljust(width * 3 - 1)
        asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"  {offset:04x}  {hexpart}  |{asciipart}|")
    return "\n".join(out)


def read_any(fd: int, timeout: float) -> list[bytes]:
    """Collect every input report that arrives within `timeout` seconds."""
    reports: list[bytes] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return reports
        r, _, _ = select.select([fd], [], [], min(remaining, 0.1))
        if not r:
            continue
        try:
            data = os.read(fd, 64)
        except OSError as exc:
            if exc.errno == 11:  # EAGAIN
                continue
            raise
        if not data:
            return reports
        reports.append(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--node", default=None, help="hidraw node (default: autodetect)")
    ap.add_argument(
        "--commands",
        nargs="*",
        default=list(DEFAULT_COMMANDS),
        help="commands to send (default: %s)" % " ".join(DEFAULT_COMMANDS),
    )
    ap.add_argument(
        "--sizes",
        nargs="*",
        type=int,
        default=[63, 64],
        help="output report sizes to try (default: 63 64)",
    )
    ap.add_argument(
        "--terminators",
        nargs="*",
        default=list(TERMINATORS),
        choices=list(TERMINATORS),
        help="line terminators to try (default: all)",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=1.2,
        help="seconds to listen for a reply after each command (default: 1.2)",
    )
    ap.add_argument(
        "--raw-hex",
        default=None,
        help="send these raw bytes instead of a command; implies --sizes/--terminators "
        "are ignored and the bytes are sent verbatim at each size",
    )
    args = ap.parse_args(argv)

    node = args.node
    if node is None:
        link = find_link(timeout=5.0)
        if link is None:
            print("Linkam T95 not found on USB.")
            return 1
        node = link.node
        print(f"T95 at {node}  {link.name!r}  serial={link.uniq!r}")

    fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
    hits = 0
    try:
        if args.raw_hex is not None:
            payload = bytes.fromhex(args.raw_hex.replace(" ", ""))
            for size in args.sizes:
                padded = payload[:size].ljust(size, b"\x00")
                print(f"\n=== raw {payload!r} at {size} bytes ===")
                os.write(fd, padded)
                reports = read_any(fd, args.timeout)
                for rep in reports:
                    hits += 1
                    print(f"  reply ({len(rep)} bytes):")
                    print(hexdump(rep))
            return 0 if hits else 1

        for size in args.sizes:
            for term_name in args.terminators:
                term = TERMINATORS[term_name]
                for command in args.commands:
                    body = (command + term.decode("ascii")).encode("ascii")
                    padded = body[:size].ljust(size, b"\x00")
                    print(f"\n=== {command!r} + {term_name!r} at {size} bytes ===")
                    print(f"  tx: {hexdump(padded)}")
                    try:
                        os.write(fd, padded)
                    except OSError as exc:
                        print(f"  write failed: {exc}")
                        continue
                    reports = read_any(fd, args.timeout)
                    if not reports:
                        print("  (no reply)")
                        continue
                    for rep in reports:
                        hits += 1
                        print(f"  reply ({len(rep)} bytes):")
                        print(hexdump(rep))
                        text = rep.rstrip(b"\x00").decode("utf-8", "replace")
                        print(f"  as text: {text!r}")
    finally:
        os.close(fd)

    print()
    if hits:
        print(f"{hits} repli(es) received.")
        return 0
    print("No replies at all.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
