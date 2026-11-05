#!/usr/bin/env python3
"""Identify the Linkam programmer's USB-to-RS232 adapter.

Run this *after* plugging the adapter in and *before* trying to talk to
the instrument::

    python tools/find_adapter.py

It lists every candidate serial port and, for each, reports the
underlying USB device, the driver bound to it, and a judgement on
whether it is likely to be:

``rs232``    a genuine USB-to-RS232 adapter -- should work
``ttl``      a USB-to-TTL breakout -- will NOT drive the instrument
``unknown``  cannot tell; check the hardware

The TTL judgement matters more than it looks.  A Linkam programmer
speaks RS-232 (+/-5 V logic levels).  A TTL breakout has no line-level
transceiver, so its levels are wrong and the programmer will never
answer -- a failure that looks identical to a framing bug and can
cost hours of fruitless debugging.

This script is read-only: it enumerates ports and reads sysfs.  It
does not open, configure or write to any port.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# Allow running this script directly from the project directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Substrings in a USB product string that indicate a genuine RS-232
# transceiver on the DB9.
RS232_MARKERS = (
    "rs232", "rs-232", "rs485", "rs-485", "serial",
    "uart", "cp210", "pl2303", "prolific", "ftdi",
)

# Markers indicating a bare logic-level UART with no transceiver.
TTL_MARKERS = (
    "ttl", "3v3", "3.3v", "raspberry", "arduino",
    "esp32", "esp8266", "usb uart", "ch340", "ch341",
    # "USB-Serial-C Converter" is the product string used by the very
    # common CH340/PL2303 *TTL* breakouts.  It must not be read as
    # RS-232 just because it contains the word "serial".
    "usb-serial-c", "usb serial c", "serial-c converter",
    "usb-serial converter", "usb to ttl",
)


def classify(text: str) -> str:
    """Return 'rs232', 'ttl' or 'unknown' for a USB description string.

    The TTL test runs first and wins unless the string *explicitly*
    says RS-232/RS-485.  That ordering matters: many TTL breakouts
    advertise "USB-Serial", and treating those as RS-232 would point
    you at the wrong adapter with false confidence.
    """
    t = (text or "").lower()
    if not t:
        return "unknown"

    explicit_rs = any(m in t for m in ("rs232", "rs-232", "rs485", "rs-485"))
    if explicit_rs:
        return "rs232"

    if any(m in t for m in TTL_MARKERS):
        return "ttl"

    # "serial"/"uart" alone is suggestive but not conclusive.
    if any(m in t for m in RS232_MARKERS):
        return "rs232"

    return "unknown"


def sysfs_usb_path(device: str) -> Path | None:
    """Resolve /dev/ttyUSB0 to its USB device sysfs directory."""
    tty = Path("/sys/class/tty") / device.split("/")[-1]
    if not tty.exists():
        return None
    # Walk up to the USB device node (the parent that has an "idVendor").
    node = tty.resolve()
    for _ in range(8):
        if (node / "idVendor").exists():
            return node
        node = node.parent
    return None


def describe_usb(device: str) -> dict:
    """Collect vendor/product/driver facts for a serial device."""
    info = {"usb_path": None, "vendor_id": None, "product_id": None,
            "product": None, "manufacturer": None, "driver": None,
            "serial": None}
    path = sysfs_usb_path(device)
    if path is None:
        return info
    info["usb_path"] = str(path)

    def read(name):
        p = path / name
        try:
            return p.read_text().strip()
        except OSError:
            return None

    info["vendor_id"] = read("idVendor")
    info["product_id"] = read("idProduct")
    info["product"] = read("product")
    info["manufacturer"] = read("manufacturer")
    info["serial"] = read("serial")

    # The driver is on the interface (the tty's own device link).
    tty = Path("/sys/class/tty") / device.split("/")[-1]
    try:
        link = (tty / "device/driver").resolve()
        info["driver"] = link.name
    except OSError:
        pass
    return info


def usb_id_text(info: dict) -> str:
    """All the human-readable text we can scrape about a USB device."""
    parts = [
        info.get("product") or "",
        info.get("manufacturer") or "",
        info.get("driver") or "",
    ]
    if info.get("vendor_id") and info.get("product_id"):
        parts.append(f"{info['vendor_id']}:{info['product_id']}")
    return " ".join(p for p in parts if p)


def report_hid() -> int | None:
    """Report the Linkam T95's USB HID link, if present.

    Returns an exit code if a T95 was found (0), or ``None`` if there is
    no HID device so that the serial-port logic can take over.
    """
    try:
        from linkam.hid import find_link
    except ImportError:
        return None

    link = find_link()
    if link is None:
        return None

    print("\nUSB HID device (Linkam T95)")
    print(f"  hidraw node  : {link.node}")
    print(f"  product      : {link.name or 'Linkam T95'}")
    if link.uniq:
        print(f"  serial no.   : {link.uniq}")
    if link.vendor is not None and link.product is not None:
        print(f"  usb id       : {link.vendor:04x}:{link.product:04x}")

    try:
        fd = os.open(link.node, os.O_RDWR | os.O_CLOEXEC)
    except OSError as exc:
        print(f"  verdict      : [!!] cannot open ({exc.strerror or exc})")
        print("                  Install a udev rule so the node is")
        print("                  group-writable, e.g.:")
        print('                    SUBSYSTEM=="hidraw", ATTRS{idVendor}=="16da",')
        print('                    ATTRS{idProduct}=="0002", MODE="0660",')
        print('                    GROUP="plugdev", TAG+="uaccess"')
        return 1
    else:
        os.close(fd)

    print("  verdict      : [OK] T95 detected and accessible")
    print()
    print("  This is a USB HID device, NOT a serial port. It needs no")
    print("  USB-to-RS232 adapter. Use --hid instead of --port:")
    print("    .venv/bin/python -m linkam.cli probe --hid")
    print("    .venv/bin/python -m linkam.cli run --hid \\")
    print("        --target 40 --rate 5 --hold 60 --no-video")
    return 0


def main() -> int:
    # The T95 is a HID device; check for it first, since it needs no
    # serial adapter and the serial-port diagnostics do not apply.
    hid_result = report_hid()
    if hid_result is not None:
        if hid_result == 0:
            print()
            print("=" * 72)
            print("LINKAM T95 FOUND — use the --hid transport.")
            print("=" * 72)
        return hid_result

    try:
        from serial.tools import list_ports
    except ImportError:
        print("pyserial is not installed in this interpreter.", file=sys.stderr)
        print("Use the project venv: .venv/bin/python tools/find_adapter.py",
              file=sys.stderr)
        return 2

    ports = list(list_ports.comports())
    print("=" * 72)
    print("Linkam adapter finder")
    print("=" * 72)

    if not ports:
        print("\nNo serial ports found, and no Linkam T95 on USB.\n")
        print("Which instrument do you have?")
        print()
        print("  * Linkam T95  -- a USB HID device. It needs NO serial")
        print("    adapter, just the USB cable. Check:")
        print("        lsusb | grep 16da")
        print("    If that shows nothing, the stage is not presenting on the")
        print("    USB bus: try a different cable (charge-only cables have no")
        print("    data lines), a different port, and make sure the stage's")
        print("    own power supply is on.")
        print()
        print("  * Older MSVP/TMS -- needs a genuine USB-to-RS232 adapter.")
        print("    Check:")
        print("        lsusb | grep -i -E 'cp210|ftdi|ch34|pl2303|prolific'")
        print("    If the device appears in lsusb but not here, the driver is")
        print("    not bound. On Debian/Ubuntu:")
        print("      sudo modprobe cp210x   # CP210x")
        print("      sudo modprobe ftdi_sio # FTDI")
        print("      sudo modprobe ch341    # CH341")
        print("      sudo modprobe usbserial # generic")
        print("    If it is a bare USB-to-TTL breakout, it will NOT work with")
        print("    a Linkam programmer regardless of drivers.")
        return 1

    candidates = []
    for p in ports:
        device = p.device
        info = describe_usb(device)
        desc = p.description or ""
        extra = " ".join(x for x in (p.manufacturer, p.product) if x)
        text = " ".join(x for x in (desc, extra, usb_id_text(info)) if x)
        kind = classify(text)

        # Ignore the laptop's own legacy UARTs unless they look like USB.
        is_usb = info["usb_path"] is not None

        print(f"\n{device}")
        print(f"  description   : {desc}")
        if extra:
            print(f"  usb strings   : {extra}")
        if info["vendor_id"]:
            print(f"  usb id        : {info['vendor_id']}:{info['product_id']}")
        if info["driver"]:
            print(f"  driver        : {info['driver']}")
        if info["serial"]:
            print(f"  serial no.    : {info['serial']}")
        if info["usb_path"]:
            print(f"  sysfs         : {info['usb_path']}")

        if not is_usb:
            print("  verdict       : [ ? ] not a USB device "
                  "(legacy UART on the motherboard -- not the instrument)")
            continue

        if kind == "rs232":
            print("  verdict       : [OK] looks like a USB-to-RS232 adapter")
            candidates.append((device, kind, desc))
        elif kind == "ttl":
            print("  verdict       : [!!] USB-to-TTL breakout -- WILL NOT WORK")
            print("                  The programmer needs +/-5 V RS-232 levels.")
            print("                  A TTL port has no transceiver, so the")
            print("                  instrument will simply never answer.")
            print("                  Buy a USB-to-RS232 adapter instead.")
        else:
            print("  verdict       : [ ? ] cannot tell RS-232 vs TTL")
            print("                  Check the adapter's label: it must say")
            print("                  'RS232' (not 'TTL' / '3.3V').")
            candidates.append((device, kind, desc))

    print()
    print("=" * 72)
    usable = [d for d, k, _ in candidates if k == "rs232"]
    unknown = [d for d, k, _ in candidates if k == "unknown"]

    if usable:
        print(f"LIKELY CORRECT: {', '.join(usable)}")
        print()
        print("Next: settle the framing with a read-only probe.")
        print(f"  .venv/bin/python -m linkam.cli probe --port {usable[0]}")
        print()
        print("Then, temperature only (no video), with a safe limit:")
        print(f"  .venv/bin/python -m linkam.cli run --port {usable[0]} "
              "--baud 9600 --target 40 --rate 5 --hold 60 --no-video \\")
        print(f"      --out results/first_test")
        return 0

    if unknown:
        print(f"POSSIBLE CANDIDATES (unverified): {', '.join(unknown)}")
        print()
        print("No port self-identifies as RS-232. That may simply mean the")
        print("adapter has a bland product string. If you know it has an")
        print("RS-232 transceiver, probe it:")
        for d in unknown:
            print(f"  .venv/bin/python -m linkam.cli probe --port {d}")
        print()
        print("If probe reports NO RESPONSE on every port, the most likely")
        print("cause is a TTL adapter or wrong DB9 gender/crossover.")
        return 1

    print("NO USB SERIAL ADAPTER FOUND.")
    print("The only ports present are the laptop's legacy UARTs.")
    print("Plug in the USB-to-RS232 adapter and run this again.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
