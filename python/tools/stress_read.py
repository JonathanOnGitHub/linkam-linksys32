"""Poll the T95 repeatedly in one process and report reliability.

Read-only: every poll is eCMDT95_GETLINK, which the instrument cannot
act on.  Reports how many polls answered, the spread of both
thermocouple channels, and the index of the first failure.

The controller leaves a backlog of reply blocks in the kernel hidraw
queue, so a process that has just opened the device inherits stale
blocks.  ``--drain`` discards them first, which is how the two effects
were separated: draining does not change the failure rate, but it does
stop the first poll after opening from being assembled out of stale
blocks.

usage: stress_read.py [polls] [interval] [--drain]
"""

from __future__ import annotations

import argparse
import os
import select
import statistics
import sys
import time

from linkam import HidProgrammer


def drain(fd: int, wait: float = 0.2) -> int:
    """Discard whatever is already queued on the device."""
    n = 0
    deadline = time.monotonic() + wait
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        r, _, _ = select.select([fd], [], [], min(remaining, 0.05))
        if not r:
            continue
        try:
            data = os.read(fd, 64)
        except OSError:
            break
        if not data:
            break
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("polls", nargs="?", type=int, default=60)
    ap.add_argument("interval", nargs="?", type=float, default=0.5)
    ap.add_argument(
        "--drain",
        action="store_true",
        help="discard the blocks already queued on the device first",
    )
    args = ap.parse_args()

    prog = HidProgrammer()
    prog.open()
    drained = drain(prog._transport._fd) if args.drain else 0
    print(
        f"mode={'drain' if args.drain else 'no-drain'}  "
        f"drained={drained} block(s)"
    )

    ok = 0
    fails: list[tuple[int, str]] = []
    temps: list[float] = []
    sensors: list[float] = []
    t0 = time.monotonic()
    try:
        for i in range(args.polls):
            try:
                st = prog.read_status(timeout=3.0)
            except Exception as exc:
                fails.append((i, str(exc)))
                continue
            ok += 1
            if st.temperature_c is not None:
                temps.append(st.temperature_c)
            if st.sensor2_c is not None:
                sensors.append(st.sensor2_c)
            if i + 1 < args.polls:
                time.sleep(args.interval)
    finally:
        prog.close()

    elapsed = time.monotonic() - t0
    print(
        f"polls        : {args.polls} in {elapsed:.1f}s "
        f"({args.polls/elapsed:.1f}/s)"
    )
    print(f"answered     : {ok}  ({ok/args.polls*100:.1f}%)")
    print(f"failed       : {len(fails)}")
    for i, msg in fails[:10]:
        print(f"    #{i}: {msg}")
    if temps:
        print(
            f"TC1 (stage)  : n={len(temps)} mean={statistics.fmean(temps):.4f} "
            f"min={min(temps):.4f} max={max(temps):.4f} "
            f"sd={statistics.pstdev(temps):.5f} C"
        )
    if sensors:
        print(
            f"TC2          : n={len(sensors)} mean={statistics.fmean(sensors):.4f} "
            f"min={min(sensors):.4f} max={max(sensors):.4f} "
            f"sd={statistics.pstdev(sensors):.5f} C"
        )
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
