"""Compare poll reliability with and without draining the input queue.

Read-only.  The T95 leaves a backlog of reply blocks in the kernel
hidraw queue, and a fresh process inherits it -- so the first poll after
opening can be assembled out of stale blocks.  This measures the
difference.

usage: stress_read2.py <polls> <drain:0|1>
"""

from __future__ import annotations

import os
import select
import statistics
import sys
import time

from linkam import HidProgrammer
from linkam.hid import HidTransport


def drain(fd: int, wait: float = 0.2) -> int:
    """Discard everything already queued on the device."""
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
    polls = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    do_drain = bool(int(sys.argv[2])) if len(sys.argv) > 2 else False

    prog = HidProgrammer()
    prog.open()
    transport = prog._transport
    drained = 0
    if do_drain:
        drained = drain(transport._fd)
    print(f"mode={'drain' if do_drain else 'no-drain'}  drained={drained} blocks")

    ok = 0
    fails = []
    first_fail = None
    temps = []
    t0 = time.monotonic()
    try:
        for i in range(polls):
            try:
                st = prog.read_status(timeout=3.0)
            except Exception as exc:
                fails.append((i, str(exc)))
                if first_fail is None:
                    first_fail = i
                continue
            ok += 1
            if st.temperature_c is not None:
                temps.append(st.temperature_c)
            time.sleep(0.2)
    finally:
        prog.close()

    elapsed = time.monotonic() - t0
    print(f"polls={polls} ok={ok} ({ok/polls*100:.1f}%) failed={len(fails)} "
          f"first_fail_index={first_fail}")
    for i, f in fails[:6]:
        print(f"    #{i}: {f}")
    if temps:
        print(f"TC1 mean={statistics.fmean(temps):.4f} sd={statistics.pstdev(temps):.5f} "
              f"min={min(temps):.4f} max={max(temps):.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
