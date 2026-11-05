"""Command-line entry point.

Examples
--------
Check whether this machine is ready to talk to the instrument::

    python -m linkam.cli doctor

List serial ports and probe the framing::

    python -m linkam.cli ports
    python -m linkam.cli probe --port /dev/ttyUSB0 --baud 9600

The Linkam T95 is a USB HID device rather than a serial port, so it uses
``--hid`` instead of ``--port``::

    python -m linkam.cli probe --hid
    python -m linkam.cli run --hid --target 40 --rate 5 --no-video

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
from .protocol import ConnectionError_, HidProgrammer, LinkamProgrammer


def _make_programmer(args):
    """Build the programmer object for the selected transport."""
    if getattr(args, "hid", False):
        return HidProgrammer(
            args.hid_node or None,
            framing=args.hid_framing,
            report_size=args.hid_report_size,
            timeout=args.timeout,
        )
    return LinkamProgrammer(args.port, args.baud, strict_framing=False)


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
    prog = _make_programmer(args)
    bauds = None
    if not getattr(args, "hid", False):
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
        if "framing" in w:
            print(f"  framing    : {w['framing']}")
        else:
            print(f"  baudrate   : {w['baudrate']}")
            print(f"  terminator : {w['terminator']!r}")
        print(f"  command    : {w['command']!r}")
        print(f"  response   : {result['response']!r}")
        print()
        if "framing" in w:
            print("Use this in your run:")
            print(f"  --hid-framing {w['framing']}")
        else:
            print("Use these in your run:")
            print(f"  --baud {w['baudrate']}"
                  + (" --strict-framing" if w["terminator"] == "cr" else ""))
    else:
        print("NO RESPONSE from the instrument.")
        print(f"tried {len(result['tried'])} combinations: "
              f"{', '.join(result['tried'][:8])} ...")
        print()
        if getattr(args, "hid", False):
            print("Checklist:")
            print("  - is the T95 powered on and plugged in?")
            print("  - lsusb | grep 16da   (should show 'Linkam T95')")
            print("  - is /dev/hidraw* readable?")
            print("    (the udev rule should give it group plugdev)")
        else:
            print("Checklist:")
            print("  - is the programmer powered on?")
            print("  - is the RS232 cable the right gender / crossover?")
            print("  - is the adapter a true RS-232 adapter (not TTL)?")
            print("  - try the other cable / a null-modem crossover")
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("winner") else 1


def _read(args) -> int:
    """Read the instrument's temperature.  Sends no commands.

    This is deliberately the smallest possible interaction with the
    instrument: it polls the T95's status and prints the temperature.
    Nothing here can start a ramp or change the setpoint.
    """
    import time as _time

    prog = _make_programmer(args)
    readings = []
    try:
        prog.open()
        count = max(1, args.count)
        for i in range(count):
            try:
                if getattr(args, "hid", False):
                    # The T95 can answer a poll before its first sensor
                    # sample, so use the retrying read.
                    status = prog.read_status(timeout=args.timeout)
                else:
                    status = prog.poll_status(timeout=args.timeout)
                temp = status.temperature_c
            except Exception as exc:
                print(f"read failed: {exc}", file=sys.stderr)
                return 1
            reading = {
                "t": _time.time(),
                "temperature_c": temp,
                "sensor2_c": status.sensor2_c,
                "rate": status.rate,
                "limit": status.limit,
                "block_types": status.block_types,
            }
            readings.append(reading)
            if not args.json:
                shown = f"{temp:.4f} C" if temp is not None else "no temperature reported"
                extra = []
                if status.rate is not None:
                    extra.append(f"rate={status.rate:g}")
                if status.limit is not None:
                    extra.append(f"limit={status.limit:g}")
                suffix = ("  " + "  ".join(extra)) if extra else ""
                print(f"  {shown}{suffix}")
            if i + 1 < count:
                _time.sleep(args.interval)
    finally:
        try:
            prog.close()
        except Exception:
            pass

    if args.json:
        print(json.dumps(readings, indent=2))
    return 0


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
    from .protocol import ProgrammerNotRamping

    exp = Experiment(
        target_c=args.target,
        rate_c_per_min=args.rate,
        hold_s=args.hold,
        output_dir=args.out,
        serial_port=args.port or "/dev/ttyUSB0",
        baudrate=args.baud,
        use_hid=bool(args.hid),
        hid_node=args.hid_node,
        hid_framing=args.hid_framing,
        hid_report_size=args.hid_report_size,
        video_backend=args.video_backend,
        video_device=args.video_device,
        video_width=args.width,
        video_height=args.height,
        video_fps=args.fps,
        save_video=not args.no_video,
        save_frames=args.save_frames,
        strict_framing=args.strict_framing,
        stall_timeout=(args.stall_timeout if args.stall_timeout > 0 else None),
        min_progress_c=args.min_progress,
    )
    run = IsothermalRun(exp)
    try:
        result = run.start()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except ProgrammerNotRamping as exc:
        print("RAMP REFUSED — the programmer is not moving.", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        return 3
    except ConnectionError_ as exc:
        print(f"CANNOT REACH THE INSTRUMENT: {exc}", file=sys.stderr)
        if args.hid:
            print("  The T95 is a USB HID device. Check:", file=sys.stderr)
            print("    lsusb | grep 16da", file=sys.stderr)
            print("    ls -l /dev/hidraw*", file=sys.stderr)
        else:
            print("  Check that the instrument is powered on and that the "
                  "serial port is correct:", file=sys.stderr)
            print("    .venv/bin/python tools/find_adapter.py", file=sys.stderr)
        return 4
    except (OSError, ValueError) as exc:
        print(f"FAILED TO START: {exc}", file=sys.stderr)
        return 4
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
    p.add_argument("--port", help="serial port (legacy RS-232 link)")
    p.add_argument("--hid", action="store_true",
                   help="probe the USB HID link (Linkam T95)")
    p.add_argument("--hid-node", default=None,
                   help="explicit hidraw node, e.g. /dev/hidraw1")
    p.add_argument("--hid-framing", default="text_padded",
                   choices=["raw", "id_text", "text_padded", "length_prefixed"],
                   help="HID report framing to use")
    p.add_argument("--hid-report-size", type=int, default=None,
                   help="output report size in bytes (default: discover)")
    p.add_argument("--baud", type=int, default=0,
                   help="try this baud first (0 = sweep all candidates)")
    p.add_argument("--timeout", type=float, default=1.0,
                   help="per-attempt response timeout in seconds")

    rd = sub.add_parser(
        "read",
        help="read the instrument's temperature (read-only, no commands sent)",
    )
    rd.add_argument("--port", help="serial port (legacy RS-232 link)")
    rd.add_argument("--hid", action="store_true",
                    help="use the USB HID link (Linkam T95)")
    rd.add_argument("--hid-node", default=None,
                    help="explicit hidraw node, e.g. /dev/hidraw1")
    rd.add_argument("--hid-framing", default="text_padded",
                    choices=["raw", "id_text", "text_padded", "length_prefixed"],
                    help="HID report framing to use")
    rd.add_argument("--hid-report-size", type=int, default=None,
                    help="output report size in bytes (default: 63, from the "
                         "report descriptor)")
    rd.add_argument("--baud", type=int, default=9600)
    rd.add_argument("--count", type=int, default=0,
                    help="number of readings to take (0 = one)")
    rd.add_argument("--interval", type=float, default=1.0,
                    help="seconds between readings")
    rd.add_argument("--timeout", type=float, default=2.0,
                    help="seconds to wait for each answer")
    rd.add_argument("--json", action="store_true",
                    help="emit the readings as JSON")

    r = sub.add_parser("run", help="run an isothermal experiment with capture")
    r.add_argument("--port", help="serial port (legacy RS-232 link)")
    r.add_argument("--hid", action="store_true",
                   help="use the USB HID link (Linkam T95)")
    r.add_argument("--hid-node", default=None,
                   help="explicit hidraw node, e.g. /dev/hidraw1")
    r.add_argument("--hid-framing", default="text_padded",
                   choices=["raw", "id_text", "text_padded", "length_prefixed"],
                   help="HID report framing to use")
    r.add_argument("--hid-report-size", type=int, default=None,
                   help="output report size in bytes (default: discover)")
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
    r.add_argument("--stall-timeout", type=float, default=120.0,
                   help="seconds without progress before declaring the ramp "
                        "refused (use 0 to wait indefinitely)")
    r.add_argument("--min-progress", type=float, default=0.2,
                   help="temperature change (C) that counts as progress "
                        "toward the target")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.cmd == "ports":
        return _ports()
    if args.cmd == "doctor":
        return _doctor(args)
    if args.cmd in ("probe", "run", "read"):
        # Exactly one transport must be selected.
        if args.hid and args.port:
            print("choose either --hid or --port, not both", file=sys.stderr)
            return 2
        if not args.hid and not args.port:
            print("one of --hid or --port is required", file=sys.stderr)
            return 2
    if args.cmd == "probe":
        return _probe(args)
    if args.cmd == "read":
        return _read(args)
    if args.cmd == "run":
        return _run(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
