"""Command-line entry point.

Examples
--------
Check whether this machine is ready to talk to the instrument::

    python -m linkam.cli doctor

List serial ports and probe the framing::

    python -m linkam.cli ports
    python -m linkam.cli probe --port /dev/ttyUSB0 --baud 9600

Run an isothermal experiment with simultaneous capture::

    python -m linkam.cli run \\
        --port /dev/ttyUSB0 --baud 9600 \\
        --target 95 --rate 2 --hold 600 \\
        --video-backend dshow --video-device 0 \\
        --out results/exp1
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .experiment import Experiment, IsothermalRun
from .protocol import LinkamProgrammer


def _ports() -> int:
    try:
        from serial.tools import list_ports
    except ImportError:
        print("pyserial is required: pip install pyserial", file=sys.stderr)
        return 2
    found = False
    for p in list_ports.comports():
        found = True
        print(f"{p.device:24s} {p.description}")
        if p.manufacturer or p.product:
            print(f"{'':24s}   {p.manufacturer} / {p.product}")
    if not found:
        print("no serial ports found")
    return 0


def _probe(args) -> int:
    prog = LinkamProgrammer(args.port, args.baud, strict_framing=False)
    bauds = None
    if args.baud:
        bauds = [args.baud, *LinkamProgrammer.CANDIDATE_BAUDS]
        bauds = list(dict.fromkeys(bauds))
    try:
        result = prog.probe(bauds=bauds, timeout=args.timeout)
    except Exception as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            prog.close()
        except Exception:
            pass

    if result.get("winner"):
        w = result["winner"]
        print("LINK ESTABLISHED")
        print(f"  baudrate   : {w['baudrate']}")
        print(f"  terminator : {w['terminator']!r}")
        print(f"  command    : {w['command']!r}")
        print(f"  response   : {result['response']!r}")
        print()
        print("Use these in your run:")
        print(f"  --baud {w['baudrate']}"
              + (" --strict-framing" if w["terminator"] == "cr" else ""))
    else:
        print("NO RESPONSE from the instrument.")
        print(f"tried {len(result['tried'])} combinations: "
              f"{', '.join(result['tried'][:8])} ...")
        print()
        print("Checklist:")
        print("  - is the programmer powered on?")
        print("  - is the RS232 cable the right gender / crossover?")
        print("  - is the adapter a true RS-232 adapter (not TTL)?")
        print("  - try the other cable / a null-modem crossover")
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("winner") else 1


def _doctor(args) -> int:
    from .diagnostics import format_report, run_checks

    report = run_checks()
    print(format_report(report))
    if args.json:
        import json as _json

        print()
        print(_json.dumps(
            {
                "ok": report.ok,
                "worst": report.worst,
                "counts": report.counts(),
                "notes": report.notes,
                "checks": [
                    {
                        "name": c.name,
                        "status": c.status,
                        "detail": c.detail,
                        "hint": c.hint,
                    }
                    for c in report.checks
                ],
            },
            indent=2,
        ))
    return 0 if report.ok else 1


def _run(args) -> int:
    exp = Experiment(
        target_c=args.target,
        rate_c_per_min=args.rate,
        hold_s=args.hold,
        output_dir=args.out,
        serial_port=args.port,
        baudrate=args.baud,
        video_backend=args.video_backend,
        video_device=args.video_device,
        video_width=args.width,
        video_height=args.height,
        video_fps=args.fps,
        save_video=not args.no_video,
        save_frames=args.save_frames,
        strict_framing=args.strict_framing,
    )
    run = IsothermalRun(exp)
    try:
        result = run.start()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    print(result.summary())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="linkam", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ports", help="list serial ports")

    d = sub.add_parser(
        "doctor",
        help="check whether this machine is ready to talk to the instrument",
    )
    d.add_argument("--json", action="store_true",
                   help="also emit the report as JSON")

    p = sub.add_parser("probe", help="probe the framing on real hardware")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=0,
                   help="try this baud first (0 = sweep all candidates)")
    p.add_argument("--timeout", type=float, default=1.0,
                   help="per-attempt response timeout in seconds")

    r = sub.add_parser("run", help="run an isothermal experiment with capture")
    r.add_argument("--port", required=True)
    r.add_argument("--baud", type=int, default=9600)
    r.add_argument("--target", type=float, required=True, help="target °C")
    r.add_argument("--rate", type=float, default=10.0, help="°C/min")
    r.add_argument("--hold", type=float, default=0.0, help="hold seconds")
    r.add_argument("--out", default="linkam_run")
    r.add_argument("--video-backend", default="auto",
                   choices=["auto", "dshow", "v4l2"])
    r.add_argument("--video-device", default=0)
    r.add_argument("--width", type=int, default=640)
    r.add_argument("--height", type=int, default=480)
    r.add_argument("--fps", type=float, default=25.0)
    r.add_argument("--no-video", action="store_true")
    r.add_argument("--save-frames", action="store_true")
    r.add_argument("--strict-framing", action="store_true",
                   help="use CR instead of CRLF as the command terminator")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.cmd == "ports":
        return _ports()
    if args.cmd == "doctor":
        return _doctor(args)
    if args.cmd == "probe":
        return _probe(args)
    if args.cmd == "run":
        return _run(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
