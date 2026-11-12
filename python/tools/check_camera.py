#!/usr/bin/env python3
"""Check that the microscope's camera can be seen.

Prints what the Aravis stack sees, how fast the USB link is, and --
when the camera is not there -- what to do about it.

    ./venv/bin/python tools/check_camera.py

It finds Aravis the same way the web GUI does, so if it works here the
GUI will work too.  Nothing is moved and nothing is captured.
"""

from __future__ import annotations

import os
import sys

PREFIX = os.environ.get("ARAVIS_PREFIX", "/tmp/opencode/aravis-root")


def _bootstrap() -> None:
    """Point the loader at a local Aravis and start again.

    The dynamic linker reads ``LD_LIBRARY_PATH`` once, when the process
    starts, so a local Aravis cannot be found by a running process.
    Starting again with the right environment is the only way in.
    """
    if os.environ.get("_ARAVIS_BOOTSTRAPPED"):
        return
    libdir = os.path.join(PREFIX, "usr", "lib", "x86_64-linux-gnu")
    typelib = os.path.join(libdir, "girepository-1.0")
    if not os.path.isdir(typelib):
        return
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = libdir + ":" + env.get("LD_LIBRARY_PATH", "")
    env["GI_TYPELIB_PATH"] = typelib + ":" + env.get("GI_TYPELIB_PATH", "")
    env["_ARAVIS_BOOTSTRAPPED"] = "1"
    try:
        os.execve(sys.executable, [sys.executable] + sys.argv, env)
    except OSError as exc:
        print(f"could not start again with Aravis: {exc}")


def usb_link() -> dict | None:
    """The USB link of any attached Allied Vision camera."""
    import glob

    for path in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            with open(path) as fh:
                if fh.read().strip().lower() != "1ab2":
                    continue
        except OSError:
            continue
        base = os.path.dirname(path)

        def read(name):
            try:
                with open(os.path.join(base, name)) as fh:
                    return fh.read().strip()
            except OSError:
                return None

        return {
            "product": read("product"),
            "manufacturer": read("manufacturer"),
            "speed": read("speed"),
            "version": read("version"),
        }
    return None


def udev_rule() -> bool:
    """Whether the udev rule that opens the camera is installed."""
    import glob

    for path in glob.glob("/etc/udev/rules.d/*"):
        try:
            with open(path) as fh:
                if "1ab2" in fh.read():
                    return True
        except OSError:
            continue
    return False


def main() -> int:
    _bootstrap()

    print("Aravis")
    try:
        import gi

        gi.require_version("Aravis", "0.8")
        from gi.repository import Aravis
    except (ImportError, ValueError) as exc:
        print(f"  the Aravis bindings are not available: {exc}")
        print("  install them with:")
        print("    sudo apt install libaravis-0.8-0 gir1.2-aravis-0.8")
        print("  or unpack them into a local prefix and point ARAVIS_PREFIX at it:")
        print("    mkdir -p /tmp/aravis-debs && cd /tmp/aravis-debs")
        print("    apt-get download libaravis-0.8-0 gir1.2-aravis-0.8")
        print("    for f in *.deb; do dpkg-deb -x \"$f\" /tmp/aravis-root; done")
        print("    export ARAVIS_PREFIX=/tmp/aravis-root")
        return 1

    print("  the bindings are here")
    Aravis.update_device_list()
    count = Aravis.get_n_devices()
    print(f"  {count} camera(s) seen")
    for i in range(count):
        print(f"    [{i}] {Aravis.get_device_vendor(i)} - {Aravis.get_device_model(i)}")
        print(f"        serial   : {Aravis.get_device_serial_nbr(i)}")
        print(f"        protocol : {Aravis.get_device_protocol(i)}")

    print("\nUSB link")
    link = usb_link()
    if link is None:
        print("  no Allied Vision camera is on the bus")
    else:
        speed = link.get("speed")
        print(f"  {link.get('product')} - {speed} Mbps (USB {link.get('version')})")
        if speed and int(speed) < 5000:
            print("  ! USB 2.x link: the camera needs USB 3.x SuperSpeed.")
            print("    Over USB 2.0 the control interface works while the")
            print("    stream interface does not, so the camera looks alive")
            print("    and captures nothing.")

    print("\nPermissions")
    if udev_rule():
        print("  the udev rule is installed")
    else:
        print("  ! no udev rule for the camera: a non-root process cannot")
        print("    open it.  Install one:")
        print("      sudo install -m 0644 99-allied-vision.rules /etc/udev/rules.d/")
        print("      sudo udevadm control --reload-rules")

    if count == 0:
        print("\nThe camera is not seen.  Check the cable, and the port: it")
        print("must be a USB 3.x SuperSpeed port, and the T95 must be on a")
        print("USB 2.0-only hub so the two do not starve each other.")
        return 1
    print("\nThe camera is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
