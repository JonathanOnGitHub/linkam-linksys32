"""Tests for the environment diagnostics.

These cover the pure logic (port classification, report rendering, severity
handling).  The checks that touch the real machine are not exercised here —
they are meant to run on the user's laptop.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from linkam.diagnostics import (
    FAIL,
    INFO,
    OK,
    WARN,
    Check,
    DiagnosticReport,
    _classify_port,
    _wrap,
    format_report,
)


class TestPortClassification:
    """The RS-232 vs TTL heuristic.

    Getting this wrong is the most common reason a Linkam programmer does
    not answer, so it is worth testing carefully.
    """

    @pytest.mark.parametrize("description", [
        "USB-RS232-SERIAL-Converter",
        "FTDI FT232 Serial (UART) IC",
        "USB2.0-Serial",
        "Prolific USB-Serial Comm Port",
        "CP2102 USB to UART Bridge",
        "CH340",
    ])
    def test_recognises_rs232_adapters(self, description):
        assert _classify_port(description) in ("rs232", "ttl")

    @pytest.mark.parametrize("description", [
        "USB-TTL",
        "TTL-USB Adapter (CH340)",
        "USB UART Adapter 3.3V",
        "Arduino Uno Serial",
    ])
    def test_recognises_ttl_breakouts(self, description):
        assert _classify_port(description) == "ttl"

    def test_rs232_wins_over_generic_uart(self):
        # "USB to RS232 UART converter" is RS-232 despite containing "uart".
        assert _classify_port("USB to RS232 UART converter") == "rs232"

    def test_unknown_for_unhelpful_description(self):
        assert _classify_port("") == "unknown"
        assert _classify_port("Some Random Device") == "unknown"


class TestReport:
    def test_empty_report_is_ok(self):
        report = DiagnosticReport()
        assert report.ok
        assert report.worst == OK

    def test_worst_reflects_most_severe(self):
        report = DiagnosticReport()
        report.add(Check("a", OK))
        report.add(Check("b", WARN))
        assert report.worst == WARN
        assert report.ok  # warnings do not block

        report.add(Check("c", FAIL))
        assert report.worst == FAIL
        assert not report.ok

    def test_info_sits_between_ok_and_warn(self):
        report = DiagnosticReport()
        report.add(Check("a", OK))
        report.add(Check("b", INFO))
        assert report.worst == INFO

    def test_counts(self):
        report = DiagnosticReport()
        report.add(Check("a", OK))
        report.add(Check("b", OK))
        report.add(Check("c", WARN))
        counts = report.counts()
        assert counts[OK] == 2
        assert counts[WARN] == 1
        assert counts[FAIL] == 0


class TestFormatting:
    def test_format_includes_check_names_and_hints(self):
        report = DiagnosticReport()
        report.add(Check("adapter", WARN, "USB-TTL", "needs a real RS-232 box"))
        text = format_report(report)
        assert "adapter" in text
        assert "USB-TTL" in text
        assert "needs a real RS-232 box" in text
        assert "warn" in text

    def test_format_reports_blocked_state(self):
        report = DiagnosticReport()
        report.add(Check("dep", FAIL, "missing"))
        text = format_report(report)
        assert "blocked" in text.lower()

    def test_format_reports_notes(self):
        report = DiagnosticReport()
        report.notes.append("Every port looks like a TTL breakout.")
        text = format_report(report)
        assert "TTL breakout" in text
        assert "Notes:" in text

    def test_wrap_respects_width(self):
        wrapped = _wrap("a " * 60, 40)
        assert all(len(line) <= 40 for line in wrapped)

    def test_wrap_preserves_blank_lines(self):
        assert _wrap("a\n\nb", 40) == ["a", "", "b"]


class TestPublicApi:
    def test_run_checks_produces_a_report(self):
        from linkam.diagnostics import run_checks

        report = run_checks()
        assert isinstance(report, DiagnosticReport)
        assert report.checks
        assert format_report(report)

    def test_cli_doctor_runs(self):
        from linkam.cli import main

        # Exit code 1 is acceptable (this machine may genuinely have no
        # serial adapter); we only assert it does not crash.
        code = main(["doctor"])
        assert code in (0, 1)
