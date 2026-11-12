"""Capture the T95's raw input-report stream, block by block.

Read-only.  Opens the device, notes whether the kernel hidraw queue
already holds anything (stale blocks left by a previous process), then
sends a series of status polls and logs every report that arrives with
its index byte and a timestamp.

This is how the "block 1 goes missing" failure mode gets pinned down.
"""

from __future__ import annotations

import os
import select
import sys
import time

from linkam.hid import HidTransport, find_link
from linkam.t95_protocol import build_status_poll


def drain(fd: float, label: str, wait: float = 0.25) -> list[bytes]:
    """Read whatever is already queued on the device."""
    out = []
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
        out.append(data)
    if out:
        print(f"  [stale at open, {label}] {len(out)} report(s): "
              f"indices={[b[0] for b in out]}")
    else:
        print(f"  [stale at open, {label}] queue empty")
    return out


def main() -> int:
    polls = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    link = find_link()
    if link is None:
        print("T95 not found")
        return 1

    transport = HidTransport(link.node)
    transport.open()
    fd = transport._fd
    print(f"device: {link.node}")
    drain(fd, "before first poll")

    try:
        for p in range(polls):
            transport.write_blocks(build_status_poll())
            t_send = time.monotonic()
            got = []
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and len(got) < 6:
                r, _, _ = select.select([fd], [], [], 0.05)
                if not r:
                    continue
                data = os.read(fd, 64)
                if not data:
                    break
                got.append((round(time.monotonic() - t_send, 4), data[0], len(data)))
            desc = " ".join(f"{t:.4f}s:idx{b}" for t, b, _ in got)
            print(f"  poll {p:>2}: {len(got)} report(s)  {desc}")
            time.sleep(0.3)
    finally:
        transport.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
