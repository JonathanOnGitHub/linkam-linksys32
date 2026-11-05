#!/usr/bin/env python3
"""Read-only reconnaissance tool for the Linkam T95 USB HID link.

This is the tool to run when the T95 is plugged in for the first time.
It answers, in order:

1. Is the device present, and which hidraw node is it?
2. What is its HID report descriptor, and what does that descriptor say
   about the report IDs, sizes and usages?
3. Which report framing does the instrument actually answer?

Everything it does is read-only with respect to the instrument's state.
The only commands it sends are the stage-register query ``MA6``, which
reads a register and changes nothing.  It never sends ``Prog start``,
``prog ramp`` or any other command that could move the stage.

Usage
-----

::

    # wait up to 60 s for the device to appear, then probe it
    .venv/bin/python tools/t95_hid_probe.py 60

    # just dump the report descriptor
    .venv/bin/python tools/t95_hid_probe.py --descriptor-only
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from linkam.hid import (  # noqa: E402
    FRAMINGS,
    HidTransport,
    find_link,
    read_report_descriptor,
)


# The descriptor parser now lives in the library, because the transport
# needs it too: the report descriptor is the authoritative statement of how
# long an Output report is.  It is re-exported here so that
# ``from t95_hid_probe import ReportDescriptorParser`` keeps working.
from linkam.hid_descriptor import (  # noqa: E402,F401
    ReportDescriptorParser,
    describe_reports,
    report_sizes,
    uses_report_ids,
)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "wait",
        nargs="?",
        type=float,
        default=0.0,
        help="seconds to wait for the device to appear (default: 0)",
    )
    ap.add_argument(
        "--descriptor-only",
        action="store_true",
        help="dump the report descriptor and exit",
    )
    ap.add_argument(
        "--node",
        default=None,
        help="use this hidraw node instead of searching for the T95",
    )
    ap.add_argument(
        "--out",
        default="/tmp/t95_hid_desc.bin",
        help="where to write the raw report descriptor",
    )
    ap.add_argument(
        "--command",
        default="MA6",
        help="read-only query to use when probing framings (default: MA6)",
    )
    args = ap.parse_args(argv)

    print("Linkam T95 read-only HID probe")
    print(f"  looking for 16da:0002")

    node = args.node
    link = None
    if node is None:
        link = find_link(timeout=args.wait)
        if link is None:
            print()
            print("Device not attached; nothing to probe. Replug the T95 and rerun.")
            print("Pass a wait time in seconds as the first argument to block, "
                  "e.g. 30.")
            return 1
        node = link.node
        print(f"  found       : {node}  {link.name or 'Linkam T95'}")
        if link.uniq:
            print(f"  serial no.  : {link.uniq}")
    else:
        print(f"  using node  : {node}")

    # -- descriptor ------------------------------------------------------- #
    print()
    print("HID report descriptor")
    descriptor = read_report_descriptor(node)
    if not descriptor:
        print("  UNAVAILABLE — the device did not return a report descriptor.")
        print("  This is expected for some vendor-specific HID devices, and")
        print("  is also what happens when another driver holds the interface.")
        print("  The framing probe below does not depend on it.")
    else:
        print(f"  {len(descriptor)} bytes")
        parser = ReportDescriptorParser()
        parser.parse(descriptor)
        print()
        print(describe_reports(parser))
        if parser.collections:
            print()
            print("  collections:")
            for c in parser.collections:
                print(f"    type 0x{c['type']:02x} on usage page 0x{c['usage_page']:04x}")
        try:
            Path(args.out).write_bytes(descriptor)
            print()
            print(f"  written to {args.out}")
        except OSError as exc:
            print(f"  could not write {args.out}: {exc}")

    if args.descriptor_only:
        return 0

    # -- framing probe ---------------------------------------------------- #
    print()
    print("Framing probe (read-only)")
    print(f"  sending {args.command!r} under each candidate framing")
    print()

    winners = []
    write_errors: set[str] = set()
    for framing in FRAMINGS:
        transport = HidTransport(node, framing=framing)
        try:
            transport.open()
        except Exception as exc:
            print(f"  {framing:<17} OPEN FAILED: {exc}")
            write_errors.add(f"open: {exc}")
            continue
        try:
            try:
                transport.write_line(args.command, b"\r\n")
            except OSError as exc:
                # ETIMEDOUT means the device is not servicing its OUT
                # endpoint at all -- a different problem from "wrong
                # framing", and one the framing sweep cannot fix.
                print(f"  {framing:<17} WRITE FAILED: {exc}")
                write_errors.add(f"{exc.errno}: {exc.strerror}")
                continue
            try:
                line = transport.read_line(2.0)
            except Exception:
                line = None
            if line:
                print(f"  {framing:<17} RESPONSE: {line!r}")
                winners.append((framing, line))
            else:
                print(f"  {framing:<17} no response")
        finally:
            transport.close()

    print()
    if winners:
        print("FRAMING FOUND")
        for framing, line in winners:
            print(f"  {framing}: {line!r}")
        print()
        print("Use it like this:")
        best = winners[0][0]
        print(f"  .venv/bin/python -m linkam.cli probe --hid --hid-framing {best}")
        print(f"  .venv/bin/python -m linkam.cli run --hid --hid-framing {best} \\")
        print("      --target 40 --rate 5 --hold 60 --no-video")
        return 0

    # Distinguish "wrong framing" from "the device is not listening".
    timed_out = any("110" in e or "timed out" in e.lower() for e in write_errors)
    if timed_out:
        print("THE DEVICE IS NOT RESPONDING TO OUTPUT REPORTS.")
        print()
        print("The USB device is present and healthy -- its report descriptor")
        print("reads correctly and the IN endpoint works -- but every write to")
        print("the OUT endpoint times out (ETIMEDOUT). That is not a framing")
        print("problem: the firmware is not servicing the endpoint.")
        print()
        print("Things to try, in order:")
        print("  1. Power-cycle the stage: switch it off, wait 10 s, switch")
        print("     it on, then re-run this tool. The USB connection may")
        print("     survive a power cycle while the firmware does not.")
        print("  2. Unplug and replug the USB cable.")
        print("  3. Make sure the stage's own power supply is on and the")
        print("     unit is not in standby / bootloader mode.")
        print("  4. As a last resort, reset the USB device (needs root):")
        print("       sudo ./tools/reset_usb_device.sh")
        return 2

    print("NO RESPONSE under any framing.")
    print()
    print("Next steps:")
    print("  1. The command may need a different terminator, or a different")
    print("     read-only query.  Try --command 'prog' or --command 'MC'.")
    print("  2. The payload may be binary rather than ASCII; capture a raw")
    print("     exchange with the vendor software and a USB sniffer.")
    print("  3. The report descriptor may be needed to find the real")
    print("     report IDs; try stealing the interface from usbhid.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
