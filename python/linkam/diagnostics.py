"""Environment diagnostics for a real Linkam instrument.

This module answers one question: *what will fail when I plug the
instrument in?*  It is deliberately read-only — nothing here opens the
programmer port, sends a command, or touches the instrument.  The only
thing it does to the serial port is list it, which does not disturb a
running session.

The checks are ordered so that the most likely blocker comes first:

1. Python version and installed dependencies.
2. Serial ports, with an assessment of whether a candidate looks like a
   genuine RS-232 adapter (not a TTL/USB-TTL breakout).
3. Video backend availability, with a platform-specific verdict.
4. A summary of the protocol questions that can only be settled by
   talking to the hardware.

Run it via ``python -m linkam.cli doctor``.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "Check",
    "DiagnosticReport",
    "run_checks",
    "format_report",
    "RS232_HINTS",
    "TTL_HINTS",
]


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #

#: Severity levels, in increasing order of "this will bite you".
OK = "ok"
WARN = "warn"
FAIL = "fail"
INFO = "info"

_SEVERITY_ORDER = {OK: 0, INFO: 1, WARN: 2, FAIL: 3}

_SYMBOL = {OK: "[ ok ]", INFO: "[info]", WARN: "[warn]", FAIL: "[FAIL]"}


@dataclass
class Check:
    """One diagnostic result."""

    name: str
    status: str
    detail: str = ""
    hint: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    @property
    def warned(self) -> bool:
        return self.status == WARN


@dataclass
class DiagnosticReport:
    """A full set of checks plus any extra advice."""

    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    @property
    def worst(self) -> str:
        if not self.checks:
            return OK
        return max((c.status for c in self.checks), key=lambda s: _SEVERITY_ORDER[s])

    @property
    def ok(self) -> bool:
        """True when nothing failed (warnings are tolerated)."""
        return not any(c.failed for c in self.checks)

    def counts(self) -> dict[str, int]:
        out = {OK: 0, INFO: 0, WARN: 0, FAIL: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out


# --------------------------------------------------------------------------- #
# Heuristics for identifying the adapter
# --------------------------------------------------------------------------- #

#: Substrings that suggest the port really is an RS-232 (or RS-485) link.
RS232_HINTS = (
    "rs232",
    "rs-232",
    "rs485",
    "rs-485",
    "serial",
    "uart",
    "ch340",
    "ch341",
    "cp210",
    "ftdi",
    "pl2303",
    "prolific",
    "usb-serial",
)

#: Substrings that suggest the port is a **TTL** UART rather than RS-232.
#: A TTL breakout has no line-level transceiver, so its TX/RX levels are
#: 0-3.3 V / 5 V logic levels, which will not drive an RS-232 instrument.
TTL_HINTS = (
    "ttl",
    "uart",
    "3v3",
    "3.3v",
    "raspberry",
    "arduino",
    "esp32",
    "esp8266",
    "usb uart",
    "ch340",
    "ch341",
)


def _classify_port(description: str) -> str:
    """Return ``'rs232'``, ``'ttl'`` or ``'unknown'`` for a port description."""
    text = (description or "").lower()
    if not text:
        return "unknown"
    # TTL markers win: a CH340 sold as "USB-TTL" is TTL, whereas a CH340
    # sold as "USB-RS232" is a genuine transceiver board.
    ttl = any(h in text for h in TTL_HINTS)
    rs = any(h in text for h in RS232_HINTS)
    if ttl and not any(
        h in text for h in ("rs232", "rs-232", "rs485", "rs-485")
    ):
        return "ttl"
    if rs:
        return "rs232"
    return "unknown"


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #

def _check_python(report: DiagnosticReport) -> None:
    if sys.version_info >= (3, 8):
        report.add(Check(
            "Python version",
            OK,
            f"{platform.python_version()} at {sys.executable}",
        ))
    else:
        report.add(Check(
            "Python version",
            FAIL,
            f"{platform.python_version()} is too old; 3.8+ is required",
            "The package uses PEP 604 unions and builtin generics. "
            "Try: python3.8 -m pip install -r requirements.txt",
        ))

    for module, why, extra in (
        ("serial", "serial port access (required for the temperature link)", ""),
        ("numpy", "frame buffers", ""),
        ("cv2", "video capture", "opencv-python"),
    ):
        try:
            mod = __import__(module)
        except ImportError:
            status = FAIL if module == "serial" else WARN
            hint = extra or f"pip install {module}"
            if module == "cv2" and sys.platform == "darwin":
                hint = ("opencv-python is excluded on macOS by "
                        "requirements.txt; install it manually with "
                        "`pip install opencv-python` if you need video "
                        "(the temperature link does not need it)")
            report.add(Check(
                f"dependency: {module}",
                status,
                "not importable",
                f"{why}. {hint}",
            ))
        else:
            ver = getattr(mod, "__version__", "unknown")
            report.add(Check(
                f"dependency: {module}",
                OK,
                f"version {ver}",
            ))


def _check_serial(report: DiagnosticReport) -> None:
    try:
        from serial.tools import list_ports
    except ImportError:
        report.add(Check(
            "serial ports",
            FAIL,
            "pyserial is not installed",
            "pip install pyserial",
        ))
        return

    ports = list(list_ports.comports())
    if not ports:
        report.add(Check(
            "serial ports",
            WARN,
            "no serial ports found",
            "Plug in the USB-to-RS232 adapter. If you are on Linux and the "
            "adapter is not listed, check `lsusb` and that the device has a "
            "cdc_acm / ftdi_sio / ch341 driver bound.",
        ))
        return

    report.add(Check(
        "serial ports",
        OK,
        f"{len(ports)} port(s) found",
    ))

    ttl_candidates = []
    for p in ports:
        kind = _classify_port(p.description or "")
        extra = " ".join(x for x in (p.manufacturer, p.product) if x)
        desc = p.description or ""
        # Avoid "ttyS0 — ttyS0" when the description just repeats the device.
        if desc and desc.lower() != p.device.lower():
            label = f"{p.device} — {desc}"
        else:
            label = p.device
        if extra:
            label += f" [{extra}]"

        if kind == "rs232":
            report.add(Check(
                f"adapter: {p.device}",
                OK,
                label,
            ))
        elif kind == "ttl":
            ttl_candidates.append(p.device)
            report.add(Check(
                f"adapter: {p.device}",
                WARN,
                label,
                "This looks like a USB-to-TTL breakout, not a USB-to-RS232 "
                "adapter. The programmer speaks RS-232 (+/-5 V logic levels) "
                "and a TTL port will NOT drive it. You need an adapter with "
                "a genuine RS-232 transceiver on the DB9.",
            ))
        else:
            report.add(Check(
                f"adapter: {p.device}",
                INFO,
                label,
                "Could not tell whether this is RS-232 or TTL. If the "
                "instrument never responds, check that the adapter has an "
                "RS-232 transceiver and the right DB9 gender/crossover.",
            ))

    if ttl_candidates and len(ttl_candidates) == len(ports):
        report.notes.append(
            "Every detected serial port looks like a USB-to-TTL breakout. "
            "That is the single most likely reason a Linkam programmer will "
            "not answer. Buy/use a USB-to-RS232 adapter instead."
        )


def _check_video(report: DiagnosticReport) -> None:
    try:
        import cv2  # noqa: F401
        have_cv2 = True
    except ImportError:
        have_cv2 = False

    if sys.platform == "win32":
        if have_cv2:
            report.add(Check(
                "video backend",
                OK,
                "DirectShow (cv2.CAP_DSHOW) available on Windows",
                "The stock Conexant/BT848 driver registers under the "
                "friendly name \"Conexant's BtPCI Capture\"; cv2 will find it.",
            ))
        else:
            report.add(Check(
                "video backend",
                WARN,
                "opencv-python not installed",
                "pip install opencv-python",
            ))
        return

    if sys.platform == "darwin":
        report.add(Check(
            "video backend",
            WARN,
            "macOS has no DirectShow and no BT848 support",
            "opencv-python is excluded on macOS by requirements.txt. The "
            "BT848 capture card needs a Windows host or a USB capture stick "
            "that macOS can drive. The temperature link is unaffected.",
        ))
        return

    # Linux and anything else
    devices = sorted(
        p for p in (os.listdir("/dev") if os.path.isdir("/dev") else [])
        if p.startswith("video")
    )
    if devices:
        report.add(Check(
            "video backend",
            WARN,
            f"V4L2 devices present: {', '.join('/dev/' + d for d in devices)}",
            "There is no mainline Linux driver for the Conexant BT848, so "
            "these are almost certainly NOT the Linkam capture card. Use a "
            "USB capture stick or capture on a Windows host.",
        ))
    else:
        report.add(Check(
            "video backend",
            WARN,
            "no V4L2 devices found",
            "There is no mainline Linux driver for the Conexant BT848. "
            "Capture on a Windows host, or attach a USB capture stick. The "
            "temperature link does not need video.",
        ))

    if not have_cv2:
        report.add(Check(
            "dependency: cv2",
            WARN,
            "opencv-python not installed",
            "pip install opencv-python",
        ))


def _check_protocol(report: DiagnosticReport) -> None:
    report.add(Check(
        "protocol framing",
        INFO,
        "command terminator / checksum / baud are UNVERIFIED",
        "These can only be settled against real hardware. Run "
        "`python -m linkam.cli probe --port <port>` — it issues read-only "
        "queries only, so it is safe on a cold instrument.",
    ))
    report.add(Check(
        "stage control",
        INFO,
        "stage commands (MA/MM/MV/ZA) are not implemented",
        "This package drives the temperature programmer only. If your unit "
        "is an MDS gradient stage, positioning and stage registers are out "
        "of scope and will not work.",
    ))
    report.add(Check(
        "licence state",
        INFO,
        "the instrument may refuse to ramp when unlicensed",
        "Watch the status channel for 'Not licensed', 'Demonstration "
        "expired' or 'Comms locked off'. If the ramp is refused, "
        "wait_for_temperature raises ProgrammerNotRamping after "
        "stall_timeout seconds rather than hanging.",
    ))


def _check_tools(report: DiagnosticReport) -> None:
    if shutil.which("socat"):
        report.add(Check(
            "socat",
            OK,
            shutil.which("socat"),
            "Useful for capturing the real traffic if probe() does not "
            "settle the framing: "
            "socat -v -x /dev/ttyUSB0,raw,echo=0 -",
        ))
    else:
        report.add(Check(
            "socat",
            INFO,
            "not installed",
            "Optional. A serial sniffer is the fastest way to see exactly "
            "what the vendor software sends. Install with: apt install socat",
        ))


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def run_checks() -> DiagnosticReport:
    """Run every diagnostic check and return the report."""
    report = DiagnosticReport()
    _check_python(report)
    _check_serial(report)
    _check_video(report)
    _check_protocol(report)
    _check_tools(report)
    return report


def format_report(report: DiagnosticReport) -> str:
    """Render a report as plain text."""
    lines = []
    width = max((len(c.name) for c in report.checks), default=10)

    lines.append("Linkam environment check")
    lines.append("=" * 60)
    for c in report.checks:
        lines.append(f"{_SYMBOL.get(c.status, '[????]')} {c.name:<{width}}  {c.detail}")
        if c.hint:
            for chunk in _wrap(c.hint, 60):
                lines.append(f"{'':<{width + 7}}{chunk}")

    counts = report.counts()
    lines.append("")
    lines.append("=" * 60)
    lines.append(
        "summary: "
        f"{counts[OK]} ok, {counts[INFO]} info, {counts[WARN]} warn, "
        f"{counts[FAIL]} fail"
    )

    if report.notes:
        lines.append("")
        lines.append("Notes:")
        for note in report.notes:
            for i, chunk in enumerate(_wrap(note, 58)):
                lines.append(("  - " if i == 0 else "    ") + chunk)

    lines.append("")
    if report.worst == FAIL:
        lines.append("RESULT: blocked — fix the FAIL items above first.")
    elif report.worst == WARN:
        lines.append("RESULT: usable, with warnings (see above).")
    else:
        lines.append("RESULT: looks good. Next step: probe the instrument.")

    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    """Wrap text to a width, preserving paragraph breaks."""
    out: list[str] = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        line = ""
        for word in para.split():
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line += " " + word
            else:
                out.append(line)
                line = word
        if line:
            out.append(line)
    return out
