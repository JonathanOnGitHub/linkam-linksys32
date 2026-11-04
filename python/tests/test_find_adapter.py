"""Tests for the adapter-finder heuristics.

The classification logic decides whether the USB adapter in front of
you can drive an RS-232 instrument.  Getting it wrong in the "rs232"
direction is expensive: you trust the tool, point the driver at a TTL
breakout, and then spend hours debugging a "framing bug" that is
really a level-mismatch problem.

The cases below use real product strings that these adapters ship with.
"""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "tools" / "find_adapter.py"
_spec = importlib.util.spec_from_file_location("find_adapter", _PATH)
find_adapter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(find_adapter)


classify = find_adapter.classify


@pytest.mark.parametrize(
    "text",
    [
        "USB-Serial-C Converter",
        "USB-Serial-C Converter (CH340)",
        "TTL-232R-5V",
        "CH340 USB-Serial",
        "Arduino Uno",
        "Raspberry Pi UART",
        "USB to TTL",
        "USB UART",
    ],
)
def test_ttl_breakouts_are_detected(text):
    """TTL breakouts must never be mistaken for RS-232 adapters."""
    assert classify(text) == "ttl"


@pytest.mark.parametrize(
    "text",
    [
        "USB2.0-Serial",
        "CP2102 USB to RS232",
        "CH341 USB to RS232",
        "PL2303 Serial Port",
        "Prolific USB-to-Serial Comm Port",
        "FTDI USB Serial Device",
        "USB RS232 Serial Adapter",
        "USB to RS232 converter",
    ],
)
def test_rs232_adapters_are_detected(text):
    assert classify(text) == "rs232"


def test_explicit_rs232_beats_ttl_marker():
    """An explicit RS-232 statement must win over a bare 'ttl' token.

    Real adapter labels sometimes contain both, e.g. "TTL USB-RS232".
    The transceiver is what matters.
    """
    assert classify("TTL USB-RS232 CH340") == "rs232"


@pytest.mark.parametrize("text", ["", "some vague blob", None])
def test_ambiguous_text_is_unknown(text):
    """Unrecognised strings must stay unknown, not guess either way."""
    assert classify(text) == "unknown"


def test_usb_serial_c_converter_is_not_treated_as_rs232():
    """Regression: 'USB-Serial-C Converter' contains 'serial'.

    These breakouts are TTL. Classifying them as rs232 would send the
    user confidently down the wrong path.
    """
    assert classify("USB-Serial-C Converter") == "ttl"
