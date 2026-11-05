#!/bin/bash
# Reset a USB device by idVendor:idProduct.
#
# The Linux USB stack has no way to reset a device without root, so this
# uses the USBDEVFS_RESET ioctl, which needs CAP_ADMIN.
#
# Usage:
#   sudo ./tools/reset_usb_device.sh [16da:0002]
#
# This is useful when a HID device has stopped servicing its OUT
# endpoint (writes time out with ETIMEDOUT) but the device is still
# enumerated and its descriptors still read.

set -euo pipefail

WANT="${1:-16da:0002}"
VID="${WANT%%:*}"
PID="${WANT##*:}"

echo "Looking for ${VID}:${PID} ..."

# Locate the device by reading sysfs, so the bus/device numbers are
# always current -- they change on every re-enumeration.
found=""
for dir in /sys/bus/usb/devices/*/; do
    [ -f "$dir/idVendor" ] || continue
    v=$(cat "$dir/idVendor" 2>/dev/null || true)
    p=$(cat "$dir/idProduct" 2>/dev/null || true)
    if [ "${v,,}" = "${VID,,}" ] && [ "${p,,}" = "${PID,,}" ]; then
        busnum=$(cat "$dir/busnum")
        devnum=$(cat "$dir/devnum")
        found="/dev/bus/usb/$(printf '%03d' "$busnum")/$(printf '%03d' "$devnum")"
        echo "  found at $found"
        break
    fi
done

if [ -z "$found" ]; then
    echo "Device ${VID}:${PID} not found on USB." >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "This needs root (the USBDEVFS_RESET ioctl requires CAP_ADMIN)." >&2
    echo "Run: sudo $0 ${VID}:${PID}" >&2
    exit 1
fi

python3 - "$found" <<'PYEOF'
import array
import fcntl
import os
import sys

node = sys.argv[1]

def _ioc(direction, type_, nr, size):
    return (direction << 30) | (size << 16) | (type_ << 8) | nr

# USBDEVFS_RESET = _IOR('U', 20, unsigned int)
USBDEVFS_RESET = _ioc(2, ord("U"), 20, 4)

fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
try:
    fcntl.ioctl(fd, USBDEVFS_RESET, array.array("i", [0]), True)
    print("USB reset issued to", node)
except OSError as exc:
    print(f"reset failed: {exc}", file=sys.stderr)
    raise SystemExit(1)
finally:
    os.close(fd)
PYEOF

echo "Waiting for the device to re-enumerate..."
sleep 3

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

ls -l /dev/hidraw* 2>/dev/null || true
