"""A software simulator of a Linkam temperature programmer.

This exists so you can rehearse the entire experiment workflow — command
generation, telemetry parsing, ramp sequencing, CSV output — with no
hardware attached.

It implements the subset of the protocol that matters for a temperature
ramp:

* parses compound commands (``prog ramp <row> rate <r> limit <l> time <t>``)
* ramps the simulated temperature at the programmed rate
* emits telemetry strings in the real wire format, every 0.2977 s
* honours ``prog start`` / ``prog stop`` / ``Prog Heat`` / ``Prog Cool`` /
  ``Prog Hold``

Wire format is exactly as the real hardware::

    Temp 23.4 °C~Rate 10 °C/min~Limit 25.4 °C

Use it with :class:`linkam.experiment.IsothermalRun` by passing it as the
``programmer`` argument, or run it standalone::

    python -m linkam.simulator --target 95 --rate 10
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from typing import Callable

from .protocol import TELEMETRY_PERIOD_S, LinkamProgrammer, Telemetry, parse_telemetry


class SimulatedProgrammer(LinkamProgrammer):
    """A LinkamProgrammer that behaves like the hardware, in software."""

    def __init__(
        self,
        *,
        start_c: float = 20.0,
        ambient_c: float = 20.0,
        max_rate_c_per_min: float = 130.0,
        noise_c: float = 0.05,
        vacuum: bool = False,
    ) -> None:
        super().__init__("/dev/null")
        self.temp_c = start_c
        self.ambient_c = ambient_c
        self.max_rate = max_rate_c_per_min
        self.noise_c = noise_c
        self.with_vacuum = vacuum

        self.rate = 0.0
        self.limit: float | None = None
        self.hold_s = 0.0
        self.hold_remaining = 0.0
        self.ramp_row = 0
        self.running = False
        self.mode = "standby"          # standby | heating | cooling | holding
        self.commands: list[str] = []

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._t = 0.0

    # -- lifecycle -------------------------------------------------------- #
    def open(self) -> None:
        self._stop_event.clear()
        self._state.connected = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._state.connected = False

    # -- command handling ------------------------------------------------- #
    def send(self, command: str) -> None:
        with self._lock:
            self.commands.append(command)
            self._handle(command)

    def _handle(self, command: str) -> None:
        parts = command.split()
        if not parts:
            return
        low = command.lower()

        if low.startswith("prog ramp"):
            kv = {}
            for i in range(3, len(parts) - 1, 2):
                kv[parts[i].lower()] = parts[i + 1]
            self.ramp_row = int(parts[2])
            if "rate" in kv:
                self.rate = float(kv["rate"])
            if "limit" in kv:
                self.limit = float(kv["limit"])
            if "time" in kv:
                self.hold_s = float(kv["time"])
        elif low.startswith("prog rate"):
            self.rate = float(parts[2])
        elif low.startswith("prog limit"):
            self.limit = float(parts[2])
        elif low.startswith("prog time"):
            self.hold_s = float(parts[2])
        elif low.startswith("prog picdelay"):
            pass
        elif low.startswith("prog start"):
            self.running = True
            self.mode = "standby"
        elif low.startswith("prog stop"):
            self.running = False
            self.mode = "standby"
            self.rate = 0.0
        elif low.startswith("prog heat"):
            self.running = True
            self.mode = "heating"
            self.limit = self.temp_c + 1000.0
        elif low.startswith("prog cool"):
            self.running = True
            self.mode = "cooling"
            self.limit = self.temp_c - 1000.0
        elif low.startswith("prog hold"):
            self.running = True
            self.mode = "holding"
            self.limit = self.temp_c
            self.hold_remaining = self.hold_s

    # -- simulation loop -------------------------------------------------- #
    def _step(self) -> None:
        """Advance the simulation by one telemetry tick."""
        self._t += TELEMETRY_PERIOD_S

        if not self.running:
            # drift back towards ambient
            self.temp_c += (self.ambient_c - self.temp_c) * 0.02
            return

        if self.mode == "standby" and self.limit is not None:
            # ramp towards the limit at the programmed rate
            if self.temp_c < self.limit - 0.05:
                self.mode = "heating"
            elif self.temp_c > self.limit + 0.05:
                self.mode = "cooling"
            else:
                self.mode = "holding"
                self.hold_remaining = self.hold_s

        if self.mode in ("heating", "cooling"):
            rate_per_tick = self.rate / 60.0 * TELEMETRY_PERIOD_S
            # don't overshoot the limit
            if self.limit is not None:
                if self.temp_c < self.limit:
                    self.temp_c = min(self.limit, self.temp_c + rate_per_tick)
                else:
                    self.temp_c = max(self.limit, self.temp_c - rate_per_tick)
            else:
                self.temp_c += rate_per_tick

        elif self.mode == "holding":
            self.hold_remaining = max(0.0, self.hold_remaining - TELEMETRY_PERIOD_S)
            # hold means stop ramping; the heater keeps the temperature
            if self.hold_remaining <= 0.0 and self.hold_s > 0:
                self.mode = "standby"
                self.rate = 0.0

    def _telemetry_string(self) -> str:
        t = self.temp_c
        # a touch of noise, as a real sensor would have
        import random

        t += random.uniform(-self.noise_c, self.noise_c)
        s = f"Temp {t:.2f} °C~Rate {self.rate:.1f} °C/min"
        if self.limit is not None:
            s += f"~Limit {self.limit:.2f} °C"
        if self.ramp_row:
            s += f"~Ramp Row {self.ramp_row}"
        if self.with_vacuum:
            s += "~Vac 0.10 mBar"
        return s

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._step()
            tlm = parse_telemetry(self._telemetry_string())
            self._absorb(tlm)
            for cb in list(self._listeners):
                try:
                    cb(tlm)
                except Exception:  # pragma: no cover
                    pass
            self._stop_event.wait(TELEMETRY_PERIOD_S)


# --------------------------------------------------------------------------- #
# Standalone demo
# --------------------------------------------------------------------------- #

def _demo(args) -> int:
    prog = SimulatedProgrammer(start_c=args.start, ambient_c=args.start)
    prog.on_telemetry(lambda t: print(f"  {t}"))
    prog.open()
    print(f"programming ramp to {args.target} C at {args.rate} C/min")
    prog.programme_ramp(row=1, rate=args.rate, limit=args.target, time_s=args.hold)
    prog.start()
    deadline = time.monotonic() + args.duration
    try:
        while time.monotonic() < deadline:
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        prog.stop()
        prog.close()
    print(f"\ncommands sent: {prog.commands}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="linkam.simulator")
    ap.add_argument("--target", type=float, default=95.0)
    ap.add_argument("--rate", type=float, default=10.0)
    ap.add_argument("--hold", type=float, default=0.0)
    ap.add_argument("--start", type=float, default=20.0)
    ap.add_argument("--duration", type=float, default=10.0)
    args = ap.parse_args(argv)
    return _demo(args)


if __name__ == "__main__":
    sys.exit(main())
