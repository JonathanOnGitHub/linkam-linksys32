"""Tests for the USB capture parser.

`tools/parse_capture.py` turns a USBPcap/tshark capture into the
host->device HID reports, which are the T95's commands -- the one part
of the protocol that could not be derived by reasoning and has to be
read off the wire.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import parse_capture  # noqa: E402


TSHARK_ROWS = [
    "01\t4d41360d0a000000",       # OUT: host -> device
    "81\t01030003880000000000e880bb41",  # IN: device -> host
    "01\t50616765200a",           # OUT
    "81\t01030003880000000000e880bb41",  # IN
]


def write(tmp_path, rows):
    path = tmp_path / "frames.txt"
    path.write_text("\n".join(rows) + "\n")
    return path


def test_splits_the_two_directions(tmp_path):
    path = write(tmp_path, TSHARK_ROWS)
    out_frames, in_frames = [], []
    for endpoint, payload in parse_capture.load(path):
        if endpoint == parse_capture.OUT_ENDPOINT:
            out_frames.append(payload)
        elif endpoint == parse_capture.IN_ENDPOINT:
            in_frames.append(payload)
    assert len(out_frames) == 2
    assert len(in_frames) == 2
    assert out_frames[0] == b"MA6\r\n\x00\x00\x00"


def test_accepts_a_plain_text_capture(tmp_path):
    """<endpoint> <payload> per line, space separated."""
    path = write(tmp_path, ["01 4d41360d0a", "81 01030003880000000000"])
    pairs = list(parse_capture.load(path))
    assert pairs[0] == (0x01, b"MA6\r\n")
    assert pairs[1][0] == 0x81


def test_ignores_unparseable_rows(tmp_path):
    path = write(tmp_path, ["not a row at all", "", "01 4d4136"])
    pairs = list(parse_capture.load(path))
    assert pairs == [(0x01, b"MA6")]


def test_hexdump_is_readable():
    out = parse_capture.hexdump(b"MA6\r\n")
    assert "4d 41 36 0d 0a" in out
    # CR and LF are not printable, so they show as dots.
    assert "|MA6..|" in out


def test_main_reports_when_there_are_no_commands(tmp_path, capsys):
    path = write(tmp_path, ["81 01030003880000000000"])
    rc = parse_capture.main([str(path)])
    assert rc == 1
    assert "No host->device reports found" in capsys.readouterr().out


def test_main_writes_the_command_frames(tmp_path):
    path = write(tmp_path, TSHARK_ROWS)
    out = tmp_path / "out.txt"
    assert parse_capture.main([str(path), "--out", str(out)]) == 0
    written = out.read_text().split()
    assert written == ["4d41360d0a000000", "50616765200a"]
