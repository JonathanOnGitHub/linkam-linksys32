"""Tests for the T95's binary link at the programmer level.

These exercise the plumbing between the HID transport and the T95's
binary protocol: that a status poll goes out as a raw report, that the
answer comes back as three blocks, and that the temperature is parsed
out of them.

No hardware is needed: a fake transport stands in for the hidraw node.
"""

from __future__ import annotations

import struct

import pytest

from linkam.protocol import HidProgrammer, ProtocolError
from linkam.t95 import T95_REPORT_SIZE


def make_block_for_test(
    kind: int,
    *,
    temperature=None,
    sensor2=None,
    rate=None,
    limit=None,
    flags=None,
    marker=None,
) -> bytes:
    """Build a status block the way the instrument does."""
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


class FakeBinaryTransport:
    """A transport that speaks the T95's binary protocol.

    Records the reports written and serves a canned answer.
    """

    def __init__(self, blocks=None):
        self.written = []
        self.blocks = list(blocks or [])
        self.description = "fake hid"

    def open(self):
        pass

    def close(self):
        pass

    def write_report(self, payload):
        self.written.append(payload)

    def read_reports(self, count, timeout=1.0):
        out = self.blocks[:count]
        self.blocks = self.blocks[count:]
        return out


def make_programmer(blocks):
    prog = HidProgrammer("/dev/hidraw1")
    prog._transport = FakeBinaryTransport(blocks)
    return prog


# --------------------------------------------------------------------------- #
# the poll
# --------------------------------------------------------------------------- #


def test_poll_status_writes_a_status_poll():
    prog = make_programmer([make_block_for_test(1), make_block_for_test(2),
                            make_block_for_test(3)])
    prog.poll_status()
    assert len(prog._transport.written) == 1
    frame = prog._transport.written[0]
    assert len(frame) == T95_REPORT_SIZE
    assert frame[0] == frame[1], "the device only accepts equal first bytes"


def test_poll_status_parses_the_answer():
    prog = make_programmer([
        make_block_for_test(1, temperature=23.6406),
        make_block_for_test(2, flags=1, marker=0x48),
        make_block_for_test(3),
    ])
    status = prog.poll_status()
    assert status.temperature_c == pytest.approx(23.6406, abs=1e-4)
    assert status.block_types == [1, 2, 3]


def test_read_temperature_returns_the_value():
    prog = make_programmer([
        make_block_for_test(1, temperature=31.25),
        make_block_for_test(2),
        make_block_for_test(3),
    ])
    assert prog.read_temperature() == pytest.approx(31.25, abs=1e-4)


def test_read_temperature_raises_when_the_instrument_is_silent():
    prog = make_programmer([])
    with pytest.raises(ProtocolError):
        prog.read_temperature()


def test_read_temperature_raises_when_no_temperature_is_reported():
    prog = make_programmer([
        make_block_for_test(1, temperature=None),
        make_block_for_test(2),
        make_block_for_test(3),
    ])
    with pytest.raises(ProtocolError):
        prog.read_temperature()


def test_read_status_retries_while_the_sensor_is_still_cold():
    """After a power cycle the first poll can come back with no sample.

    A zero is not a temperature, so the read must be repeated rather
    than reporting "no temperature".
    """
    prog = make_programmer([
        make_block_for_test(1, temperature=None),
        make_block_for_test(2),
        make_block_for_test(3),
        make_block_for_test(1, temperature=23.5),
        make_block_for_test(2),
        make_block_for_test(3),
    ])
    status = prog.read_status()
    assert status.temperature_c == pytest.approx(23.5, abs=1e-4)
    assert len(prog._transport.written) == 2, "it should have polled twice"


def test_read_status_gives_up_after_the_retry_limit():
    prog = make_programmer([
        make_block_for_test(1, temperature=None),
        make_block_for_test(2),
        make_block_for_test(3),
    ])
    with pytest.raises(ProtocolError):
        prog.read_status(retries=2)
    assert len(prog._transport.written) == 2


# --------------------------------------------------------------------------- #
# the telemetry loop
# --------------------------------------------------------------------------- #


def test_telemetry_callbacks_receive_the_temperature():
    """The inherited on_telemetry API works on the binary link."""
    prog = make_programmer([
        make_block_for_test(1, temperature=21.5),
        make_block_for_test(2),
        make_block_for_test(3),
    ])
    seen = []
    prog.on_telemetry(seen.append)
    prog._read_loop_once()
    assert seen, "the callback should have been invoked"
    assert seen[0].temperature == pytest.approx(21.5, abs=1e-4)
    assert seen[0].units["temperature"] == "°C"


def test_state_is_updated_from_the_status():
    prog = make_programmer([
        make_block_for_test(1, temperature=21.5, rate=0.7, limit=0.35),
        make_block_for_test(2),
        make_block_for_test(3),
    ])
    prog._read_loop_once()
    assert prog._state.temperature == pytest.approx(21.5, abs=1e-4)


def test_a_floated_temperature_round_trips():
    """The float32 encoding must not lose meaningful precision."""
    prog = make_programmer([
        make_block_for_test(1, temperature=-19.125),
        make_block_for_test(2),
        make_block_for_test(3),
    ])
    assert prog.read_temperature() == pytest.approx(-19.125, abs=1e-3)
