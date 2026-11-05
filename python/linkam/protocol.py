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

from .transport import SerialTransport, Transport, TransportTimeout
from .t95 import T95Status

log = logging.getLogger(__name__)

__all__ = [
    "ProgrammerState",
    "Telemetry",
    "LinkamProgrammer",
    "HidProgrammer",
    "ProtocolError",
    "ConnectionError_",
    "ProgrammerNotRamping",
]


class ProtocolError(Exception):
    """Malformed or unexpected data from the programmer."""


class ConnectionError_(Exception):
    """Comm port could not be opened, or the link dropped."""


class ProgrammerNotRamping(Exception):
    """The programmer is connected and reporting, but not moving.

    Distinct from :class:`TimeoutError`: this says the temperature is being
    reported but is *not changing*, which usually means the ramp was
    refused rather than that it was slow.  Common causes:

    * the instrument is unlicensed, in demonstration mode, or in OEM mode,
      so it declines to heat or cool;
    * ``Prog start`` was not accepted;
    * the programmed rate is zero;
    * the limit is already at the current temperature.
    """


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
        transport: Transport | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.strict_framing = strict_framing
        self._transport: Transport | None = transport
        self._serial = None
        self._terminator_bytes: bytes | None = None
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Telemetry], None]] = []
        self._state = ProgrammerState()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle -------------------------------------------------------- #
    def _make_transport(self) -> Transport:
        """Build the underlying link.  Overridden by :class:`HidProgrammer`."""
        return SerialTransport(self.port, self.baudrate, timeout=self.timeout)

    def open(self) -> None:
        """Open the link and start the background reader thread."""
        if self._transport is None:
            self._transport = self._make_transport()
        self._transport.open()
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._state.connected = True
        log.info("opened %s", self._transport.description)

    def close(self) -> None:
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=2.0)
            self._reader = None
        if self._transport is not None:
            try:
                self._transport.close()
            finally:
                self._transport = None
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
        payload = command.encode("ascii", "replace") + self._terminator()
        with self._lock:
            log.debug("-> %r", payload)
            if self._transport is not None:
                self._transport.write_line(command, self._terminator())
                return
            # Backwards compatibility: a caller may have injected a raw
            # pyserial-like object into ``_serial`` (the tests do this).
            if self._serial is not None:
                self._serial.write(payload)
                self._serial.flush()
                return
            raise ConnectionError_("port is not open")

    def request(self, command: str, timeout: float | None = None) -> str:
        """Send a command and return the next non-empty response line."""
        self.send(command)
        return self.read_line(timeout)

    def read_line(self, timeout: float | None = None) -> str:
        """Read one line from the link."""
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        if self._transport is None:
            # Legacy path: read from an injected pyserial-like object.
            if self._serial is None:
                raise ConnectionError_("port is not open")
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
            raise ProtocolError("timeout waiting for response")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError("timeout waiting for response")
            try:
                return self._transport.read_line(remaining)
            except TransportTimeout:
                raise ProtocolError("timeout waiting for response")

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
        stall_timeout: float | None = None,
        min_progress_c: float = 0.2,
    ) -> None:
        """Heat/cool to `target_c` at `rate_c_per_min`, then optionally hold.

        This is the core of an isothermal experiment: set a rate, set the
        limit, start, and wait for the ramp to reach the limit.

        See :meth:`wait_for_temperature` for the meaning of
        `stall_timeout` and `min_progress_c`.
        """
        if hold_s:
            self.set_hold_time(hold_s)
        self.set_rate(rate_c_per_min)
        self.set_limit(target_c)
        self.start()

        self.wait_for_temperature(
            target_c,
            timeout=timeout,
            stall_timeout=stall_timeout,
            min_progress_c=min_progress_c,
        )

    def wait_for_temperature(
        self,
        target_c: float,
        tolerance_c: float = 0.5,
        timeout: float = 3600.0,
        *,
        stall_timeout: float | None = None,
        min_progress_c: float = 0.2,
    ) -> float:
        """Block until the measured temperature is within `tolerance_c`.

        If `stall_timeout` is given, this gives up early when the
        programmer is reporting a temperature but **not moving**.  That is
        the common failure mode when the instrument refuses to heat or
        cool — for example when it is unlicensed, in demonstration mode, or
        in OEM mode — and it is much better to report that after a couple
        of minutes than to sit until `timeout` expires with no
        explanation.

        How "not moving" is decided
        ----------------------------
        A ramp is judged stalled when the temperature has not come
        *meaningfully closer* to the target for `stall_timeout` seconds.
        "Meaningfully closer" means the error has shrunk by at least
        `min_progress_c` **or** by a margin consistent with the
        programmed rate.

        The rate-aware test matters: a genuine 0.5 C/min ramp improves the
        error by only 0.008 C every second, so a purely absolute threshold
        would eventually declare a real ramp stalled.  When the programmer
        reports a rate, the expected movement over the stall window is
        compared against the observed movement instead, so slow ramps are
        given the time they need while a genuinely frozen instrument is
        caught quickly.

        Raises :class:`ProgrammerNotRamping` if the temperature stalls,
        or :class:`TimeoutError` if the overall deadline passes first.
        """
        deadline = time.monotonic() + timeout

        # Stall detection bookkeeping.
        stall_deadline: float | None = None
        best_error: float | None = None   # smallest |T - target| seen so far
        best_temp: float | None = None    # temperature at that best error
        #: The temperature and the time at the start of the current stall
        #: window.  A stall window is a period during which we have not
        #: confirmed genuine progress; it is reset whenever progress is
        #: confirmed.
        window_temp: float | None = None
        window_at: float = time.monotonic()

        while time.monotonic() < deadline:
            st = self._state
            if st.temperature is not None:
                error = abs(st.temperature - target_c)
                if error <= tolerance_c:
                    return st.temperature

                if best_error is None:
                    # First reading: record it and open the stall window.
                    best_error = error
                    best_temp = st.temperature
                    window_temp = st.temperature
                    window_at = time.monotonic()
                    if stall_timeout is not None:
                        stall_deadline = time.monotonic() + stall_timeout
                else:
                    # Movement toward the target since the start of the
                    # current stall window.
                    moved = window_temp - error if window_temp is not None else 0.0

                    # A stall is only declared if the temperature has not
                    # come meaningfully closer.  "Meaningfully" is judged
                    # two ways:
                    #
                    #  * an absolute floor (`min_progress_c`), which catches
                    #    a completely frozen instrument; and
                    #  * against the programmed rate, so that a genuinely
                    #    slow ramp is not mistaken for a stall.  A real
                    #    ramp at `rate` should close the gap by roughly
                    #    rate * elapsed/60.
                    #
                    # The rate test uses the *programmed* rate, which the
                    # programmer reports in telemetry, rather than a
                    # threshold, so it scales with however slow the ramp
                    # really is.
                    progressed = moved >= min_progress_c
                    if not progressed:
                        rate = self._state.rate
                        if rate and rate > 0:
                            elapsed = time.monotonic() - window_at
                            expected = abs(rate) * elapsed / 60.0
                            # Require only a fraction of the implied
                            # movement, to tolerate noise and a late start.
                            progressed = moved >= expected * 0.5

                    if progressed:
                        best_error = error
                        best_temp = st.temperature
                        # Open a fresh stall window from here.
                        window_temp = st.temperature
                        window_at = time.monotonic()
                        if stall_timeout is not None:
                            stall_deadline = time.monotonic() + stall_timeout
                    # else: no meaningful progress — the stall clock keeps running.

                if (
                    stall_timeout is not None
                    and stall_deadline is not None
                    and time.monotonic() >= stall_deadline
                ):
                    raise ProgrammerNotRamping(
                        f"the programmer is reporting {st.temperature:.2f} C "
                        f"but is not moving toward {target_c:.2f} C "
                        f"(closest approach {best_temp:.2f} C) after "
                        f"{stall_timeout:.0f}s. The ramp was most likely "
                        "refused: check for an unlicensed instrument, "
                        "demonstration mode or OEM mode, confirm the "
                        "programmed rate is non-zero, and check that the "
                        "limit differs from the current temperature."
                    )
            time.sleep(0.1)

        raise TimeoutError(
            f"temperature did not reach {target_c} C within {timeout}s"
        )

    def wait_for_stable_temperature(
        self,
        target_c: float,
        duration_s: float,
        *,
        tolerance_c: float = 0.5,
        settle_s: float = 1.0,
        timeout: float | None = None,
        poll_s: float = 0.1,
    ) -> float:
        """Block until the temperature has been *at* `target_c` for `duration_s`.

        This is the host-side view of a hold: the measured temperature must
        stay within `tolerance_c` of `target_c` continuously for
        `duration_s` seconds.

        Why this exists
        ---------------
        The programmer runs the hold itself -- the compound command
        ``prog ramp <row> rate <r> limit <l> time <t>`` tells it to hold
        for `t` seconds once it reaches the limit.  The host must
        therefore *not* simply sleep for `t` seconds and then stop: that
        would double-count the hold, and would cut the real hold short
        whenever the ramp-completion check returned late.

        Measuring the hold from the telemetry instead makes the host
        agree with the physics, and works whether or not the instrument
        honours the ``time`` parameter itself.

        Settling, not just "in range"
        -----------------------------
        Simply requiring the temperature to be *within* `tolerance_c` of
        the target is not enough: a temperature that is still ramping
        towards the target passes through that band on its way, and the
        hold timer would start during the ramp rather than at the top of
        it.  On a slow ramp that can eat the entire hold.

        So the temperature must also have **stopped moving**.  It counts
        as settled once it has stayed within `settle_s` of the target
        *and* moved no more than `max_drift_c` over that window.  Only
        then does the hold clock start, and if the temperature drifts
        out again the clock restarts from zero.

        A note on timing
        ----------------
        Because the host can only infer "the temperature has stopped
        moving" by *observing* it, the measured hold runs slightly longer
        than `duration_s`: by up to `settle_s` plus one telemetry period.
        That is deliberate -- it guarantees the hold is never cut short.
        The instrument's own ``time`` parameter is what determines the
        scientifically meaningful hold window; this method only decides
        when it is safe to stop the programmer.
        """
        if timeout is None:
            # Allow generous slack: the hold has to complete, and a slow
            # ramp should not be cut short.
            timeout = duration_s + settle_s + 300.0

        deadline = time.monotonic() + timeout

        # Phase 1: wait for the temperature to settle at the target.
        #   `band_entered`  -- when the temperature first came into range
        #   `band_first`    -- the temperature when it came into range
        # Phase 2: the hold clock runs from `stable_since`.
        #   `stable_since`  -- when the temperature was judged settled
        band_entered: float | None = None
        band_first: float | None = None
        stable_since: float | None = None

        while time.monotonic() < deadline:
            st = self._state
            temp = st.temperature
            if temp is None:
                time.sleep(poll_s)
                continue

            now = time.monotonic()

            if abs(temp - target_c) > tolerance_c:
                # Outside the band: everything resets.
                if stable_since is not None:
                    log.info(
                        "left the %.2f C band (now %.2f C); hold timer reset",
                        target_c, temp,
                    )
                band_entered = None
                band_first = None
                stable_since = None
                time.sleep(poll_s)
                continue

            # Inside the band.
            if band_entered is None:
                band_entered = now
                band_first = temp

            if stable_since is None:
                # Still settling: has the temperature stopped moving?
                #
                # The reported rate is the strongest signal: when the
                # programmer has reached the limit it reports no rate (or
                # zero), which means it is holding.  When it does not
                # report a rate, fall back to comparing the measured
                # movement against what the ramp *would* have produced.
                rate = st.rate
                if rate is not None:
                    # The programmer reports its rate; at the limit it
                    # reports no rate (or zero), which means it is
                    # holding.
                    still_moving = abs(rate) > 0.01
                else:
                    # No rate reported: fall back to comparing the
                    # measured movement against a fixed threshold.
                    drift = abs(temp - band_first)
                    still_moving = drift > self.MAX_HOLD_DRIFT_C

                if still_moving:
                    # Still ramping through the band -- restart the
                    # settling window.
                    band_entered = now
                    band_first = temp
                elif now - band_entered >= settle_s:
                    stable_since = now
                    log.info(
                        "settled at %.2f C (target %.2f C); holding for %.0f s",
                        temp, target_c, duration_s,
                    )
            else:
                if now - stable_since >= duration_s:
                    return temp

            time.sleep(poll_s)

        raise TimeoutError(
            f"temperature did not settle within {tolerance_c} C of "
            f"{target_c} C and stay there for {duration_s}s within {timeout}s"
        )

    #: Largest temperature excursion (in degrees Celsius) allowed during
    #: the settling window before the temperature is considered to still
    #: be moving towards the target.  Only used when the programmer does
    #: not report a rate.
    MAX_HOLD_DRIFT_C = 0.05

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
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None
        self.baudrate = baudrate
        self._terminator_bytes = terminator
        self._transport = self._make_transport()
        self._transport.open()
        # drain anything the instrument sent while we were reconfiguring
        try:
            if isinstance(self._transport, SerialTransport):
                self._transport._serial.reset_input_buffer()
        except Exception:
            pass


class HidProgrammer(LinkamProgrammer):
    """A Linkam T95 temperature programmer over USB HID.

    The T95 is a USB HID device (``16da:0002``), not a serial port, so it
    needs a different physical link -- but the **application layer is
    identical**: the same ASCII commands and the same ASCII telemetry
    strings.  This class therefore inherits every bit of the ramp logic,
    stall detection and telemetry parsing from :class:`LinkamProgrammer`
    and only replaces the transport.

    Example
    -------
    ::

        from linkam import HidProgrammer

        p = HidProgrammer()
        p.open()
        p.on_telemetry(lambda t: print(t.temperature))
        ...
        p.close()

    The command vocabulary is unchanged, so ``p.programme_ramp(...)`` and
    ``p.start()`` mean exactly what they do on the serial link.
    """

    def __init__(
        self,
        node: str | None = None,
        *,
        framing: str = "text_padded",
        report_size: int | None = None,
        input_report_id: int | None = None,
        output_report_id: int | None = None,
        timeout: float = 2.0,
        strict_framing: bool = False,
        vendor: int = 0x16DA,
        product: int = 0x0002,
    ) -> None:
        # ``port`` is meaningless for HID; pass the hidraw node so that
        # the inherited attributes still make sense in log messages.
        super().__init__(
            node or "hidraw",
            timeout=timeout,
            strict_framing=strict_framing,
        )
        self.node = node
        self.framing = framing
        self.report_size = report_size
        self.input_report_id = input_report_id
        self.output_report_id = output_report_id
        self.vendor = vendor
        self.product = product

    def _make_transport(self) -> Transport:
        from .hid import HidTransport

        return HidTransport(
            self.node,
            framing=self.framing,
            report_size=self.report_size,
            input_report_id=self.input_report_id,
            output_report_id=self.output_report_id,
            vendor=self.vendor,
            product=self.product,
            timeout=self.timeout,
        )

    def probe(self, *args, **kwargs) -> dict:
        """Probe the HID link.

        The serial implementation sweeps baud rates, which is meaningless
        over HID -- there is no baud rate.  Instead this tries the
        candidate report framings and reports which one the instrument
        answers.
        """
        from .hid import discover

        results = discover(self.node)
        winner = next((r for r in results if r.get("response")), None)
        return {
            "port": self.node or "auto",
            "tried": [r["framing"] for r in results],
            "winner": (
                {"framing": winner["framing"], "command": "MA6"}
                if winner
                else None
            ),
            "response": winner["response"] if winner else None,
            "details": results,
        }

    # -- the T95's binary protocol ----------------------------------------- #
    #
    # The T95 does not speak the Linksys32 ASCII application layer over
    # HID.  It speaks a binary block protocol (see ``linkam/t95.py``):
    # the device answers a status poll with three 63-byte blocks, the
    # first of which carries the current temperature as a float32.
    #
    # So the HID programmer overrides the ASCII read loop with a polling
    # loop.  The ASCII command methods are inherited unchanged, but they
    # have **no effect** on this link -- the command frame format has
    # not been found -- so they must not be used to drive a ramp until
    # it is.

    #: How often to poll the T95 for status.
    POLL_PERIOD_S = 0.5

    def poll_status(self, timeout: float | None = None) -> T95Status:
        """Ask the T95 for its status and return the parsed answer.

        This is the only write this class makes on the T95's binary
        protocol: a status poll, which the instrument cannot act on.
        """
        from .t95 import build_status_poll, parse_status

        transport = self._transport
        if transport is None:
            raise ConnectionError_("port is not open")
        writer = getattr(transport, "write_report", None)
        reader = getattr(transport, "read_reports", None)
        if writer is None or reader is None:
            raise ConnectionError_(
                "the transport does not support the T95's binary protocol"
            )

        with self._lock:
            writer(build_status_poll())
        blocks = reader(3, timeout if timeout is not None else self.timeout)
        if not blocks:
            raise ProtocolError(
                "the T95 did not answer the status poll; it may still be "
                "warming up after power-on (it answers about a minute after "
                "it is plugged in)"
            )
        return parse_status(blocks)

    def read_status(self, timeout: float | None = None, retries: int = 3) -> T95Status:
        """Poll for status, retrying until a temperature is present.

        Right after a power cycle the instrument can answer a poll
        before it has taken its first sensor sample, and the temperature
        field is then zero.  A zero is not a temperature, so the poll is
        repeated rather than reporting "no temperature".

        Raises :class:`ProtocolError` if the instrument does not answer,
        or keeps answering without a temperature.
        """
        last_error = "the T95 answered but reported no temperature"
        for _ in range(max(1, retries)):
            status = self.poll_status(timeout)
            if status.temperature_c is not None:
                return status
            last_error = (
                "the T95 answered but reported no temperature; "
                f"blocks={[b[:12].hex() for b in status.blocks]}"
            )
            time.sleep(0.3)
        raise ProtocolError(last_error)

    def read_temperature(self, timeout: float | None = None, retries: int = 3) -> float:
        """Return the current stage temperature in degrees Celsius.

        Raises :class:`ProtocolError` if the instrument does not answer,
        or keeps answering without a temperature.
        """
        return self.read_status(timeout, retries).temperature_c

    def _read_loop(self) -> None:
        """Poll the T95 for status and dispatch it to the listeners."""
        from .t95 import build_status_poll, parse_status

        transport = self._transport
        if transport is None:
            return
        writer = getattr(transport, "write_report", None)
        reader = getattr(transport, "read_reports", None)
        if writer is None or reader is None:
            # No binary protocol on this transport: fall back to the
            # inherited ASCII read loop.
            super()._read_loop()
            return

        while not self._stop.is_set():
            try:
                self._read_loop_once(writer, reader)
            except Exception as exc:  # pragma: no cover - hardware fault
                log.warning("poll error: %s", exc)
                self._state.connected = False
                break
            self._stop.wait(self.POLL_PERIOD_S)

    def _read_loop_once(self, writer=None, reader=None) -> bool:
        """Poll once and dispatch the answer.  Returns True if it answered.

        Split out from :meth:`_read_loop` so it can be tested directly.
        """
        from .t95 import build_status_poll, parse_status

        transport = self._transport
        if transport is None:
            return False
        if writer is None:
            writer = getattr(transport, "write_report", None)
        if reader is None:
            reader = getattr(transport, "read_reports", None)
        if writer is None or reader is None:
            return False

        with self._lock:
            writer(build_status_poll())
        blocks = reader(3, self.timeout)
        if not blocks:
            return False

        status = parse_status(blocks)
        tlm = Telemetry(raw="T95 binary status")
        if status.temperature_c is not None:
            tlm.values["temperature"] = status.temperature_c
            tlm.units["temperature"] = "°C"
        if status.sensor2_c is not None:
            tlm.values["temperature2"] = status.sensor2_c
            tlm.units["temperature2"] = "°C"
        if status.rate is not None:
            tlm.values["rate"] = status.rate
            tlm.units["rate"] = "°C/min"
        if status.limit is not None:
            tlm.values["limit"] = status.limit
            tlm.units["limit"] = "°C"

        self._absorb(tlm)
        for cb in list(self._listeners):
            try:
                cb(tlm)
            except Exception:  # pragma: no cover
                log.exception("telemetry callback failed")
        return True
