"""Tests for the Linkam protocol / telemetry parsing.

The example strings are taken verbatim from Linksys32Help.chm (the
"ProgNewData Event" documentation) and from the string tables in
LinksysActXLib.ocx.

Run with:  python3 -m pytest tests/ -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from linkam.protocol import (
    FIELD_TAGS,
    LinkamProgrammer,
    Telemetry,
    parse_telemetry,
)


def test_documented_example():
    """The exact example from Linksys32Help.chm, TempControl ActiveX.htm."""
    text = "Temp 23.4 °C~Vac 0.1 mBar;Rate 10 °C/min~Limit 25.4 °C"
    tlm = parse_telemetry(text)
    assert tlm.values["temperature"] == pytest.approx(23.4)
    assert tlm.values["vacuum"] == pytest.approx(0.1)
    assert tlm.values["rate"] == pytest.approx(10.0)
    assert tlm.values["limit"] == pytest.approx(25.4)


def test_units_are_captured():
    text = "Temp 23.4 °C~Vac 0.1 mBar;Rate 10 °C/min~Limit 25.4 °C"
    tlm = parse_telemetry(text)
    assert tlm.units["temperature"] == "°C"
    assert tlm.units["vacuum"] == "mBar"
    assert tlm.units["rate"] == "°C"      # "°C/min" -> numeric unit is °C
    assert tlm.units["limit"] == "°C"


def test_optional_fields_are_parsed():
    """Optional tags start with '~' and only appear when fitted."""
    text = "Temp 23.4 °C~Force 12.5 N~Strain 0.003~RH 45.0 %"
    tlm = parse_telemetry(text)
    assert tlm.values["force"] == pytest.approx(12.5)
    assert tlm.values["strain"] == pytest.approx(0.003)
    assert tlm.values["relative_humidity"] == pytest.approx(45.0)


def test_second_stage_fields():
    text = "Temp 23.4 °C~Temp2 24.1 °C~Rate2 5.0 °C/min~Limit2 30.0 °C"
    tlm = parse_telemetry(text)
    assert tlm.values["temperature2"] == pytest.approx(24.1)
    assert tlm.values["rate2"] == pytest.approx(5.0)
    assert tlm.values["limit2"] == pytest.approx(30.0)


def test_ramp_row():
    text = "Temp 23.4 °C~Ramp Row 3~Rate 10 °C/min~Limit 25.4 °C"
    tlm = parse_telemetry(text)
    assert tlm.values["ramp_row"] == pytest.approx(3)


def test_negative_temperature():
    text = "Temp -15.25 °C~Rate 5.0 °C/min~Limit -60.0 °C"
    tlm = parse_telemetry(text)
    assert tlm.values["temperature"] == pytest.approx(-15.25)
    assert tlm.values["limit"] == pytest.approx(-60.0)


def test_malformed_input_does_not_raise():
    tlm = parse_telemetry("garbage nonsense")
    assert tlm.values == {}


def test_empty_string():
    tlm = parse_telemetry("")
    assert tlm.values == {}


def test_every_documented_field_tag_is_known():
    """Sanity check that FIELD_TAGS covers the recovered vocabulary."""
    for tag in ("Temp", "Temp2", "Vac", "Force", "Strain", "Rate", "Limit"):
        assert tag in FIELD_TAGS


def test_command_strings_match_recovered_verbs():
    """The command keywords must match LinksysActXLib.ocx exactly."""
    prog = LinkamProgrammer("/dev/null")
    sent = []

    class FakeSerial:
        def write(self, data):
            sent.append(data)

        def flush(self):
            pass

    prog._serial = FakeSerial()
    prog.send("Prog start")
    prog.set_rate(2.5)
    prog.set_limit(95.0)
    prog.set_hold_time(60)
    prog.heat()
    prog.cool()
    prog.hold()
    prog.stop()

    assert sent == [
        b"Prog start\r\n",
        b"prog rate 2.5\r\n",
        b"prog limit 95\r\n",
        b"prog time 60\r\n",
        b"Prog Heat\r\n",
        b"Prog Cool\r\n",
        b"Prog Hold\r\n",
        b"Prog stop\r\n",
    ]


def test_strict_framing_uses_cr_only():
    prog = LinkamProgrammer("/dev/null", strict_framing=True)
    sent = []

    class FakeSerial:
        def write(self, data):
            sent.append(data)

        def flush(self):
            pass

    prog._serial = FakeSerial()
    prog.start()
    assert sent == [b"Prog start\r"]


def test_compound_ramp_command_matches_ocx_format():
    """The OCX builds profile rows as one compound command.

    String table at 0x1e308 in LinksysActXLib.ocx:
        'prog ramp ' + row + ' rate ' + r + ' limit ' + lim + ' time ' + t
    """
    prog = LinkamProgrammer("/dev/null")
    sent = []

    class FakeSerial:
        def write(self, data):
            sent.append(data)

        def flush(self):
            pass

    prog._serial = FakeSerial()
    prog.programme_ramp(row=1, rate=2.5, limit=95.0, time_s=60.0)
    assert sent == [b"prog ramp 1 rate 2.5 limit 95 time 60\r\n"]


def test_picture_delay_command():
    prog = LinkamProgrammer("/dev/null")
    sent = []

    class FakeSerial:
        def write(self, data):
            sent.append(data)

        def flush(self):
            pass

    prog._serial = FakeSerial()
    prog.set_picture_delay(0.5)
    assert sent == [b"prog picdelay 0.5\r\n"]


class _FakeSerial:
    """A serial port that replies with a canned line."""

    def __init__(self, reply=b"Temp 23.4 \xc2\xb0C\r\n"):
        self.reply = reply
        self.written = []
        self.baudrate = None

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass

    def read(self, n=1):
        if not self.reply:
            return b""
        chunk, self.reply = self.reply[:n], self.reply[n:]
        return chunk

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


def test_probe_finds_baud_and_terminator(monkeypatch):
    """probe() must sweep baud x terminator and report what answered."""
    import sys

    class AnsweringSerial:
        """Answers with one telemetry line, but only at 19200 baud."""

        def __init__(self, port, baudrate, **kwargs):
            self.baudrate = baudrate
            self._buf = bytearray()

        def write(self, data):
            if self.baudrate == 19200:
                self._buf += b"Temp 23.4 \xc2\xb0C\r\n"

        def flush(self):
            pass

        def read(self, n=1):
            if not self._buf:
                return b""
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
            return chunk

        def reset_input_buffer(self):
            self._buf.clear()

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "serial",
                        type("S", (), {"Serial": AnsweringSerial}))

    p = LinkamProgrammer("/dev/null")
    result = p.probe(bauds=[9600, 19200], terminators=[("cr", b"\r")],
                     commands=["MA6"], timeout=0.2)
    assert result["winner"] is not None
    assert result["winner"]["baudrate"] == 19200
    assert result["winner"]["terminator"] == "cr"
    assert "Temp" in result["response"]


def test_probe_reports_failure_cleanly():
    p = LinkamProgrammer("/dev/null")
    result = p.probe(bauds=[9600], terminators=[("crlf", b"\r\n")],
                     commands=["MA6"], timeout=0.05)
    # no hardware present -> no winner, but no exception either
    assert result["winner"] is None
    assert "9600/crlf" in result["tried"]


def test_terminator_override_is_honoured():
    p = LinkamProgrammer("/dev/null")
    p._terminator_bytes = b"\r"
    assert p._terminator() == b"\r"
    p._terminator_bytes = None
    p.strict_framing = True
    assert p._terminator() == b"\r"
    p.strict_framing = False
    assert p._terminator() == b"\r\n"
