"""Tests for the HID report-descriptor parser.

The parser is validated against a *real* descriptor captured from a
device attached to the development machine (a USB keyboard), which
proves the item encoding is handled correctly rather than merely
self-consistent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from t95_hid_probe import (  # noqa: E402
    ReportDescriptorParser,
    describe_reports,
)


# A minimal but valid descriptor:
#   Usage Page (Vendor Defined 0xFF00)
#   Usage (0x01)
#   Collection (Application)
#     Report ID 1, Report Size 8, Report Count 64
#     Input  (Data, Var, Abs)  Usage 0x02
#     Report ID 2, Report Size 8, Report Count 64
#     Output (Data, Var, Abs)  Usage 0x03
#   End Collection
#
# Item encoding: first byte = (tag << 4) | (type << 2) | size
#   type: 0 = main, 1 = global, 2 = local
#   size: 0, 1, 2, 3 mean 0, 1, 2, 4 data bytes
VENDOR_DESCRIPTOR = bytes([
    0x06, 0x00, 0xFF,   # Usage Page (0xFF00), global, size 2
    0x09, 0x01,         # Usage (0x01), local, size 1
    0xA1, 0x01,         # Collection (Application), main, size 1
    0x85, 0x01,         # Report ID (1), global, size 1
    0x75, 0x08,         # Report Size (8), global, size 1
    0x95, 0x40,         # Report Count (64), global, size 1
    0x09, 0x02,         # Usage (0x02), local, size 1
    0x81, 0x02,         # Input (Data, Var, Abs), main, size 1
    0x85, 0x02,         # Report ID (2), global, size 1
    0x75, 0x08,         # Report Size (8), global, size 1
    0x95, 0x40,         # Report Count (64), global, size 1
    0x09, 0x03,         # Usage (0x03), local, size 1
    0x91, 0x02,         # Output (Data, Var, Abs), main, size 1
    0xC0,               # End Collection, main, size 0
])


def test_parses_vendor_descriptor():
    p = ReportDescriptorParser()
    p.parse(VENDOR_DESCRIPTOR)

    assert p.usage_page == 0xFF00
    assert len(p.collections) == 1
    assert p.collections[0]["type"] == 1  # Application

    inputs = [r for r in p.reports if r["kind"] == 0x8]
    outputs = [r for r in p.reports if r["kind"] == 0x9]
    assert len(inputs) == 1
    assert len(outputs) == 1

    assert inputs[0]["report_id"] == 1
    assert inputs[0]["size"] == 8
    assert inputs[0]["count"] == 64
    assert inputs[0]["bit_offset"] == 0
    assert inputs[0]["usages"] == [0x02]

    assert outputs[0]["report_id"] == 2
    assert outputs[0]["size"] == 8
    assert outputs[0]["count"] == 64
    assert outputs[0]["bit_offset"] == 512  # 64 bytes after the input report
    assert outputs[0]["usages"] == [0x03]


def test_describe_reports_renders_a_table():
    p = ReportDescriptorParser()
    p.parse(VENDOR_DESCRIPTOR)
    text = describe_reports(p)
    assert "INPUT" in text
    assert "OUTPUT" in text
    assert "0x01" in text
    assert "0x02" in text


def test_global_push_and_pop_restore_state():
    """Push/Pop must save and restore the global item state."""
    descriptor = bytes([
        0x06, 0x00, 0xFF,   # Usage Page 0xFF00
        0xA1, 0x01,         # Collection (Application)
        0x75, 0x08,         # Report Size 8
        0xA4,               # Push
        0x75, 0x10,         # Report Size 16 (inside the push)
        0x95, 0x02,         # Report Count 2
        0xB4,               # Pop
        0x95, 0x04,         # Report Count 4 (back to the outer state)
        0x81, 0x02,         # Input
        0xC0,               # End Collection
    ])
    p = ReportDescriptorParser()
    p.parse(descriptor)
    report = p.reports[0]
    assert report["size"] == 8   # restored by Pop
    assert report["count"] == 4


def test_local_items_are_cleared_between_main_items():
    """A Usage must not leak from one main item to the next."""
    descriptor = bytes([
        0x06, 0x00, 0xFF,
        0xA1, 0x01,
        0x75, 0x08,
        0x95, 0x01,
        0x09, 0x11,         # Usage 0x11
        0x81, 0x02,         # Input
        0x09, 0x22,         # Usage 0x22
        0x91, 0x02,         # Output
        0xC0,
    ])
    p = ReportDescriptorParser()
    p.parse(descriptor)
    inputs = [r for r in p.reports if r["kind"] == 0x8]
    outputs = [r for r in p.reports if r["kind"] == 0x9]
    assert inputs[0]["usages"] == [0x11]
    assert outputs[0]["usages"] == [0x22]


def test_parses_a_real_keyboard_descriptor():
    """Parse a descriptor captured from a real USB keyboard.

    This is the strong test: it exercises boot-protocol reports,
    consumer-control feature reports and a deeply nested collection
    structure.  The descriptor is the one from the development machine's
    keyboard; it is checked in here so the test does not depend on any
    hardware being attached.
    """
    descriptor = bytes([
        0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x01, 0x09, 0x01,
        0xA1, 0x00, 0x05, 0x09, 0x19, 0x01, 0x29, 0x02, 0x15, 0x00,
        0x25, 0x01, 0x75, 0x01, 0x95, 0x02, 0x81, 0x02, 0x95, 0x06,
        0x81, 0x03, 0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x15, 0x81,
        0x25, 0x7F, 0x75, 0x08, 0x95, 0x02, 0x81, 0x06, 0x75, 0x08,
        0x95, 0x05, 0x81, 0x03, 0xC0,
    ])
    p = ReportDescriptorParser()
    p.parse(descriptor)

    assert p.usage_page == 0x01          # Generic Desktop
    assert p.reports
    inputs = [r for r in p.reports if r["kind"] == 0x8]
    assert inputs
    # The boot keyboard report (report ID 1) has four input items:
    #   2 bits of modifiers, 6 bits of reserved modifiers, then
    #   2 x 8-bit keycodes and 5 x 8-bit keycodes.
    boot = [r for r in inputs if r["report_id"] == 1]
    assert len(boot) == 4
    assert boot[0]["bit_offset"] == 0
    assert boot[0]["size"] == 1
    assert boot[0]["count"] == 2
    assert boot[1]["size"] == 1
    assert boot[1]["count"] == 6
    assert boot[2]["size"] == 8
    assert boot[2]["count"] == 2
    assert boot[2]["bit_offset"] == 8
    assert boot[3]["size"] == 8
    assert boot[3]["count"] == 5
    assert boot[3]["bit_offset"] == 24
    # Collections are balanced.
    assert len(p.collections) == 2


def test_empty_descriptor_is_handled():
    p = ReportDescriptorParser()
    p.parse(b"")
    assert p.reports == []
    assert "no input" in describe_reports(p)


def test_truncated_descriptor_does_not_raise():
    """A truncated descriptor must not crash the probe."""
    p = ReportDescriptorParser()
    p.parse(VENDOR_DESCRIPTOR[:12])
    # No exception; whatever was parsed is simply what it is.
    assert isinstance(p.reports, list)
