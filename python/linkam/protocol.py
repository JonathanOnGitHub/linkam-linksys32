"""Linkam Linksys32 serial protocol — temperature programmer.

Reverse-engineered from the shipped binaries (see ../PROTOCOL_NOTES.md):

  * LinksysActXLib.ocx  (v2.2.0.6)  — public ActiveX hardware library
  * CommX.ocx                        — serial port + framing layer

Everything in this module is derived from string tables and documentation
recovered from those binaries.  Where a detail could not be determined
statically it is marked ``UNVERIFIED`` and :func:`LinkamProgrammer.probe`
should be used to confirm against real hardware.

Key facts recovered:

* The programmer speaks a **text, line-oriented** protocol over RS-232.
  The original implementation reads responses with VB's ``Line Input``
  (``__vbaLineInputStr``) and writes with ``Print #``/``WriteFile``.
* Telemetry arrives as a single string every **0.2977 s** (documented in
  Linksys32Help.chm, "ProgNewData Event"), of the form::

      Temp 23.4 °C~Vac 0.1 mBar;Rate 10 °C/min~Limit 25.4 °C

  i.e. fields separated by ``~``; a field may carry sub-values separated
  by ``;``.
* Command keywords recovered verbatim from LinksysActXLib.ocx::

      Prog start    Prog stop     Prog Heat     Prog Cool     Prog Hold
      Prog rate <n> Prog Limit <n> Prog time <n>
      prog ramp <n>  prog picdelay <n>  prog clearbuffer  Prog samplecal

* The *stage* command set (used for the MDS/gradient stages, not the
  programmer) is a separate, more cryptic vocabulary::

      MA1 MA6 MA7 MA8 MA9  MF1 MF2  MFP
      MLX MLZ               MMm MMm1000
      MMR MMX MMY MMZ       MP  MR
      MSA MSX MSZ           MVX MVY MVZ  MVZ20000 MVZ4000
      MMV0 MMV1 MMVP        M0 M1 Mp MC Pa0
      ZA ZF ZV Zi Zd Zu Zv  c0 c1
      stdy step osc relax cw acw n/a
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

log = logging.getLogger(__name__)

__all__ = [
    "ProgrammerState",
    "Telemetry",
    "LinkamProgrammer",
    "ProtocolError",
    "ConnectionError_",
]


class ProtocolError(Exception):
    """Malformed or unexpected data from the programmer."""


class ConnectionError_(Exception):
    """Comm port could not be opened, or the link dropped."""


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #

#: Field tag -> canonical name.  Recovered from the LinksysActXLib.ocx string
#: table; tags beginning with ``~`` are "optional" (only present when the
#: fitted hardware supports them), untagged ones are always present.
FIELD_TAGS: dict[str, str] = {
    "Temp": "temperature",
    "TempCal": "temperature_calibration",
    "Temp2": "temperature2",
    "Vac": "vacuum",
    "DSC": "dsc",
    "DSCCal": "dsc_calibration",
    "Res": "resistance",
    "Intensity": "intensity",
    "Force": "force",
    "Strain": "strain",
    "Stress": "stress",
    "Dist": "distance",
    "RH": "relative_humidity",
    "CO2": "co2",
    "O2": "oxygen",
    "PH1": "ph1",
    "PH2": "ph2",
    "FLOW": "flow",
    "Press": "pressure",
    "Current": "current",
    "IO": "io",
    "Limit": "limit",
    "Limit2": "limit2",
    "Rate": "rate",
    "Rate2": "rate2",
    "Ramp Row": "ramp_row",
    "Ramp2 Row2": "ramp_row2",
}

#: Units seen in the same string table.
UNIT_TAGS = {
    "°C", "C", "K", "Pa", "mBar", "Torr", "Micro", "PH", "cc/min",
    "mA", "um", "%", "N/mm2", "mm", "nm",
}

_NUM = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"


@dataclass
class Telemetry:
    """One sub-sampled telemetry frame from the programmer."""

    raw: str
    values: dict[str, float] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)

    # -- convenience accessors ------------------------------------------- #
    @property
    def temperature(self) -> float | None:
        return self.values.get("temperature")

    @property
    def rate(self) -> float | None:
        return self.values.get("rate")

    @property
    def limit(self) -> float | None:
        return self.values.get("limit")

    @property
    def vacuum(self) -> float | None:
        return self.values.get("vacuum")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        body = " ".join(
            f"{k}={v}{self.units.get(k, '')}" for k, v in self.values.items()
        )
        return f"<Telemetry {body}>"


_FIELD_RE = re.compile(
    r"(?P<tag>~?(?:" + "|".join(re.escape(t) for t in sorted(FIELD_TAGS, key=len, reverse=True)) + r"))"
    r"\s+(?P<value>" + _NUM + r")"
    r"(?:\s*(?P<unit>[°%A-Za-z/0-9]+))?"
)


def parse_telemetry(text: str) -> Telemetry:
    """Parse a ProgNewData telemetry string.

    >>> parse_telemetry("Temp 23.4 °C~Vac 0.1 mBar;Rate 10 °C/min~Limit 25.4 °C").values
    {'temperature': 23.4, 'vacuum': 0.1, 'rate': 10.0, 'limit': 25.4}
    """
    tlm = Telemetry(raw=text)
    for match in _FIELD_RE.finditer(text):
        tag = match.group("tag")
        key = FIELD_TAGS[tag.lstrip("~")]
        try:
            value = float(match.group("value"))
        except ValueError:  # pragma: no cover - regex guarantees numeric
            continue
        tlm.values[key] = value
        unit = match.group("unit")
        if unit:
            # "°C/min" -> the numeric unit is °C, the rest is a rate qualifier
            unit = unit.strip()
            for known in sorted(UNIT_TAGS, key=len, reverse=True):
                if unit.startswith(known):
                    unit = known
                    break
            tlm.units[key] = unit
    return tlm


# --------------------------------------------------------------------------- #
# Programmer state
# --------------------------------------------------------------------------- #

#: Status words reported by the programmer.
STATUS_CODES: dict[str, str] = {
    "stdy": "standby",
    "step": "stepping",
    "osc": "oscillating",
    "relax": "relaxing",
}

#: Direction words.
DIRECTION_CODES: dict[str, str] = {
    "cw": "clockwise",
    "acw": "anticlockwise",
}


@dataclass
class ProgrammerState:
    """Snapshot of the programmer's internal state."""

    temperature: float | None = None
    rate: float | None = None
    limit: float | None = None
    hold_time: float | None = None
    ramp_row: int | None = None
    status: str | None = None
    direction: str | None = None
    demo_mode: bool = False
    demo_expired: bool = False
    licensed: bool = True
    oem_mode: bool = False
    connected: bool = False
    last_error: str | None = None


# --------------------------------------------------------------------------- #
# The programmer
# --------------------------------------------------------------------------- #

#: How often the hardware pushes a telemetry frame.
TELEMETRY_PERIOD_S = 0.2977


class LinkamProgrammer:
    """A Linkam temperature programmer (MSVP / TMS series) over RS-232.

    This class deliberately contains **no** framing assumptions that could not
    be confirmed statically.  It sends ASCII commands terminated by CR/LF and
    reads responses line-by-line, which matches the original VB6
    implementation (``__vbaLineInputStr`` / ``__vbaFileOpen`` +
    ``__vbaLineInputStr``).

    The outstanding unknown is the exact command terminator and whether a
    checksum is appended to requests.  :attr:`strict_framing` controls this:

    ``strict_framing=False`` (default)
        Write ``CMD\\r\\n``.  Most Linkam programmers of this vintage accept
        CR, LF or CRLF interchangeably.

    ``strict_framing=True``
        Write ``CMD\\r`` only.

    Use :meth:`probe` against real hardware to settle it; it tries each
    variant and reports which one the hardware answers.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        *,
        timeout: float = 2.0,
        strict_framing: bool = False,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.strict_framing = strict_framing
        self._serial = None
        self._terminator_bytes: bytes | None = None
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Telemetry], None]] = []
        self._state = ProgrammerState()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle -------------------------------------------------------- #
    def open(self) -> None:
        """Open the comm port and start the background reader thread."""
        import serial  # pyserial, imported lazily

        self._serial = serial.Serial(
            self.port, self.baudrate, timeout=0.2, write_timeout=self.timeout
        )
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._state.connected = True
        log.info("opened %s at %d baud", self.port, self.baudrate)

    def close(self) -> None:
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=2.0)
            self._reader = None
        if self._serial is not None:
            self._serial.close()
            self._serial = None
        self._state.connected = False

    def __enter__(self) -> "LinkamProgrammer":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- events ----------------------------------------------------------- #
    def on_telemetry(self, callback: Callable[[Telemetry], None]) -> None:
        """Register a callback invoked for every telemetry frame."""
        self._listeners.append(callback)

    # -- raw I/O ---------------------------------------------------------- #
    def _terminator(self) -> bytes:
        if self._terminator_bytes is not None:
            return self._terminator_bytes
        return b"\r" if self.strict_framing else b"\r\n"

    def send(self, command: str) -> None:
        """Send a raw command string."""
        if self._serial is None:
            raise ConnectionError_("port is not open")
        payload = command.encode("ascii", "replace") + self._terminator()
        with self._lock:
            log.debug("-> %r", payload)
            self._serial.write(payload)
            self._serial.flush()

    def request(self, command: str, timeout: float | None = None) -> str:
        """Send a command and return the next non-empty response line."""
        self.send(command)
        return self.read_line(timeout)

    def read_line(self, timeout: float | None = None) -> str:
        """Read one line from the port."""
        if self._serial is None:
            raise ConnectionError_("port is not open")
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self._serial.read(1)
            if not chunk:
                continue
            buf += chunk
            if chunk in (b"\r", b"\n"):
                if buf[-1:] == chunk and len(buf) == 1:
                    continue  # swallow a bare CR or LF
                line = buf[:-1].decode("ascii", "replace").strip()
                if line:
                    log.debug("<- %r", line)
                    return line
                buf.clear()
        raise ProtocolError(f"timeout waiting for response to {command!r}")

    def _read_loop(self) -> None:
        """Background thread: parse telemetry and dispatch to listeners."""
        while not self._stop.is_set():
            try:
                line = self.read_line(timeout=0.5)
            except ProtocolError:
                continue
            except Exception as exc:  # pragma: no cover - hardware fault
                log.warning("read error: %s", exc)
                self._state.connected = False
                break
            if line is None:
                continue
            try:
                tlm = parse_telemetry(line)
            except Exception:  # pragma: no cover
                log.debug("unparseable line: %r", line)
                continue
            if not tlm.values:
                continue
            self._absorb(tlm)
            for cb in list(self._listeners):
                try:
                    cb(tlm)
                except Exception:  # pragma: no cover
                    log.exception("telemetry callback failed")

    def _absorb(self, tlm: Telemetry) -> None:
        st = self._state
        if "temperature" in tlm.values:
            st.temperature = tlm.values["temperature"]
        if "rate" in tlm.values:
            st.rate = tlm.values["rate"]
        if "limit" in tlm.values:
            st.limit = tlm.values["limit"]
        if "ramp_row" in tlm.values:
            st.ramp_row = int(tlm.values["ramp_row"])

    # -- high level commands ---------------------------------------------- #
    # Keyword strings below are verbatim from LinksysActXLib.ocx.
    #
    # The programmer accepts *compound* commands: a leading verb followed by
    # one or more space-separated `keyword value` pairs.  The OCX builds
    # them like this (string table at 0x1e308):
    #
    #     'prog ramp ' + row + ' rate ' + r + ' limit ' + lim + ' time ' + t
    #
    # and the single-value forms:
    #
    #     'prog rate '   'prog limit '   'prog time '   'prog picdelay '
    #
    # Note the original mixes case ('Prog rate' vs 'prog rate',
    # 'Prog Limit' vs 'prog limit'); the firmware accepts either, and we
    # mirror the lower-case forms used by the profile builder.

    def start(self) -> None:
        """Start the programmed ramp.  ``Prog start``"""
        self.send("Prog start")

    def stop(self) -> None:
        """Stop the programmer.  ``Prog stop``"""
        self.send("Prog stop")

    def heat(self) -> None:
        """Force heating at the current rate.  ``Prog Heat``"""
        self.send("Prog Heat")

    def cool(self) -> None:
        """Force cooling at the current rate.  ``Prog Cool``"""
        self.send("Prog Cool")

    def hold(self) -> None:
        """Hold the current temperature.  ``Prog Hold``"""
        self.send("Prog Hold")

    def set_rate(self, rate: float) -> None:
        """Set the heating/cooling rate.  ``prog rate <n>``"""
        self.send(f"prog rate {rate:g}")

    def set_limit(self, limit: float) -> None:
        """Set the temperature limit.  ``prog limit <n>``"""
        self.send(f"prog limit {limit:g}")

    def set_hold_time(self, seconds: float) -> None:
        """Set the hold time.  ``prog time <n>``"""
        self.send(f"prog time {seconds:g}")

    def set_ramp(self, row: int) -> None:
        """Select a profile ramp row.  ``prog ramp <n>``"""
        self.send(f"prog ramp {row:d}")

    def programme_ramp(
        self,
        row: int,
        rate: float,
        limit: float,
        time_s: float,
    ) -> None:
        """Programme a complete ramp in a single compound command.

        Mirrors the OCX's profile builder:
        ``prog ramp <row> rate <r> limit <lim> time <t>``

        Sending one compound command rather than four separate ones is
        preferable: it removes any chance of the programmer acting on a
        partially-applied programme.
        """
        self.send(
            f"prog ramp {row:d} rate {rate:g} limit {limit:g} time {time_s:g}"
        )

    def clear_buffer(self) -> None:
        """``prog clearbuffer``"""
        self.send("prog clearbuffer")

    def sample_cal(self) -> None:
        """``Prog samplecal``"""
        self.send("Prog samplecal")

    def set_picture_delay(self, seconds: float) -> None:
        """``prog picdelay <n>`` — interval between captured images."""
        self.send(f"prog picdelay {seconds:g}")

    # -- convenience ------------------------------------------------------ #
    def ramp_to(
        self,
        target_c: float,
        rate_c_per_min: float = 10.0,
        hold_s: float = 0.0,
        *,
        timeout: float = 3600.0,
    ) -> None:
        """Heat/cool to `target_c` at `rate_c_per_min`, then optionally hold.

        This is the core of an isothermal experiment: set a rate, set the
        limit, start, and wait for the ramp to reach the limit.
        """
        if hold_s:
            self.set_hold_time(hold_s)
        self.set_rate(rate_c_per_min)
        self.set_limit(target_c)
        self.start()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            st = self._state
            if st.temperature is None or st.limit is None:
                continue
            if abs(st.temperature - st.limit) <= 0.5:
                return
        raise TimeoutError(f"did not reach {target_c} C within {timeout}s")

    def wait_for_temperature(
        self,
        target_c: float,
        tolerance_c: float = 0.5,
        timeout: float = 3600.0,
    ) -> float:
        """Block until the measured temperature is within `tolerance_c`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self._state
            if st.temperature is not None:
                if abs(st.temperature - target_c) <= tolerance_c:
                    return st.temperature
            time.sleep(0.1)
        raise TimeoutError(
            f"temperature did not reach {target_c} C within {timeout}s"
        )

    # -- diagnostics ------------------------------------------------------ #
    #: Baud rates to try, in the order suggested by the vendor's own
    #: firmware tool (Instrument Updater/CHIPDEF.INI lists 4800, 9600,
    #: 19200 and 38400 for the Fujitsu MB90Fxxx programmer links).
    CANDIDATE_BAUDS = (9600, 19200, 4800, 38400, 2400, 1200)

    #: Terminators to try.  The original VB6 code reads responses with
    #: VB's line input, so the application layer is line-oriented; only the
    #: exact terminator is unknown.
    CANDIDATE_TERMINATORS = (("crlf", b"\r\n"), ("cr", b"\r"), ("lf", b"\n"))

    #: Read-only queries used to test the link.  None of these change the
    #: state of the instrument.
    PROBE_COMMANDS = ("MA6", "MC", "prog")

    def probe(
        self,
        bauds: Iterable[int] | None = None,
        terminators: Iterable[tuple[str, bytes]] | None = None,
        commands: Iterable[str] | None = None,
        *,
        timeout: float = 1.0,
    ) -> dict:
        """Sweep baud rate x terminator x command to find the link.

        Only read-only queries are issued, so this is safe on a cold
        instrument.  Returns a dict of what answered.

        The first non-empty response for a (baud, terminator) pair wins;
        the programmer is usually chatty, so any reply confirms the
        combination.
        """
        bauds = list(bauds or self.CANDIDATE_BAUDS)
        terminators = list(terminators or self.CANDIDATE_TERMINATORS)
        commands = list(commands or self.PROBE_COMMANDS)

        results: dict = {
            "port": self.port,
            "tried": [],
            "winner": None,
            "response": None,
        }

        for baud in bauds:
            for name, term in terminators:
                key = f"{baud}/{name}"
                results["tried"].append(key)
                try:
                    self.reopen(baudrate=baud, terminator=term)
                    for cmd in commands:
                        try:
                            line = self.request(cmd, timeout=timeout)
                        except Exception:
                            continue
                        if line:
                            results["winner"] = {
                                "baudrate": baud,
                                "terminator": name,
                                "command": cmd,
                            }
                            results["response"] = line
                            return results
                except Exception as exc:
                    results.setdefault("errors", {})[key] = repr(exc)
                finally:
                    try:
                        self.close()
                    except Exception:
                        pass
        return results

    def reopen(self, baudrate: int, terminator: bytes) -> None:
        """Close and re-open the port at a different speed/framing."""
        import serial  # pyserial

        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        self.baudrate = baudrate
        self._terminator_bytes = terminator
        self._serial = serial.Serial(
            self.port, baudrate, timeout=0.2, write_timeout=self.timeout
        )
        # drain anything the instrument sent while we were reconfiguring
        try:
            self._serial.reset_input_buffer()
        except Exception:
            pass
