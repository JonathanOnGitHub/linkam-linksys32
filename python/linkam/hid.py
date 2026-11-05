"""USB HID transport for the Linkam T95 temperature programmer.

The T95 is a **USB HID** device, not a serial port.  It enumerates as
``16da:0002`` and appears as a ``/dev/hidraw*`` node (the kernel binds
``hid-generic`` to it).  This is why the legacy Linksys32 code, which is
built around pyserial, cannot talk to it at all.

What is now known
-----------------

* USB vendor/product: ``16da:0002`` ("Linkam T95").
* Two interrupt endpoints; 64-byte IN reports; ``bInterval`` 1.
* The device is **silent until prompted** -- a 30 s passive listen produced
  no unsolicited input frames at all.
* hidraw output reports accept lengths 2, 4, 8, 16, 32 and 64.
  A **1-byte** output report is rejected with ``EINVAL``.
* ``HIDIOCGRAWNAME`` -> ``Linkam T95``; ``HIDIOCGRAWUNIQ`` -> ``1234``;
  ``HIDIOCGRAWINFO`` -> vendor ``0x16da``, product ``0x0002``.

The HID report descriptor
-------------------------

Retrieved from the device with ``HIDIOCGRDESC`` (29 bytes)::

    06 a0 ff   Usage Page (0xFFA0)         -- Linkam vendor page
    09 01      Usage (0x01)
    a1 01      Collection (Application)
    09 01      Usage (0x01)
    15 00      Logical Minimum (0)
    26 ff 00   Logical Maximum (255)
    75 08      Report Size (8)
    95 3f      Report Count (63)             -- 63 bytes
    81 82      Input  (Data, Var, Abs)
    09 02      Usage (0x02)
    75 08      Report Size (8)
    95 3f      Report Count (63)             -- 63 bytes
    91 82      Output (Data, Var, Abs)
    c0         End Collection

So the T95 has **no report IDs** and a **63-byte payload** in each
direction: one vendor-defined INPUT report and one vendor-defined
OUTPUT report.  The application-layer ASCII is carried in those bytes,
padded out to 63.

Design
------

The Linkam application layer is line-oriented ASCII (see
``../PROTOCOL_NOTES.md``), so this module's job is to unwrap ASCII from
HID reports and rewrap ASCII into HID reports.  It exposes the
:class:`linkam.transport.Transport` interface, which means every layer
above -- telemetry parsing, ramp sequencing, stall detection -- is shared
with the serial link and needs no changes.

The candidate framings are:

``raw``
    The report is the ASCII payload, starting at byte 0, with the report
    ID (if any) at byte 0.  Output reports are the command bytes padded
    with zeros to the device's report size.

``id_text``
    Byte 0 is the report ID; the ASCII text starts at byte 1.

``text_padded``
    The ASCII text starts at byte 0 of a report that has **no** report ID
    (a device with a single output report); trailing bytes are padding.

``length_prefixed``
    The report contains a length byte (or 16-bit length) followed by that
    many ASCII bytes.  Common in firmware that packs variable-length
    strings into fixed reports.

The correct framing can be pinned down once the hardware is attached by
running the discovery helper, after which it should be hard-coded.

Safety
------

Nothing in this module starts a ramp.  Commands are only sent when the
caller explicitly asks for them.
"""

from __future__ import annotations

import errno
import fcntl
import glob
import logging
import os
import re
import struct
import time
from typing import Iterable, Sequence

from .transport import CommandTooLong, TransportTimeout

log = logging.getLogger(__name__)

__all__ = [
    "HidTransport",
    "HidLink",
    "find_link",
    "FRAMINGS",
    "T95_VID",
    "T95_PID",
]

#: USB vendor/product IDs of the Linkam T95.
T95_VID = 0x16DA
T95_PID = 0x0002

#: Vendor-specific HID usage page used by Linkam's newer HID products.
LINKAM_USAGE_PAGE = 0xFF00

#: Candidate payload framings, in the order they are tried by default.
FRAMINGS = ("raw", "id_text", "text_padded", "length_prefixed")


# --------------------------------------------------------------------------- #
# ioctl helpers
# --------------------------------------------------------------------------- #

# The Linux ioctl request-number layout, from
# ``<asm-generic/ioctl.h>``::
#
#     _IOC(dir,type,nr,size) = (dir << 30) | (size << 16) | (type << 8) | nr
#
# so: nr occupies bits 0-7, type bits 8-15, size bits 16-29 and the
# direction bits 30-31.
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_DIRBITS = 2

_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS          # 8
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS      # 16
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS       # 30

_IOC_NONE = 0
_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, type_: int, nr: int, size: int) -> int:
    """Build an ioctl request number (Linux ``<asm-generic/ioctl.h>``)."""
    return (
        (direction << _IOC_DIRSHIFT)
        | (type_ << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


#: ``HID_MAX_DESCRIPTOR_SIZE`` from ``<linux/hid.h>``.
HID_MAX_DESCRIPTOR_SIZE = 4096

_HIDIOCGRDESCSIZE = _ioc(_IOC_READ, ord("H"), 0x01, 4)

#: ``HIDIOCGRDESC``.
#:
#: The size field must be ``sizeof(struct hidraw_report_descriptor)``,
#: which is ``4 + HID_MAX_DESCRIPTOR_SIZE`` = 4100 bytes -- the ``__u32
#: size`` member plus the ``__u8 value[4096]`` array.  The kernel's
#: ``hidraw_report_descriptor`` handler copies the descriptor into that
#: array and updates the leading ``size``.
#:
#: This ioctl is what makes the T95's report layout discoverable, so it
#: is worth getting exactly right: the request number must encode the
#: size as 4100 with the shifts from ``<asm-generic/ioctl.h>``, i.e.
#: ``(dir << 30) | (size << 16) | (type << 8) | nr``.
_HIDIOCGRDESC = _ioc(_IOC_READ, ord("H"), 0x02, 4 + HID_MAX_DESCRIPTOR_SIZE)

_HIDIOCGRAWINFO = _ioc(_IOC_READ, ord("H"), 0x03, 8)
_HIDIOCGRAWUNIQ = _ioc(_IOC_READ, ord("H"), 0x08, 256)


# --------------------------------------------------------------------------- #
# Device discovery
# --------------------------------------------------------------------------- #

#: ``HID_ID`` in a hidraw uevent looks like ``0003:000016DA:00000002``:
#: bus type, vendor, product.
_HID_ID_RE = re.compile(r"^([0-9A-Fa-f]+):([0-9A-Fa-f]+):([0-9A-Fa-f]+)$")


class HidLink:
    """A located HID device: its hidraw node and identifying strings."""

    def __init__(
        self,
        node: str,
        *,
        vendor: int | None = None,
        product: int | None = None,
        name: str = "",
        uniq: str = "",
    ) -> None:
        self.node = node
        self.vendor = vendor
        self.product = product
        self.name = name
        self.uniq = uniq

    @property
    def is_t95(self) -> bool:
        return self.vendor == T95_VID and self.product == T95_PID

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        vid = "----" if self.vendor is None else f"{self.vendor:04x}"
        pid = "----" if self.product is None else f"{self.product:04x}"
        return f"<HidLink {self.node} {vid}:{pid} {self.name!r}>"


def _read_uevent_strings(node: str) -> dict[str, str]:
    """Parse the ``device/uevent`` file that belongs to a hidraw node."""
    out: dict[str, str] = {}
    candidates = [
        os.path.join("/sys/class/hidraw", os.path.basename(node), "device", "uevent"),
        os.path.join("/sys/class/hidraw", os.path.basename(node), "uevent"),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for line in text.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                out.setdefault(key.strip(), value.strip())
        break
    return out


def _parse_hex(value: str) -> int:
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return -1


def _hidraw_nodes() -> list[str]:
    return sorted(glob.glob("/dev/hidraw*"))


def _read_string(fd: int, nr: int, size: int = 256) -> str:
    """Read a NUL-terminated string via a hidraw ``GRAW*`` ioctl."""
    import array

    buf = array.array("B", [0] * size)
    try:
        fcntl.ioctl(fd, _ioc(_IOC_READ, ord("H"), nr, size), buf, True)
    except OSError:
        return ""
    raw = buf.tobytes()
    end = raw.find(b"\x00")
    if end >= 0:
        raw = raw[:end]
    return raw.decode("utf-8", "replace")


def find_link(
    vendor: int = T95_VID,
    product: int = T95_PID,
    *,
    timeout: float = 0.0,
) -> HidLink | None:
    """Locate the T95's hidraw node.

    Returns ``None`` if the device is not attached.  If `timeout` is
    greater than zero, poll for up to that many seconds waiting for it to
    appear -- useful because the device has to be replugged or the kernel
    has to rebind the driver.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        found = _find_link_once(vendor, product)
        if found is not None or time.monotonic() >= deadline:
            return found
        time.sleep(0.5)


def _find_link_once(vendor: int, product: int) -> HidLink | None:
    """Scan the hidraw nodes for one matching ``vendor``/``product``.

    Identification is done primarily from sysfs, which needs no open file
    descriptor and therefore works even when the device node is not
    readable by this user.  The hidraw ioctls are only used to enrich the
    result with the advertised name and serial.
    """
    for node in _hidraw_nodes():
        strings = _read_uevent_strings(node)
        hid_id = strings.get("HID_ID", "")
        m = _HID_ID_RE.match(hid_id)
        hid_vendor = hid_product = None
        if m:
            hid_vendor = _parse_hex(m.group(2))
            hid_product = _parse_hex(m.group(3))

        advertised = strings.get("HID_NAME", "")
        uniq = strings.get("HID_UNIQ", "")

        # Best-effort enrichment from the device itself.
        io_vendor = io_product = None
        try:
            fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
        except OSError:
            fd = None
        if fd is not None:
            try:
                info = array_u32_u16_u16(fd)
                if info is not None:
                    _, iv, ip = info
                    io_vendor, io_product = iv & 0xFFFF, ip & 0xFFFF
                if not advertised:
                    advertised = _read_string(fd, 0x04)
                if not uniq:
                    uniq = _read_string(fd, 0x08)
            except OSError:
                pass
            finally:
                os.close(fd)

        found_vendor = next(
            (v for v in (io_vendor, hid_vendor) if v is not None), None
        )
        found_product = next(
            (p for p in (io_product, hid_product) if p is not None), None
        )

        matches = (
            found_vendor == vendor
            and found_product == product
        )
        if not matches and vendor == T95_VID and "linkam" in advertised.lower():
            matches = True

        if matches:
            return HidLink(
                node,
                vendor=found_vendor,
                product=found_product,
                name=advertised,
                uniq=uniq,
            )
    return None


def array_u32_u16_u16(fd: int) -> tuple[int, int, int] | None:
    """Read ``struct hidraw_devinfo`` (u32 bustype, s16 vendor, s16 product)."""
    import array

    buf = array.array("B", [0] * 8)
    try:
        fcntl.ioctl(fd, _HIDIOCGRAWINFO, buf, True)
    except OSError:
        return None
    bustype, vendor, product = struct.unpack("<Ih h".replace(" ", ""), buf.tobytes())
    return bustype, vendor, product


# --------------------------------------------------------------------------- #
# Report descriptor
# --------------------------------------------------------------------------- #


def read_report_descriptor(node: str) -> bytes:
    """Fetch the HID report descriptor via ``HIDIOCGRDESC``.

    The buffer layout is ``struct hidraw_report_descriptor``::

        __u32 size;
        __u8  value[HID_MAX_DESCRIPTOR_SIZE];

    but the ioctl size must be ``HID_MAX_DESCRIPTOR_SIZE`` (4096), not
    ``sizeof(struct)`` (4100) -- the kernel rejects anything larger with
    ``EINVAL``.  The descriptor bytes therefore start at offset 4 and run
    to the end of the 4096-byte buffer.

    Returns an empty ``bytes`` if the descriptor is unavailable (some
    vendor-specific HID devices do not expose one, and the ioctl fails
    when another driver holds the interface).
    """
    import array

    fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
    try:
        size_buf = array.array("i", [0])
        try:
            fcntl.ioctl(fd, _HIDIOCGRDESCSIZE, size_buf, True)
        except OSError:
            return b""
        size = size_buf[0]
        if not (0 < size <= HID_MAX_DESCRIPTOR_SIZE):
            return b""

        # struct hidraw_report_descriptor { __u32 size; __u8 value[4096]; }
        buf = array.array("B", [0] * (4 + HID_MAX_DESCRIPTOR_SIZE))
        struct.pack_into("<I", buf, 0, size)
        fcntl.ioctl(fd, _HIDIOCGRDESC, buf, True)
        out_size = struct.unpack_from("<I", buf, 0)[0]
        if not (0 < out_size <= HID_MAX_DESCRIPTOR_SIZE):
            out_size = size
        return buf[4 : 4 + out_size].tobytes()
    except OSError as exc:
        log.debug("report descriptor unavailable on %s: %s", node, exc)
        return b""
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# The transport
# --------------------------------------------------------------------------- #


class HidTransport:
    """Line-oriented transport over a USB HID link.

    Parameters
    ----------
    node:
        Path to the hidraw device, e.g. ``/dev/hidraw1``.  If omitted the
        T95 is looked up automatically.
    framing:
        One of :data:`FRAMINGS`.  Controls how the ASCII payload is
        mapped onto HID reports.
    report_size:
        Size in bytes of the output report to send.  ``None`` means
        "discover it" by trying the sizes the device accepts.
    input_report_id:
        Report ID to expect on input reports, if the device uses them.
        ``None`` means "accept any".
    output_report_id:
        Report ID to put in the first byte of output reports, if used.

    Notes
    -----
    The T95's report descriptor gives a payload of **63 bytes** in each
    direction, with **no report IDs**.  The default framing is therefore
    ``text_padded``: the ASCII payload starts at byte 0 and the rest of
    the report is zero padding.
    """

    #: Payload size of the T95's input and output reports, from its HID
    #: report descriptor (Report Count 63, Report Size 8).
    T95_REPORT_SIZE = 63

    def __init__(
        self,
        node: str | None = None,
        *,
        framing: str = "text_padded",
        report_size: int | None = None,
        input_report_id: int | None = None,
        output_report_id: int | None = None,
        vendor: int = T95_VID,
        product: int = T95_PID,
        timeout: float = 2.0,
    ) -> None:
        if framing not in FRAMINGS:
            raise ValueError(f"unknown framing {framing!r}; expected one of {FRAMINGS}")
        self.framing = framing
        self.report_size = report_size
        self.input_report_id = input_report_id
        self.output_report_id = output_report_id
        self.vendor = vendor
        self.product = product
        self.timeout = timeout
        self._fd: int | None = None
        self._node = node
        self._buf = bytearray()
        self._out_size = report_size
        self._out_id = output_report_id
        self._in_id = input_report_id

    # -- lifecycle -------------------------------------------------------- #
    @property
    def node(self) -> str:
        if self._node is None:
            raise ConnectionError("HID device not open")
        return self._node

    @property
    def description(self) -> str:
        return f"hid {self._node} framing={self.framing}"

    def open(self) -> None:
        if self._node is None:
            link = find_link(self.vendor, self.product, timeout=0.0)
            if link is None:
                raise ConnectionError(
                    f"Linkam T95 (VID {self.vendor:04x} PID {self.product:04x}) "
                    "not found on USB"
                )
            self._node = link.node
            log.info("found T95 at %s (%s)", link.node, link.name)

        self._fd = os.open(self._node, os.O_RDWR | os.O_CLOEXEC)
        log.info("opened %s", self._node)

        if self._out_size is None:
            self._out_size = self._determine_output_size()

        # Clear any stale state.
        self._buf.clear()

    def _determine_output_size(self) -> int:
        """Work out how long an output report should be.

        The report descriptor is the authoritative answer, so read it
        first.  That avoids probing, which would mean *writing* to the
        device -- something best avoided when merely opening it.

        If the descriptor cannot be read (some vendor-specific devices do
        not expose one, and the ioctl fails when another driver holds the
        interface), fall back to probing.
        """
        descriptor = read_report_descriptor(self._node)
        if descriptor:
            try:
                from .hid_descriptor import ReportDescriptorParser, report_sizes

                parser = ReportDescriptorParser()
                parser.parse(descriptor)
                sizes = report_sizes(parser)
                out_size = sizes.get("OUTPUT")
                if out_size:
                    log.info(
                        "output report size: %d bytes (from the report descriptor)",
                        out_size,
                    )
                    return out_size
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("could not derive the report size: %s", exc)

        if self.vendor == T95_VID and self.product == T95_PID:
            # The T95's descriptor says 63 bytes; use that if we could
            # not read it just now.
            log.info(
                "output report size: %d bytes (known T95 value)",
                self.T95_REPORT_SIZE,
            )
            return self.T95_REPORT_SIZE

        return self._discover_output_size()

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> "HidTransport":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- output size discovery -------------------------------------------- #
    #: Output report lengths to try, smallest first.
    #:
    #: 1 is excluded on purpose: the T95 rejects 1-byte output reports
    #: with ``EINVAL``.
    PROBE_SIZES = (2, 4, 8, 16, 32, 63, 64)

    def _discover_output_size(self) -> int:
        """Find the smallest output report the device accepts.

        Used only for devices whose report descriptor is unavailable.
        The T95's descriptor *is* available and states the size, so the
        transport does not probe it -- probing means writing to the
        instrument, and on this device a write it does not like can wedge
        the OUT endpoint.
        """
        for size in self.PROBE_SIZES:
            if self._try_output_size(size):
                log.info("output report size: %d bytes", size)
                return size
        # Nothing worked; fall back to the endpoint's maximum.
        log.warning("no output size accepted; defaulting to 64 bytes")
        return 64

    def _try_output_size(self, size: int) -> bool:
        """Test whether an output report of `size` bytes is accepted.

        The probe payload is all zeros.  For every framing currently in
        use this is inert: it cannot spell a command keyword, so it
        cannot start a ramp.
        """
        if self._fd is None:
            return False
        payload = self._build_output("", size=size)
        try:
            os.write(self._fd, payload)
        except OSError as exc:
            if exc.errno in (errno.EINVAL, errno.ENOSPC, errno.EPIPE):
                return False
            raise
        return True

    def probe_output_size(
        self,
        sizes: Iterable[int] | None = None,
        *,
        baseline: int = 2,
    ) -> tuple[int | None, list[dict]]:
        """Safely find the largest output report length the device accepts.

        Why not just use the descriptor
        --------------------------------
        The T95's report descriptor says its Output report is 63 bytes,
        but on real hardware a 63-byte output write times out
        (``ETIMEDOUT``) and leaves the OUT endpoint refusing further
        writes.  So the descriptor and the firmware disagree, and the
        only way to find out what the firmware will actually take is to
        ask it.

        Why this is safe
        ----------------
        A NAK on this device appears to be *sticky*: once a write has
        timed out, even writes that previously succeeded time out too.
        So the probe:

        * ascends, smallest first, so the sizes known to work are tried
          before the ones that might not;
        * stops at the first failure rather than pushing on;
        * re-checks liveness with a `baseline`-sized write after a
          failure, so the caller can tell "that size is rejected" from
          "the device has stopped talking to us entirely".

        The probe payload is all zeros at every size, so it cannot spell
        a command and cannot start a ramp.

        Returns
        -------
        (best_size, results)
            ``best_size`` is the largest size that was accepted, or
            ``None`` if nothing worked.  ``results`` is one record per
            size tried, for reporting.
        """
        if self._fd is None:
            raise ConnectionError("hidraw device is not open")

        sizes = list(sizes) if sizes is not None else list(self.PROBE_SIZES)
        results: list[dict] = []
        best: int | None = None

        for size in sizes:
            entry = {"size": size, "ok": False, "error": None, "alive_after": None}
            payload = self._build_output("", size=size)
            try:
                os.write(self._fd, payload)
                entry["ok"] = True
                best = size
            except OSError as exc:
                entry["error"] = errno.errorcode.get(exc.errno, str(exc.errno))
                # Did the device survive the rejection?  A `baseline`
                # write tells us whether the endpoint is still usable.
                try:
                    os.write(self._fd, self._build_output("", size=baseline))
                    entry["alive_after"] = True
                except OSError as exc2:
                    entry["alive_after"] = False
                    entry["alive_error"] = errno.errorcode.get(
                        exc2.errno, str(exc2.errno)
                    )
            results.append(entry)
            if not entry["ok"]:
                break

        return best, results

    # -- framing ---------------------------------------------------------- #
    def _build_output(self, text: str, *, size: int | None = None) -> bytes:
        """Encode an ASCII command into an HID output report."""
        size = size or self._out_size or 64
        body = text.encode("ascii", "replace")

        if self.framing == "raw":
            # Report ID first (if the device uses IDs), then the text.
            if self._out_id is not None:
                out = bytearray([self._out_id]) + body
            else:
                out = bytearray(body)
        elif self.framing == "id_text":
            out = bytearray([self._out_id if self._out_id is not None else 0])
            out += body
        elif self.framing == "text_padded":
            out = bytearray(body)
        elif self.framing == "length_prefixed":
            if len(body) < 256:
                out = bytearray([len(body)])
            else:
                out = bytearray(struct.pack("<H", len(body)))
            out += body
        else:  # pragma: no cover - guarded in __init__
            raise ValueError(self.framing)

        if len(out) > size:
            # Never silently truncate a command.  A truncated command is
            # not a harmless error: the instrument may read it as a
            # *different, valid* command.  `prog ramp 1 rate 5 limit 95
            # time 60` cut short at "limit 9" is a request to hold at
            # 9 degrees, and nothing would report the discrepancy.
            #
            # This is not hypothetical: an earlier version of this
            # transport discovered the output report size by probing,
            # found that a 2-byte write was accepted, and then truncated
            # every command to 2 bytes.  The framing probe dutifully
            # sent the two bytes "MA" to the instrument and reported
            # "NO RESPONSE under any framing" -- a puzzle that had
            # nothing to do with the framing.
            raise CommandTooLong(
                f"command of {len(out)} bytes does not fit in the device's "
                f"{size}-byte output report"
            )
        out += bytes(size - len(out))
        return bytes(out)

    def _extract_text(self, report: bytes) -> str:
        """Pull the ASCII payload out of an input report."""
        if self.framing == "raw":
            if self._in_id is not None and report and report[0] == self._in_id:
                report = report[1:]
            return report.decode("utf-8", "replace")
        if self.framing == "id_text":
            return report[1:].decode("utf-8", "replace") if len(report) > 1 else ""
        if self.framing == "text_padded":
            return report.decode("utf-8", "replace")
        if self.framing == "length_prefixed":
            if not report:
                return ""
            if report[0] < 256:
                n = report[0]
                return report[1 : 1 + n].decode("utf-8", "replace")
            n = struct.unpack_from("<H", report, 0)[0]
            return report[2 : 2 + n].decode("utf-8", "replace")
        return ""  # pragma: no cover

    # -- transport interface ---------------------------------------------- #
    def write_line(self, text: str, terminator: bytes) -> None:
        if self._fd is None:
            raise ConnectionError("hidraw device is not open")
        payload = self._build_output(text + terminator.decode("ascii", "replace"))
        log.debug("-> %r", payload)
        try:
            os.write(self._fd, payload)
        except OSError as exc:
            if exc.errno == errno.ETIMEDOUT:
                raise ConnectionError(
                    "the T95 did not accept the output report "
                    "(ETIMEDOUT): the device is enumerated and its report "
                    "descriptor reads correctly, but it is not servicing its "
                    "OUT endpoint. This is not a framing problem. Power-cycle "
                    "the stage (off, wait 10 s, on) and replug the USB cable; "
                    "if it persists, reset the USB device with "
                    "'sudo ./tools/reset_usb_device.sh'."
                ) from exc
            raise

    def read_line(self, timeout: float) -> str:
        if self._fd is None:
            raise ConnectionError("hidraw device is not open")
        deadline = time.monotonic() + timeout
        while True:
            line = self._take_line()
            if line is not None:
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportTimeout(f"no line within {timeout}s")
            # Poll so that the timeout is honoured even when the device is
            # completely silent.
            import select

            r, _, _ = select.select([self._fd], [], [], min(remaining, 0.2))
            if r:
                try:
                    data = os.read(self._fd, 64)
                except OSError as exc:
                    if exc.errno == errno.EAGAIN:
                        continue
                    raise
                if not data:
                    raise ConnectionError("hidraw device closed")
                self._buf += data

    def _take_line(self) -> str | None:
        """Pop one complete line from the receive buffer, if there is one."""
        # A NUL is the natural string terminator in a fixed-size report.
        nul = self._buf.find(b"\x00")
        cr = self._buf.find(b"\r")
        lf = self._buf.find(b"\n")

        candidates = [p for p in (nul, cr, lf) if p >= 0]
        if not candidates:
            return None
        end = min(candidates)
        raw = bytes(self._buf[:end])
        del self._buf[: end + 1]

        text = raw.decode("utf-8", "replace").strip()
        # Strip a leading report ID if the framing uses one.
        if self.framing == "id_text" and text:
            text = text[1:]
        return text or None

    # -- raw access, for the discovery helper ----------------------------- #
    def write_report(self, payload: bytes) -> None:
        """Write one raw output report, with no ASCII framing applied.

        The T95's real protocol is binary, not line-oriented, so commands
        and status polls have to go out as raw bytes.  The payload is
        zero-padded to the device's output report size.

        Unlike :meth:`write_line` this does **not** raise
        :class:`~linkam.transport.CommandTooLong` for a payload that is
        too long -- it is a raw write, so the caller decides.  A payload
        longer than the report size is truncated, because at this level
        there is no command to misread.
        """
        if self._fd is None:
            raise ConnectionError("hidraw device is not open")
        size = self._out_size or self.T95_REPORT_SIZE
        out = payload[:size]
        out += bytes(size - len(out))
        log.debug("-> raw %r", out)
        try:
            os.write(self._fd, out)
        except OSError as exc:
            if exc.errno == errno.ETIMEDOUT:
                raise ConnectionError(
                    "the T95 did not accept the output report "
                    "(ETIMEDOUT): the device is enumerated and its report "
                    "descriptor reads correctly, but it is not servicing its "
                    "OUT endpoint. This is not a framing problem. Power-cycle "
                    "the stage (off, wait 10 s, on) and replug the USB cable; "
                    "if it persists, reset the USB device with "
                    "'sudo ./tools/reset_usb_device.sh'."
                ) from exc
            raise

    def read_reports(self, count: int, timeout: float = 1.0) -> list[bytes]:
        """Read up to `count` raw input reports, stopping at `timeout`.

        Returns fewer than `count` reports if the device stops sending or
        the timeout expires; it never blocks for longer than `timeout`
        in total.
        """
        reports: list[bytes] = []
        deadline = time.monotonic() + timeout
        while len(reports) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            report = self.read_report(remaining)
            if report is None:
                break
            reports.append(report)
        return reports

    def read_report(self, timeout: float = 1.0) -> bytes | None:
        """Read one raw input report, or ``None`` on timeout."""
        if self._fd is None:
            raise ConnectionError("hidraw device is not open")
        import select

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            r, _, _ = select.select([self._fd], [], [], min(remaining, 0.1))
            if not r:
                continue
            try:
                data = os.read(self._fd, 64)
            except OSError as exc:
                if exc.errno == errno.EAGAIN:
                    continue
                raise
            if not data:
                raise ConnectionError("hidraw device closed")
            return data


def discover(
    node: str | None = None,
    *,
    commands: Sequence[str] = ("MA6",),
    timeouts: Iterable[float] = (0.5, 1.5, 3.0),
) -> list[dict]:
    """Try each framing against the device and report which one answers.

    This is read-only with respect to the instrument's state: it sends
    only the stage-register query ``MA6``, which reads a register and
    changes nothing.  It returns one result dict per framing so the
    caller can pick the winner and hard-code it.
    """
    results: list[dict] = []
    for framing in FRAMINGS:
        transport = HidTransport(node, framing=framing)
        entry: dict = {"framing": framing, "response": None, "error": None}
        try:
            transport.open()
            for cmd in commands:
                for timeout in timeouts:
                    try:
                        transport.write_line(cmd, b"\r\n")
                    except OSError as exc:
                        entry["error"] = f"write failed: {exc}"
                        break
                    try:
                        line = transport.read_line(timeout)
                    except TransportTimeout:
                        continue
                    except OSError as exc:
                        entry["error"] = f"read failed: {exc}"
                        break
                    if line:
                        entry["response"] = line
                        break
                if entry["response"]:
                    break
        except Exception as exc:  # pragma: no cover - diagnostic path
            entry["error"] = repr(exc)
        finally:
            transport.close()
        results.append(entry)
        log.info("framing %-17s -> %s", framing, entry["response"] or entry["error"] or "no reply")
    return results
