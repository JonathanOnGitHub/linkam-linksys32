"""Poll the T95 repeatedly in one process and report reliability.

Read-only: every poll is eCMDT95_GETLINK, which the instrument cannot
act on.  Reports how many polls answered, and the spread of both
thermocouple channels.
"""

from __future__ import annotations

import statistics
import sys
import time

from linkam import HidProgrammer


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5

    prog = HidProgrammer()
    prog.open()
    ok = 0
    fails = []
    temps = []
    sensors = []
    t0 = time.monotonic()
    try:
        for i in range(count):
            try:
                st = prog.read_status(timeout=3.0)
            except Exception as exc:
                fails.append(f"#{i}: {exc}")
                continue
            ok += 1
            if st.temperature_c is not None:
                temps.append(st.temperature_c)
            if st.sensor2_c is not None:
                sensors.append(st.sensor2_c)
            if i + 1 < count:
                time.sleep(interval)
    finally:
        prog.close()

    elapsed = time.monotonic() - t0
    print(f"polls        : {count} in {elapsed:.1f}s ({count/elapsed:.1f}/s)")
    print(f"answered     : {ok}  ({ok/count*100:.1f}%)")
    print(f"failed       : {len(fails)}")
    for f in fails[:10]:
        print(f"    {f}")
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
