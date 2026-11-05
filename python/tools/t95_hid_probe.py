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


# --------------------------------------------------------------------------- #
# A minimal HID report-descriptor parser
# --------------------------------------------------------------------------- #


class ReportDescriptorParser:
    """Parse a HID report descriptor into a description of its reports.

    This is deliberately small -- it understands enough to tell us the
    report IDs, the report sizes and the usage codes, which is what we
    need in order to work out how the ASCII payload is framed.

    A note on the item encoding: HID short items are
    ``(prefix << 4) | tag`` where the prefix gives the data size and the
    tag is interpreted according to the item *type* -- global, local or
    main.  The numeric tag values therefore overlap between the three
    namespaces (a global "Report Size" is 0x7, a local "Usage" is 0x0,
    and a main "Input" is 0x8), so the parser has to keep track of
    which namespace it is currently in.  That is what the ``_state``
    machine below does.
    """

    # Item types, encoded in bits 2-3 of the prefix byte.
    TYPE_MAIN = 0
    TYPE_GLOBAL = 1
    TYPE_LOCAL = 2

    # Main item tags.
    MAIN_INPUT = 0x8
    MAIN_OUTPUT = 0x9
    MAIN_COLLECTION = 0xA
    MAIN_FEATURE = 0xB
    MAIN_END_COLLECTION = 0xC

    # Global item tags.
    GLOBAL_USAGE_PAGE = 0x0
    GLOBAL_LOGICAL_MIN = 0x1
    GLOBAL_LOGICAL_MAX = 0x2
    GLOBAL_PHYSICAL_MIN = 0x3
    GLOBAL_PHYSICAL_MAX = 0x4
    GLOBAL_UNIT_EXPONENT = 0x5
    GLOBAL_UNIT = 0x6
    GLOBAL_REPORT_SIZE = 0x7
    GLOBAL_REPORT_ID = 0x8
    GLOBAL_REPORT_COUNT = 0x9
    GLOBAL_PUSH = 0xA
    GLOBAL_POP = 0xB

    # Local item tags.
    LOCAL_USAGE = 0x0
    LOCAL_USAGE_MIN = 0x1
    LOCAL_USAGE_MAX = 0x2

    def __init__(self) -> None:
        self.usage_page = 0
        self.report_size = 0
        self.report_count = 0
        self.report_id: int | None = None
        self.logical_min = 0
        self.logical_max = 0
        self._global_stack: list[dict] = []
        self._local_usages: list[int] = []
        self._local_usage_min: int | None = None
        self._local_usage_max: int | None = None
        self._bit_offset = 0
        self.reports: list[dict] = []
        self.collections: list[dict] = []
        self._collection_stack: list[dict] = []
        self._state = "global"

    # -- helpers ---------------------------------------------------------- #
    @staticmethod
    def _signed(value: int) -> int:
        return value - 0x100 if value & 0x80 else value

    def _new_report(self, kind: int) -> dict:
        entry = {
            "kind": kind,
            "report_id": self.report_id,
            "bit_offset": self._bit_offset,
            "count": self.report_count,
            "size": self.report_size,
            "usages": list(self._local_usages),
            "usage_min": self._local_usage_min,
            "usage_max": self._local_usage_max,
            "logical_min": self.logical_min,
            "logical_max": self.logical_max,
            "usage_page": self.usage_page,
        }
        self.reports.append(entry)
        self._bit_offset += self.report_size * self.report_count
        return entry

    def _clear_local(self) -> None:
        self._local_usages = []
        self._local_usage_min = None
        self._local_usage_max = None

    # -- parsing ---------------------------------------------------------- #
    def parse(self, data: bytes) -> None:
        """Walk the descriptor.

        HID short items are ``(prefix << 4) | tag``.  The tag namespace
        depends on context:

        * tags 0x8..0xC are always **main** items (Input, Output,
          Collection, Feature, End Collection);
        * tags 0x0..0xB are **global** items when they appear before any
          main item, or between main items;
        * after a main item, tags 0x0..0x7 are **local** items.

        The only genuinely ambiguous tags are 0x1 and 0x2, which are
        Logical Min/Max globally and Usage Min/Max locally.  They are
        resolved by whether a main item has been seen yet.
        """
        pos = 0
        n = len(data)

        while pos < n:
            head = data[pos]
            pos += 1
            if head == 0xFE:  # long item
                if pos >= n:
                    break
                data_size = data[pos]
                pos += 1
                pos += data_size
                continue

            # A HID short item is encoded as
            #
            #     first byte = (tag << 4) | (type << 2) | size
            #
            # where `type` is 0=main, 1=global, 2=local, and `size` is
            # 0, 1, 2 or 3 meaning 0, 1, 2 or 4 data bytes.
            tag = (head >> 4) & 0x0F
            item_type = (head >> 2) & 0x03
            size = head & 0x03
            if size == 3:
                size = 4

            if pos + size > n:
                break
            raw = data[pos : pos + size]
            pos += size

            value = 0
            for i, byte in enumerate(raw):
                value |= byte << (8 * i)

            signed = self._signed(value) if size and (raw[-1] & 0x80) else value

            if item_type == self.TYPE_MAIN:
                self._handle_main(tag, value)
            elif item_type == self.TYPE_GLOBAL:
                self._handle_global(tag, value, signed)
            else:
                self._handle_local(tag, value)

    def _handle_main(self, tag: int, value: int) -> None:
        if tag in (self.MAIN_INPUT, self.MAIN_OUTPUT, self.MAIN_FEATURE):
            self._new_report(tag)
            self._clear_local()
        elif tag == self.MAIN_COLLECTION:
            coll = {
                "type": value,
                "usage_page": self.usage_page,
                "usages": list(self._local_usages),
                "usage_min": self._local_usage_min,
                "usage_max": self._local_usage_max,
            }
            self.collections.append(coll)
            self._collection_stack.append(coll)
            # A Usage declared before a Collection belongs to that
            # Collection, not to the reports inside it.  Clear the local
            # state so it does not leak into the first report.
            self._clear_local()
        elif tag == self.MAIN_END_COLLECTION:
            if self._collection_stack:
                self._collection_stack.pop()

    def _handle_global(self, tag: int, value: int, signed: int) -> None:
        if tag == self.GLOBAL_USAGE_PAGE:
            self.usage_page = value
        elif tag == self.GLOBAL_LOGICAL_MIN:
            self.logical_min = signed
        elif tag == self.GLOBAL_LOGICAL_MAX:
            self.logical_max = signed
        elif tag == self.GLOBAL_REPORT_SIZE:
            self.report_size = value
        elif tag == self.GLOBAL_REPORT_COUNT:
            self.report_count = value
        elif tag == self.GLOBAL_REPORT_ID:
            self.report_id = value
        elif tag == self.GLOBAL_PUSH:
            self._global_stack.append(
                {
                    "usage_page": self.usage_page,
                    "logical_min": self.logical_min,
                    "logical_max": self.logical_max,
                    "report_size": self.report_size,
                    "report_count": self.report_count,
                    "report_id": self.report_id,
                }
            )
        elif tag == self.GLOBAL_POP:
            if self._global_stack:
                saved = self._global_stack.pop()
                # Pop must restore the global item state that was in
                # effect when the matching Push was seen.
                self.usage_page = saved["usage_page"]
                self.logical_min = saved["logical_min"]
                self.logical_max = saved["logical_max"]
                self.report_size = saved["report_size"]
                self.report_count = saved["report_count"]
                self.report_id = saved["report_id"]

    def _handle_local(self, tag: int, value: int) -> None:
        if tag == self.LOCAL_USAGE:
            self._local_usages.append(value)
        elif tag == self.LOCAL_USAGE_MIN:
            self._local_usage_min = value
        elif tag == self.LOCAL_USAGE_MAX:
            self._local_usage_max = value


_KIND_NAMES = {0x8: "INPUT", 0x9: "OUTPUT", 0xB: "FEATURE"}


def describe_reports(parser: ReportDescriptorParser) -> str:
    """Render the parsed report table."""
    lines = []
    if not parser.reports:
        return "  (no input/output/feature items found)"

    lines.append(
        f"  {'kind':<8} {'id':>4} {'bitoff':>7} {'count':>6} {'size':>5} "
        f"{'bits':>6}  usages"
    )
    for r in parser.reports:
        rid = "--" if r["report_id"] is None else f"0x{r['report_id']:02x}"
        bits = r["count"] * r["size"]
        usage_parts = []
        if r["usages"]:
            usage_parts.append(",".join(f"0x{u:04x}" for u in r["usages"]))
        if r["usage_min"] is not None:
            hi = r["usage_max"] if r["usage_max"] is not None else r["usage_min"]
            usage_parts.append(f"0x{r['usage_min']:04x}-0x{hi:04x}")
        usages = ";".join(usage_parts) if usage_parts else "-"
        lines.append(
            f"  {_KIND_NAMES.get(r['kind'], '?'):<8} {rid:>4} "
            f"{r['bit_offset']:>7} {r['count']:>6} {r['size']:>5} "
            f"{bits:>6}  {usages}"
        )
    return "\n".join(lines)


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
    for framing in FRAMINGS:
        transport = HidTransport(node, framing=framing)
        try:
            transport.open()
        except Exception as exc:
            print(f"  {framing:<17} OPEN FAILED: {exc}")
            continue
        try:
            transport.write_line(args.command, b"\r\n")
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
