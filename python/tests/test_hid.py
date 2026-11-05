"""Tests for the USB HID transport used by the Linkam T95.

These do not need the hardware: the HID framing logic is exercised
against a fake hidraw file descriptor, and the discovery helpers are
tested against synthetic sysfs trees.
"""

from __future__ import annotations

import io
import os
import struct
from unittest import mock

import pytest

from linkam import hid
from linkam.hid import (
    FRAMINGS,
    HidLink,
    HidTransport,
    find_link,
    read_report_descriptor,
)


# --------------------------------------------------------------------------- #
# ioctl number construction
# --------------------------------------------------------------------------- #


def test_ioc_matches_linux_layout():
    """The ioctl encoding must match <asm-generic/ioctl.h> exactly.

    Getting this wrong is what caused the earlier HIDIOCGRDESC failure.
    """
    # The layout is (dir << 30) | (size << 16) | (type << 8) | nr.
    assert hid._IOC_NRSHIFT == 0
    assert hid._IOC_TYPESHIFT == 8
    assert hid._IOC_SIZESHIFT == 16
    assert hid._IOC_DIRSHIFT == 30

    # HIDIOCGRDESCSIZE is _IOR('H', 0x01, int) -> size 4.
    assert hid._ioc(hid._IOC_READ, ord("H"), 0x01, 4) == 0x80044801


def test_report_descriptor_ioctl_size_is_correctly_encoded():
    """HIDIOCGRDESC must encode ``sizeof(struct hidraw_report_descriptor)``.

    The struct is ``__u32 size`` plus ``__u8 value[4096]``, so its size
    is 4100 bytes and that is what the ioctl size field must carry.

    Regression test: an earlier version shifted the size field by 14
    bits instead of 16, so the encoded size was 1 rather than 4100 and
    every call to HIDIOCGRDESC failed with EINVAL -- which looked
    exactly like "another driver owns the interface".  The T95's report
    descriptor was unobtainable until this was fixed.
    """
    assert hid.HID_MAX_DESCRIPTOR_SIZE == 4096
    expected_size = 4 + hid.HID_MAX_DESCRIPTOR_SIZE
    assert expected_size == 4100

    size_field = (hid._HIDIOCGRDESC >> hid._IOC_SIZESHIFT) & 0x3FFF
    assert size_field == expected_size, (
        f"HIDIOCGRDESC encodes size {size_field}, expected {expected_size}"
    )


def test_report_descriptor_constants_are_correct():
    assert hid._HIDIOCGRDESCSIZE == 0x80044801
    # 0x90044802 encodes a size of 4100.
    assert hid._HIDIOCGRDESC == 0x90044802
    assert hid._HIDIOCGRAWINFO == 0x80084803


# --------------------------------------------------------------------------- #
# a fake hidraw device
# --------------------------------------------------------------------------- #


class FakeHidraw:
    """Stand-in for /dev/hidrawN.

    Records everything written and serves a queue of canned input
    reports.  ``accepted_sizes`` lets a test model the real T95, which
    rejects 1-byte output reports with EINVAL.
    """

    def __init__(self, responses=(), accepted_sizes=(2, 4, 8, 16, 32, 64)):
        self.responses = list(responses)
        self.accepted_sizes = tuple(accepted_sizes)
        self.written = []
        self._out = io.BytesIO()

    # -- os-level API used by HidTransport -------------------------------- #
    def write(self, fd, data):
        if len(data) not in self.accepted_sizes:
            raise OSError(22, "Invalid argument")
        self.written.append(data)
        return len(data)

    def read(self, fd, size):
        if not self.responses:
            return b""
        return self.responses.pop(0)

    def close(self, fd):
        pass

    def open(self, path, flags):
        return 99

    def select_readable(self, fd):
        return bool(self.responses)


@pytest.fixture
def patched_hidraw(monkeypatch):
    """Install a FakeHidraw as the device behind ``/dev/hidraw1``."""
    fake = FakeHidraw()
    monkeypatch.setattr(os, "open", fake.open)
    monkeypatch.setattr(os, "write", fake.write)
    monkeypatch.setattr(os, "read", fake.read)
    monkeypatch.setattr(os, "close", fake.close)

    import select as select_mod

    real_select = select_mod.select

    def fake_select(rlist, wlist, xlist, timeout=None):
        if rlist and fake.select_readable(rlist[0]):
            return (list(rlist), [], [])
        # Nothing to read: sleep briefly so the test is quick but the
        # timeout logic still gets exercised.
        return real_select([], [], [], min(timeout or 0, 0.01))

    monkeypatch.setattr(select_mod, "select", fake_select)
    return fake


# --------------------------------------------------------------------------- #
# output framing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("framing", FRAMINGS)
def test_output_is_padded_to_report_size(patched_hidraw, framing):
    """Every framing must produce exactly ``report_size`` bytes."""
    patched_hidraw.accepted_sizes = (8,)
    t = HidTransport("/dev/hidraw1", framing=framing, report_size=8)
    t.open()
    t.write_line("MA6", b"\r\n")
    assert len(patched_hidraw.written[0]) == 8


def test_raw_framing_puts_text_at_offset_zero(patched_hidraw):
    patched_hidraw.accepted_sizes = (16,)
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=16)
    t.open()
    t.write_line("MA6", b"\r\n")
    assert patched_hidraw.written[0].startswith(b"MA6\r\n")
    assert patched_hidraw.written[0][5:] == b"\x00" * 11


def test_raw_framing_with_report_id(patched_hidraw):
    patched_hidraw.accepted_sizes = (16,)
    t = HidTransport(
        "/dev/hidraw1",
        framing="raw",
        report_size=16,
        output_report_id=0x02,
    )
    t.open()
    t.write_line("MA6", b"\r\n")
    assert patched_hidraw.written[0][0] == 0x02
    assert patched_hidraw.written[0][1:].startswith(b"MA6\r\n")


def test_id_text_framing_prefixes_report_id(patched_hidraw):
    patched_hidraw.accepted_sizes = (16,)
    t = HidTransport(
        "/dev/hidraw1", framing="id_text", report_size=16, output_report_id=0x03
    )
    t.open()
    t.write_line("MA6", b"\r\n")
    assert patched_hidraw.written[0][0] == 0x03
    assert patched_hidraw.written[0][1:].startswith(b"MA6\r\n")


def test_length_prefixed_framing(patched_hidraw):
    patched_hidraw.accepted_sizes = (16,)
    t = HidTransport("/dev/hidraw1", framing="length_prefixed", report_size=16)
    t.open()
    t.write_line("MA6", b"\r\n")
    data = patched_hidraw.written[0]
    assert data[0] == len(b"MA6\r\n")
    assert data[1:].startswith(b"MA6\r\n")


def test_long_command_is_refused_not_truncated(patched_hidraw):
    """A command that does not fit must raise, never be silently cut.

    Truncation is not a safe default: the cut command can still be a
    *valid* command with a different meaning.  ``limit 95`` cut to
    ``limit 9`` asks the instrument to hold at 9 degrees, and nothing
    downstream would notice.

    This is not hypothetical.  An earlier version of the transport
    discovered the output report size by probing, found that a 2-byte
    write was accepted, and then truncated every command to 2 bytes.
    The framing probe sent the two bytes "MA" to the instrument and
    reported "NO RESPONSE under any framing" -- a puzzle that had
    nothing to do with the framing.
    """
    from linkam.transport import CommandTooLong

    patched_hidraw.accepted_sizes = (8,)
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=8)
    t.open()
    with pytest.raises(CommandTooLong):
        t.write_line("prog ramp 1 rate 5 limit 95 time 60", b"\r\n")
    assert not patched_hidraw.written, "nothing should reach the device"


# --------------------------------------------------------------------------- #
# input framing
# --------------------------------------------------------------------------- #


def test_read_line_splits_on_nul(patched_hidraw):
    patched_hidraw.responses = [b"Temp 23.4 \xc2\xb0C\x00" + b"\x00" * 40]
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=64)
    t.open()
    assert t.read_line(1.0) == "Temp 23.4 °C"


def test_read_line_splits_on_newline(patched_hidraw):
    patched_hidraw.responses = [b"Temp 23.4 \xc2\xb0C\r\n"]
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=64)
    t.open()
    assert t.read_line(1.0) == "Temp 23.4 °C"


def test_read_line_splits_on_carriage_return(patched_hidraw):
    patched_hidraw.responses = [b"Temp 23.4 \xc2\xb0C\r"]
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=64)
    t.open()
    assert t.read_line(1.0) == "Temp 23.4 °C"


def test_read_line_raises_on_timeout(patched_hidraw):
    from linkam.transport import TransportTimeout

    patched_hidraw.responses = []
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=64)
    t.open()
    with pytest.raises(TransportTimeout):
        t.read_line(0.2)


def test_read_line_reassembles_split_reports(patched_hidraw):
    """A line split across two input reports must be reassembled."""
    patched_hidraw.responses = [b"Temp 23.4 ", b"\xc2\xb0C\x00"]
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=64)
    t.open()
    assert t.read_line(1.0) == "Temp 23.4 °C"


def test_telemetry_string_survives_the_round_trip(patched_hidraw):
    """The canonical telemetry string must survive the HID envelope."""
    text = "Temp 23.4 °C~Vac 0.1 mBar;Rate 10 °C/min~Limit 25.4 °C"
    raw = text.encode("utf-8") + b"\x00"
    patched_hidraw.responses = [raw + bytes(64 - len(raw))]
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=64)
    t.open()
    line = t.read_line(1.0)

    from linkam.protocol import parse_telemetry

    tlm = parse_telemetry(line)
    assert tlm.temperature == 23.4
    assert tlm.rate == 10.0
    assert tlm.limit == 25.4
    assert tlm.vacuum == 0.1


# --------------------------------------------------------------------------- #
# output size discovery
# --------------------------------------------------------------------------- #


def test_t95_report_size_comes_from_the_descriptor_not_probing(patched_hidraw):
    """The T95's payload is 63 bytes, as its report descriptor says.

    No probing is done, because probing means writing to the device.
    """
    patched_hidraw.accepted_sizes = (2, 4, 8, 16, 32, 64)
    t = HidTransport("/dev/hidraw1", framing="text_padded")
    t.open()
    assert t._out_size == 63
    assert not patched_hidraw.written, "opening the device should not write to it"


def test_discover_output_size_picks_smallest_accepted(patched_hidraw):
    """For an unknown device, the size is discovered by probing."""
    patched_hidraw.accepted_sizes = (2, 4, 8, 16, 32, 64)
    t = HidTransport("/dev/hidraw1", framing="raw", vendor=0x1234, product=0x5678)
    t.open()
    assert t._out_size == 2


def test_discover_output_size_handles_only_large_reports(patched_hidraw):
    patched_hidraw.accepted_sizes = (32, 64)
    t = HidTransport("/dev/hidraw1", framing="raw", vendor=0x1234, product=0x5678)
    t.open()
    assert t._out_size == 32


def test_discovery_probe_payload_is_inert(patched_hidraw):
    """The size-discovery probe must not be able to spell a command.

    A ramp must never start as a side effect of opening the device.
    """
    patched_hidraw.accepted_sizes = (2, 4, 8, 16, 32, 64)
    t = HidTransport("/dev/hidraw1", framing="raw", vendor=0x1234, product=0x5678)
    t.open()
    for payload in patched_hidraw.written:
        lowered = payload.lower()
        assert b"prog" not in lowered
        assert b"start" not in lowered
        assert b"ramp" not in lowered
        assert b"heat" not in lowered
        assert b"cool" not in lowered
        assert b"hold" not in lowered


def test_unknown_framing_is_rejected():
    with pytest.raises(ValueError):
        HidTransport("/dev/hidraw1", framing="nonsense")


# --------------------------------------------------------------------------- #
# the safe output-size probe
# --------------------------------------------------------------------------- #


class RejectingHidraw(FakeHidraw):
    """A hidraw that NAKs output reports longer than `max_size`.

    This models the real T95: it accepts short output reports but
    times out on a 63-byte one, and the timeout is *sticky* -- once a
    write has been rejected, even short writes fail.
    """

    def __init__(self, max_size=32, sticky=True, responses=()):
        super().__init__(responses=responses)
        self.max_size = max_size
        self.sticky = sticky
        self.wedged = False

    def write(self, fd, data):
        if self.wedged:
            raise OSError(110, "Connection timed out")
        if len(data) > self.max_size:
            if self.sticky:
                self.wedged = True
            raise OSError(110, "Connection timed out")
        self.written.append(data)
        return len(data)


@pytest.fixture
def rejecting_hidraw(monkeypatch):
    fake = RejectingHidraw()
    monkeypatch.setattr(os, "open", fake.open)
    monkeypatch.setattr(os, "write", fake.write)
    monkeypatch.setattr(os, "read", fake.read)
    monkeypatch.setattr(os, "close", fake.close)
    return fake


def test_probe_finds_the_largest_accepted_size(rejecting_hidraw):
    """The probe must report the biggest size the device will take."""
    rejecting_hidraw.max_size = 32
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=2)
    t.open()
    best, results = t.probe_output_size()
    assert best == 32
    # 2..32 were accepted; 63 was rejected and the sweep stopped there.
    assert [r["size"] for r in results] == [2, 4, 8, 16, 32, 63]
    assert [r["ok"] for r in results] == [True] * 5 + [False]


def test_probe_stops_at_the_first_rejection(rejecting_hidraw):
    """It must not keep pushing after a size has been rejected."""
    rejecting_hidraw.max_size = 8
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=2)
    t.open()
    best, results = t.probe_output_size()
    assert best == 8
    # 16 was rejected, and the sweep stopped there.
    assert [r["size"] for r in results] == [2, 4, 8, 16]
    assert results[-1]["ok"] is False
    assert results[-1]["error"] == "ETIMEDOUT"


def test_probe_reports_whether_the_device_survived(rejecting_hidraw):
    """A rejection may wedge the device; the probe must say so."""
    rejecting_hidraw.max_size = 8
    rejecting_hidraw.sticky = True
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=2)
    t.open()
    _, results = t.probe_output_size()
    last = results[-1]
    assert last["ok"] is False
    # The baseline write after the rejection also failed: the device is wedged.
    assert last["alive_after"] is False
    assert last["alive_error"] == "ETIMEDOUT"


def test_probe_payload_cannot_spell_a_command(rejecting_hidraw):
    """Probing must never be able to start a ramp."""
    rejecting_hidraw.max_size = 64
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=2)
    t.open()
    t.probe_output_size()
    for payload in rejecting_hidraw.written:
        lowered = payload.lower()
        for keyword in (b"prog", b"start", b"ramp", b"heat", b"cool", b"hold"):
            assert keyword not in lowered


# --------------------------------------------------------------------------- #
# raw binary reports (the T95's real protocol)
# --------------------------------------------------------------------------- #


def test_write_report_sends_raw_bytes(patched_hidraw):
    """The T95's protocol is binary, so raw reports must bypass the
    ASCII framing entirely."""
    patched_hidraw.accepted_sizes = (16,)
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=16)
    t.open()
    t.write_report(b"\x00\x00\x41\x42")
    assert patched_hidraw.written[0] == b"\x00\x00\x41\x42" + b"\x00" * 12


def test_write_report_pads_to_the_report_size(patched_hidraw):
    patched_hidraw.accepted_sizes = (16,)
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=16)
    t.open()
    t.write_report(b"\x01")
    assert len(patched_hidraw.written[0]) == 16


def test_write_report_truncates_an_oversized_payload(patched_hidraw):
    """A raw write is not a command, so truncating cannot misread it."""
    patched_hidraw.accepted_sizes = (4,)
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=4)
    t.open()
    t.write_report(b"0123456789")
    assert patched_hidraw.written[0] == b"0123"


def test_write_report_raises_when_the_device_is_not_open(patched_hidraw):
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=16)
    with pytest.raises(ConnectionError):
        t.write_report(b"\x00\x00")


def test_read_reports_returns_up_to_count(patched_hidraw):
    patched_hidraw.responses = [b"a" * 63, b"b" * 63, b"c" * 63]
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=63)
    t.open()
    reports = t.read_reports(2, timeout=1.0)
    assert len(reports) == 2
    assert reports[0][:1] == b"a"


def test_read_reports_stops_at_the_timeout(patched_hidraw):
    patched_hidraw.responses = []
    t = HidTransport("/dev/hidraw1", framing="raw", report_size=63)
    t.open()
    assert t.read_reports(3, timeout=0.3) == []


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_find_link_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(hid, "_hidraw_nodes", lambda: [])
    assert find_link() is None


def test_find_link_matches_by_vendor_product(monkeypatch):
    monkeypatch.setattr(hid, "_hidraw_nodes", lambda: ["/dev/hidraw1"])
    monkeypatch.setattr(
        hid,
        "_read_uevent_strings",
        lambda node: {
            "HID_ID": "0003:000016DA:00000002",
            "HID_NAME": "Linkam T95",
            "HID_UNIQ": "1234",
        },
    )
    monkeypatch.setattr(hid, "array_u32_u16_u16", lambda fd: (3, 0x16DA, 0x0002))
    monkeypatch.setattr(hid, "_read_string", lambda fd, nr, size=256: "Linkam T95")
    link = find_link()
    assert link is not None
    assert link.node == "/dev/hidraw1"
    assert link.is_t95
    assert link.vendor == 0x16DA
    assert link.product == 0x0002


def test_find_link_ignores_other_devices(monkeypatch):
    monkeypatch.setattr(hid, "_hidraw_nodes", lambda: ["/dev/hidraw0"])
    monkeypatch.setattr(hid, "array_u32_u16_u16", lambda fd: (3, 0x046D, 0xC52B))
    monkeypatch.setattr(hid, "_read_string", lambda fd, nr, size=256: "Logitech")
    monkeypatch.setattr(hid, "_read_uevent_strings", lambda node: {})
    assert find_link() is None


def test_find_link_falls_back_to_hid_id_uevent(monkeypatch):
    """Some devices refuse GRAWINFO; the uevent HID_ID still identifies them."""
    monkeypatch.setattr(hid, "_hidraw_nodes", lambda: ["/dev/hidraw7"])
    monkeypatch.setattr(hid, "array_u32_u16_u16", lambda fd: None)
    monkeypatch.setattr(hid, "_read_string", lambda fd, nr, size=256: "")
    monkeypatch.setattr(
        hid,
        "_read_uevent_strings",
        lambda node: {
            "HID_ID": "0003:000016DA:00000002",
            "HID_NAME": "Linkam T95",
            "HID_UNIQ": "1234",
        },
    )
    link = find_link()
    assert link is not None
    assert link.vendor == 0x16DA
    assert link.product == 0x0002
    assert link.uniq == "1234"


# --------------------------------------------------------------------------- #
# report descriptor
# --------------------------------------------------------------------------- #


def test_read_report_descriptor_handles_refusal(monkeypatch):
    """A device that refuses HIDIOCGRDESC must not raise."""
    monkeypatch.setattr(os, "open", lambda path, flags: 99)
    monkeypatch.setattr(os, "close", lambda fd: None)

    def boom(fd, request, buf, mutate):
        raise OSError(22, "Invalid argument")

    import fcntl

    monkeypatch.setattr(fcntl, "ioctl", boom)
    assert read_report_descriptor("/dev/hidraw1") == b""


def test_output_size_is_derived_from_the_report_descriptor(monkeypatch):
    """The transport should read the descriptor rather than probe.

    Probing means writing to the device; reading the descriptor does
    not.  The T95's descriptor says the Output report is 63 bytes.
    """
    from linkam.hid_descriptor import ReportDescriptorParser

    descriptor = bytes([
        0x06, 0x00, 0xFF,   # Usage Page (0xFF00)
        0x09, 0x01,         # Usage (0x01)
        0xA1, 0x01,         # Collection (Application)
        0x75, 0x08,         # Report Size (8)
        0x95, 0x3F,         # Report Count (63)
        0x09, 0x02,         # Usage (0x02)
        0x91, 0x02,         # Output
        0xC0,               # End Collection
    ])

    monkeypatch.setattr(hid, "read_report_descriptor", lambda node: descriptor)
    monkeypatch.setattr(hid, "_hidraw_nodes", lambda: ["/dev/hidraw1"])
    monkeypatch.setattr(
        hid,
        "_read_uevent_strings",
        lambda node: {"HID_ID": "0003:000016DA:00000002", "HID_NAME": "Linkam T95"},
    )

    fake = FakeHidraw()
    monkeypatch.setattr(os, "open", fake.open)
    monkeypatch.setattr(os, "close", fake.close)

    t = hid.HidTransport()
    t.open()
    try:
        assert t._out_size == 63
        assert not fake.written, "deriving the size must not write to the device"
    finally:
        t.close()


def test_report_sizes_helper_reports_output_size():
    from linkam.hid_descriptor import (
        ReportDescriptorParser,
        report_sizes,
        uses_report_ids,
    )

    descriptor = bytes([
        0x06, 0x00, 0xFF,
        0x09, 0x01,
        0xA1, 0x01,
        0x75, 0x08,
        0x95, 0x3F,
        0x09, 0x02,
        0x91, 0x02,
        0xC0,
    ])
    p = ReportDescriptorParser()
    p.parse(descriptor)
    assert report_sizes(p) == {"OUTPUT": 63}
    assert uses_report_ids(p) is False
