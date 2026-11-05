"""Tests for the Linkam T95's binary HID protocol.

The framing was recovered from Linkam's own SDK (``libLinkamSDK.so``),
so these tests encode the SDK's layout.  The status blocks used here
were captured from a real T95, so the parser is also validated against
what the instrument actually sends.
"""

from __future__ import annotations

import struct

import pytest

from linkam.t95 import (
    BLOCK_SIZE,
    MULTI_COMMAND_SIZE,
    REPLY_SIZE,
    T95_REPORT_SIZE,
    T95_STATUS_BLOCKS,
    Cmd,
    MultiCommand,
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


# --------------------------------------------------------------------------- #
# the status poll
# --------------------------------------------------------------------------- #


def test_poll_is_one_block():
    frames = build_status_poll()
    assert len(frames) == 1
    assert len(frames[0]) == BLOCK_SIZE


def test_poll_block_header():
    """byte 0 reserved, byte 1 block index, byte 2 block count."""
    frame = build_status_poll()[0]
    assert frame[0] == 0x00
    assert frame[1] == 1
    assert frame[2] == 1


def test_poll_carries_the_getlink_command():
    frame = build_status_poll()[0]
    command, length, param3, param4 = struct.unpack_from("<HHHH", frame, 3)
    assert command == Cmd.GETLINK == 0x0300
    assert length == 0
    assert param3 == 0
    assert param4 == 0


def test_poll_carries_no_instruction_text():
    """The poll must not be able to spell an ASCII command."""
    frame = build_status_poll()[0].lower()
    for keyword in (b"prog", b"start", b"ramp", b"heat", b"cool", b"hold"):
        assert keyword not in frame


# --------------------------------------------------------------------------- #
# command framing
# --------------------------------------------------------------------------- #


def test_command_header_layout():
    frames = build_command_frames(Cmd.SETMULT, b"payload", param3=7, param4=9)
    frame = frames[0]
    command, length, param3, param4 = struct.unpack_from("<HHHH", frame, 3)
    assert command == Cmd.SETMULT
    assert length == len(b"payload")
    assert param3 == 7
    assert param4 == 9
    assert frame[11:11 + len(b"payload")] == b"payload"


def test_a_short_command_fits_in_one_block():
    frames = build_command_frames(Cmd.GETLINK)
    assert len(frames) == 1
    assert frames[0][1] == 1
    assert frames[0][2] == 1


def test_a_long_command_is_split_into_blocks():
    """The SDK's rule: ceil((payload length + 8) / 60) blocks."""
    payload = b"\x00" * 96
    frames = build_command_frames(Cmd.SETMULT, payload)
    assert len(frames) == 2
    assert frames[0][1] == 1 and frames[0][2] == 2
    assert frames[1][1] == 2 and frames[1][2] == 2


def test_every_block_carries_60_bytes_of_the_command_stream():
    """The 8-byte header plus 52 payload bytes in block 1, 60 after."""
    payload = bytes(range(120))
    frames = build_command_frames(Cmd.SETMULT, payload)
    stream = struct.pack("<HHHH", Cmd.SETMULT, len(payload), 0, 0) + payload
    assert frames[0][3:63] == stream[0:60]
    assert frames[1][3:63] == stream[60:120]


def test_blocks_are_numbered_from_one():
    frames = build_command_frames(Cmd.SETMULT, b"\x00" * 200)
    assert [f[1] for f in frames] == [1, 2, 3, 4]
    assert all(f[2] == 4 for f in frames)


def test_a_command_id_must_fit_in_16_bits():
    with pytest.raises(ValueError):
        build_command_frames(0x10000)


# --------------------------------------------------------------------------- #
# the MultiCommand payload
# --------------------------------------------------------------------------- #


def test_multi_command_is_96_bytes():
    assert len(build_multi_command([(Cmd.MULTI_SETLIMIT, 30.0)])) == MULTI_COMMAND_SIZE


def test_multi_command_layout():
    """uint16 cmd[16] at offset 0, float value[16] at offset 32."""
    raw = build_multi_command([(Cmd.MULTI_SETRATE, 2.5)])
    assert struct.unpack_from("<H", raw, 0)[0] == Cmd.MULTI_SETRATE
    assert struct.unpack_from("<f", raw, 32)[0] == pytest.approx(2.5)


def test_multi_command_terminates_with_a_zero():
    raw = build_multi_command([(Cmd.MULTI_SETRATE, 2.5)])
    assert struct.unpack_from("<H", raw, 2)[0] == 0


def test_multi_command_round_trips():
    entries = [(Cmd.MULTI_SETRATE, 2.5), (Cmd.MULTI_SETLIMIT, 95.0)]
    parsed = MultiCommand.from_bytes(build_multi_command(entries))
    assert parsed.entries == entries


def test_multi_command_rejects_too_many_entries():
    with pytest.raises(ValueError):
        build_multi_command([(Cmd.MULTI_SETLIMIT, 1.0)] * 17)


# --------------------------------------------------------------------------- #
# setting the temperature
# --------------------------------------------------------------------------- #


def test_setpoint_command_sets_the_limit():
    frames = build_setpoint_command(95.0)
    assert len(frames) == 2, "a MultiCommand payload needs two blocks"
    command, length, _, _ = struct.unpack_from("<HHHH", frames[0], 3)
    assert command == Cmd.SETMULT
    assert length == MULTI_COMMAND_SIZE

    raw = frames[0][11:63] + frames[1][3:63]
    multi = MultiCommand.from_bytes(raw[:MULTI_COMMAND_SIZE])
    assert multi.entries == [(Cmd.MULTI_SETLIMIT, 95.0)]


def test_rate_command_sets_the_rate():
    frames = build_rate_command(2.0)
    raw = frames[0][11:63] + frames[1][3:63]
    multi = MultiCommand.from_bytes(raw[:MULTI_COMMAND_SIZE])
    assert multi.entries == [(Cmd.MULTI_SETRATE, 2.0)]


def test_start_command_programs_rate_limit_and_start_together():
    """One atomic command, so the instrument cannot act half-applied."""
    frames = build_start_heating_command(2.0, 95.0)
    raw = frames[0][11:63] + frames[1][3:63]
    multi = MultiCommand.from_bytes(raw[:MULTI_COMMAND_SIZE])
    assert multi.entries == [
        (Cmd.MULTI_SETRATE, 2.0),
        (Cmd.MULTI_SETLIMIT, 95.0),
        (Cmd.MULTI_START, 0.0),
    ]


def test_stop_command():
    frames = build_stop_heating_command()
    command, length, _, _ = struct.unpack_from("<HHHH", frames[0], 3)
    assert command == Cmd.SETSTOP == 0x0303
    assert length == 0


# --------------------------------------------------------------------------- #
# parsing the answer
# --------------------------------------------------------------------------- #


def make_block(
    index: int,
    *,
    count: int = 3,
    temperature: float | None = 23.6406,
    sensor2: float | None = None,
    rate: float | None = None,
    setpoint: float | None = None,
) -> bytes:
    """Build a reply block the way the instrument does."""
    block = bytearray(REPLY_SIZE)
    block[0] = index
    block[1] = count
    struct.pack_into("<HHHH", block, 2, Cmd.GETLINK, 136, 0, 0)
    if temperature is not None:
        struct.pack_into("<f", block, 10, temperature)
    if sensor2 is not None:
        struct.pack_into("<f", block, 14, sensor2)
    if rate is not None:
        struct.pack_into("<f", block, 18, rate)
    if setpoint is not None:
        struct.pack_into("<f", block, 22, setpoint)
    return bytes(block)


def test_parses_the_temperature():
    blocks = [make_block(1, temperature=23.6406), make_block(2), make_block(3)]
    status = parse_status(blocks)
    assert status.temperature_c == pytest.approx(23.6406, abs=1e-4)


def test_parses_the_second_sensor():
    blocks = [make_block(1, sensor2=-45.5), make_block(2), make_block(3)]
    status = parse_status(blocks)
    assert status.sensor2_c == pytest.approx(-45.5, abs=1e-4)


def test_parses_rate_and_setpoint():
    blocks = [make_block(1, rate=0.7, setpoint=0.35), make_block(2), make_block(3)]
    status = parse_status(blocks)
    assert status.rate == pytest.approx(0.7, abs=1e-6)
    assert status.setpoint_c == pytest.approx(0.35, abs=1e-6)


def test_blocks_are_identified_by_index_not_position():
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


def test_reply_header_is_decoded():
    blocks = [make_block(1), make_block(2), make_block(3)]
    reply = parse_reply(blocks)
    assert reply.command == Cmd.GETLINK
    assert reply.length == 136
    assert reply.param3 == 0
    assert reply.param4 == 0
    assert reply.is_status


# --------------------------------------------------------------------------- #
# the captured answer
# --------------------------------------------------------------------------- #


def test_parses_a_captured_answer():
    """A real capture from the instrument.

    Taken from a T95 sitting at room temperature, answering a status
    poll with three blocks.  The bytes decode exactly as the SDK says
    they should: command id 0x0300, payload length 136, then the
    temperature, the second thermocouple, the rate and the setpoint.
    """
    block1 = bytes.fromhex(
        "010300038800000000003c80bb41362e36c23333333f3333b33e"
        + "00" * 37
    )
    block2 = bytes.fromhex("020300" + "00" * 60)
    block3 = bytes.fromhex("030300" + "00" * 60)

    status = parse_status([block1, block2, block3])
    assert len(block1) == T95_REPORT_SIZE
    assert status.temperature_c == pytest.approx(23.4376, abs=1e-4)
    assert status.sensor2_c == pytest.approx(-45.5451, abs=1e-4)
    assert status.rate == pytest.approx(0.70, abs=1e-6)
    assert status.setpoint_c == pytest.approx(0.35, abs=1e-6)
    assert status.block_types == [1, 2, 3]


def test_the_captured_answer_carries_the_getlink_command():
    """Cross-check: the reply header the instrument really sends."""
    block1 = bytes.fromhex("010300038800000000003c80bb41362e36c2")
    reply = parse_reply([block1])
    assert reply.command == 0x0300
    assert reply.length == 136


def test_the_answer_has_three_blocks():
    assert T95_STATUS_BLOCKS == 3
