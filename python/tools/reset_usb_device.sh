#!/bin/bash
# Reset a USB device by idVendor:idProduct.
#
# This is useful when a HID device has stopped servicing its OUT
# endpoint (writes time out with ETIMEDOUT) but the device is still
# enumerated and its descriptors still read.
#
# Usage:
#   sudo ./tools/reset_usb_device.sh [16da:0002]
#
# How it works
# ------------
# Two mechanisms are tried, in order:
#
#   1. sysfs unbind/bind of the USB device.  This is the reliable one:
#      it unbinds the driver and re-enumerates the device, which is
#      equivalent to a hot-unplug/re-plug without touching the cable.
#
#   2. the USBDEVFS_RESET ioctl.  This one does NOT work on a device
#      whose interface is claimed by a kernel driver (hid-generic):
#      the kernel refuses it with ENOTTY ("Inappropriate ioctl for
#      device"), because it cannot reset a device that is in use.
#      It is kept only as a fallback, and its failure is not fatal.
#
# Note that a reset is not always enough: the Linkam T95's firmware
# sometimes stays wedged even across a re-enumeration, in which case
# the stage has to be power-cycled (off, wait 10 s, on).

set -euo pipefail

WANT="${1:-16da:0002}"
VID="${WANT%%:*}"
PID="${WANT##*:}"

echo "Looking for ${VID}:${PID} ..."

# Locate the device by reading sysfs, so the bus/device numbers are
# always current -- they change on every re-enumeration.
sysfs_dir=""
for dir in /sys/bus/usb/devices/*/; do
    [ -f "$dir/idVendor" ] || continue
    v=$(cat "$dir/idVendor" 2>/dev/null || true)
    p=$(cat "$dir/idProduct" 2>/dev/null || true)
    if [ "${v,,}" = "${VID,,}" ] && [ "${p,,}" = "${PID,,}" ]; then
        sysfs_dir="${dir%/}"
        break
    fi
done

if [ -z "$sysfs_dir" ]; then
    echo "Device ${VID}:${PID} not found on USB." >&2
    echo "It may have dropped off the bus entirely; unplug and replug it," >&2
    echo "and power-cycle the stage if it does not come back." >&2
    exit 1
fi

busnum=$(cat "$sysfs_dir/busnum")
devnum=$(cat "$sysfs_dir/devnum")
usb_node="/dev/bus/usb/$(printf '%03d' "$busnum")/$(printf '%03d' "$devnum")"
devname=$(basename "$sysfs_dir")
echo "  found at $usb_node (sysfs: $devname)"

if [ "$(id -u)" -ne 0 ]; then
    echo "This needs root (unbinding a USB device needs CAP_SYS_ADMIN)." >&2
    echo "Run: sudo $0 ${VID}:${PID}" >&2
    exit 1
fi

# --- 1. sysfs unbind/bind -------------------------------------------------- #
echo "Resetting by unbinding and rebinding the USB device ..."
if echo "$devname" > "/sys/bus/usb/drivers/usb/unbind" 2>/dev/null; then
    sleep 1
    if echo "$devname" > "/sys/bus/usb/drivers/usb/bind" 2>/dev/null; then
        echo "  unbind/bind done"
    else
        echo "  rebind failed; the device will re-enumerate on its own" >&2
    fi
else
    echo "  unbind failed, trying the USBDEVFS_RESET ioctl" >&2
fi

# --- 2. USBDEVFS_RESET ioctl (fallback) ----------------------------------- #
if [ ! -e "/sys/bus/usb/devices/$devname" ]; then
    python3 - "$usb_node" <<'PYEOF' || true
import array
import fcntl
import os
import sys

node = sys.argv[1]

def _ioc(direction, type_, nr, size):
    return (direction << 30) | (size << 16) | (type_ << 8) | nr

# USBDEVFS_RESET = _IOR('U', 20, unsigned int)
USBDEVFS_RESET = _ioc(2, ord("U"), 20, 4)

try:
    fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
except OSError as exc:
    print(f"  could not open {node}: {exc}", file=sys.stderr)
    raise SystemExit(0)
try:
    fcntl.ioctl(fd, USBDEVFS_RESET, array.array("i", [0]), True)
    print("  USB reset issued to", node)
except OSError as exc:
    # ENOTTY here just means the device is claimed by a driver.
    print(f"  USBDEVFS_RESET not permitted ({exc}); this is expected for a "
          f"driver-claimed device", file=sys.stderr)
finally:
    os.close(fd)
PYEOF
fi

echo "Waiting for the device to re-enumerate..."
for i in $(seq 1 20); do
    sleep 1
    for dir in /sys/bus/usb/devices/*/; do
        [ -f "$dir/idVendor" ] || continue
        v=$(cat "$dir/idVendor" 2>/dev/null || true)
        p=$(cat "$dir/idProduct" 2>/dev/null || true)
        if [ "${v,,}" = "${VID,,}" ] && [ "${p,,}" = "${PID,,}" ]; then
            busnum=$(cat "$dir/busnum")
            devnum=$(cat "$dir/devnum")
            echo "  back at /dev/bus/usb/$(printf '%03d' "$busnum")/$(printf '%03d' "$devnum")"
            break
        fi
    done
done

ls -l /dev/hidraw* 2>/dev/null || true
echo
echo "If the instrument still does not answer, power-cycle the stage:"
echo "  switch the controller off, wait 10-15 s, switch it on, replug the USB cable."
