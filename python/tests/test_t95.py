"""Tests for the Linkam T95's binary HID protocol.

The blocks used here were captured from a real T95 (a status poll and
its three-block answer), so the parser is validated against what the
instrument actually sends rather than against a guess.
"""

from __future__ import annotations

import struct

import pytest

from linkam.t95 import (
    T95_REPORT_SIZE,
    T95_STATUS_BLOCKS,
    T95Status,
    build_status_poll,
    parse_status,
)


def make_block(
    kind: int,
    *,
    temperature: float | None = 23.6406,
    sensor2: float | None = None,
    rate: float | None = None,
    limit: float | None = None,
    flags: int | None = None,
    marker: int | None = None,
) -> bytes:
    """Build a block the way the instrument does."""
    block = bytearray(T95_REPORT_SIZE)
    block[0] = kind
    block[1] = 0x03
    if temperature is not None:
        struct.pack_into("<f", block, 10, temperature)
    if sensor2 is not None:
        struct.pack_into("<f", block, 14, sensor2)
    if rate is not None:
        struct.pack_into("<f", block, 18, rate)
    if limit is not None:
        struct.pack_into("<f", block, 22, limit)
    if flags is not None:
        block[34] = flags
    if marker is not None:
        block[51] = marker
    return bytes(block)


# --------------------------------------------------------------------------- #
# the poll frame
# --------------------------------------------------------------------------- #


def test_poll_frame_is_the_right_length():
    assert len(build_status_poll()) == T95_REPORT_SIZE


def test_poll_frame_has_its_first_two_bytes_equal():
    """The device accepts a frame if and only if bytes 0 and 1 match."""
    for marker in (0x00, 0x01, 0x41, 0x52, 0x7F, 0xFF):
        frame = build_status_poll(marker)
        assert frame[0] == frame[1] == marker


def test_poll_frame_is_zero_elsewhere():
    frame = build_status_poll(0x41)
    assert frame[2:] == bytes(T95_REPORT_SIZE - 2)


def test_poll_frame_carries_no_instruction():
    """The poll must not be able to spell a command."""
    frame = build_status_poll()
    lowered = frame.lower()
    for keyword in (b"prog", b"start", b"ramp", b"heat", b"cool", b"hold"):
        assert keyword not in lowered


# --------------------------------------------------------------------------- #
# parsing the answer
# --------------------------------------------------------------------------- #


def test_parses_the_temperature():
    blocks = [make_block(1, temperature=23.6406), make_block(2), make_block(3)]
    status = parse_status(blocks)
    assert status.temperature_c == pytest.approx(23.6406, abs=1e-4)


def test_parses_the_second_sensor():
    blocks = [make_block(1, sensor2=-45.5), make_block(2), make_block(3)]
    status = parse_status(blocks)
    assert status.sensor2_c == pytest.approx(-45.5, abs=1e-4)


def test_parses_rate_and_limit():
    blocks = [make_block(1, rate=0.7, limit=0.35), make_block(2), make_block(3)]
    status = parse_status(blocks)
    assert status.rate == pytest.approx(0.7, abs=1e-6)
    assert status.limit == pytest.approx(0.35, abs=1e-6)


def test_parses_block2_fields():
    blocks = [make_block(1), make_block(2, flags=1, marker=0x48), make_block(3)]
    status = parse_status(blocks)
    assert status.block2_flags == 1
    assert status.block2_marker == 0x48


def test_block_types_are_reported():
    blocks = [make_block(1), make_block(2), make_block(3)]
    assert parse_status(blocks).block_types == [1, 2, 3]


def test_blocks_are_identified_by_type_not_position():
    """A device that reordered the blocks would still parse."""
    blocks = [make_block(3), make_block(2), make_block(1, temperature=19.5)]
    status = parse_status(blocks)
    assert status.temperature_c == pytest.approx(19.5, abs=1e-4)


def test_missing_fields_are_none_not_zero():
    """An unpopulated field must not be reported as a measurement."""
    blocks = [make_block(1, temperature=None), make_block(2), make_block(3)]
    status = parse_status(blocks)
    assert status.temperature_c is None
    assert status.rate is None


def test_short_blocks_are_tolerated():
    status = parse_status([b"\x01"])
    assert status.temperature_c is None
    assert status.blocks == [b"\x01"]


def test_empty_answer_is_tolerated():
    status = parse_status([])
    assert isinstance(status, T95Status)
    assert status.temperature_c is None
    assert status.block_types == []


def test_raw_blocks_are_kept():
    blocks = [make_block(1), make_block(2), make_block(3)]
    assert parse_status(blocks).blocks == blocks


# --------------------------------------------------------------------------- #
# the captured answer
# --------------------------------------------------------------------------- #


def test_parses_a_captured_answer():
    """A real capture from the instrument.

    Taken from a T95 sitting at room temperature, answering a status
    poll with three blocks.
    """
    block1 = bytes.fromhex(
        "010300038800000000003c80bb41362e36c23333333f3333b33e"
        + "00" * 37
    )
    block2 = bytes.fromhex("020300" + "00" * 31 + "01" + "00" * 16 + "48" + "00" * 11)
    block3 = bytes.fromhex("030300" + "00" * 60)

    status = parse_status([block1, block2, block3])
    assert len(block1) == T95_REPORT_SIZE
    assert status.temperature_c == pytest.approx(23.4376, abs=1e-4)
    assert status.sensor2_c == pytest.approx(-45.5451, abs=1e-4)
    assert status.rate == pytest.approx(0.70, abs=1e-6)
    assert status.limit == pytest.approx(0.35, abs=1e-6)
    assert status.block2_flags == 1
    assert status.block2_marker == 0x48
    assert status.block_types == [1, 2, 3]


def test_the_answer_has_three_blocks():
    assert T95_STATUS_BLOCKS == 3
