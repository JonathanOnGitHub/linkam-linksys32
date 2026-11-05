"""Tests for the USB topology diagnostic.

The T95 is a USB 2.0 device with latency-sensitive HID transfers.  A
USB 3 camera on the same root hub can starve it, and a starved T95
stops servicing its OUT endpoint altogether — the symptom is a write
that times out and a controller that needs a power cycle to recover.
So the diagnostic has to spot a shared root hub before it causes that.
"""

from __future__ import annotations

import pytest

from linkam.diagnostics import (
    UsbDevice,
    _parent_of,
    usb_topology_report,
)


def make_devices() -> list[UsbDevice]:
    """A laptop with the T95 and an Alvium on the same root hub."""
    return [
        UsbDevice(name="usb1", vendor="1d6b", product="0002", speed="480", busnum="1"),
        UsbDevice(name="1-1", vendor="1d6b", product="0003", speed="5000", busnum="1"),
        UsbDevice(name="1-2", vendor="0409", product="005a", speed="480", busnum="1"),
        UsbDevice(name="1-2.1", vendor="16da", product="0002", speed="480", busnum="1"),
        UsbDevice(name="1-3", vendor="1d6b", product="0003", speed="5000", busnum="1"),
        UsbDevice(name="1-3.2", vendor="1cf0", product="0052", speed="5000", busnum="1"),
    ]


def test_parent_of_a_plain_device():
    assert _parent_of("1-2") == "1"


def test_parent_of_a_device_behind_a_hub():
    assert _parent_of("1-2.1") == "1-2"


def test_parent_of_a_root_hub_is_none():
    assert _parent_of("usb1") is None


def test_super_speed_is_recognised():
    assert UsbDevice(name="x", speed="5000").is_super_speed
    assert UsbDevice(name="x", speed="10000").is_super_speed
    assert not UsbDevice(name="x", speed="480").is_super_speed
    assert not UsbDevice(name="x", speed="").is_super_speed


def test_a_root_hub_is_recognised():
    assert UsbDevice(name="usb1").is_root_hub
    assert not UsbDevice(name="1-2").is_root_hub


def test_the_topology_finds_the_t95(monkeypatch):
    monkeypatch.setattr(
        "linkam.diagnostics.usb_devices", lambda: make_devices()
    )
    info = usb_topology_report()
    assert info["t95"] is not None
    assert info["t95"].name == "1-2.1"


def test_the_topology_finds_the_root_hub(monkeypatch):
    monkeypatch.setattr(
        "linkam.diagnostics.usb_devices", lambda: make_devices()
    )
    info = usb_topology_report()
    assert info["root_hub"].name == "usb1"


def test_a_usb3_camera_on_the_same_root_hub_is_flagged(monkeypatch):
    monkeypatch.setattr(
        "linkam.diagnostics.usb_devices", lambda: make_devices()
    )
    info = usb_topology_report()
    assert info["shares_root_hub_with_super_speed"] is True
    assert any(d.name == "1-3.2" for d in info["super_speed_devices"])


def test_a_t95_on_its_own_root_hub_is_not_flagged(monkeypatch):
    """The camera is on bus 3, the T95 on bus 1: no shared root hub."""
    devices = [
        UsbDevice(name="usb1", vendor="1d6b", product="0002", speed="480", busnum="1"),
        UsbDevice(name="1-2", vendor="0409", product="005a", speed="480", busnum="1"),
        UsbDevice(name="1-2.1", vendor="16da", product="0002", speed="480", busnum="1"),
        UsbDevice(name="usb3", vendor="1d6b", product="0003", speed="5000", busnum="3"),
        UsbDevice(name="3-1", vendor="1cf0", product="0052", speed="5000", busnum="3"),
    ]
    monkeypatch.setattr("linkam.diagnostics.usb_devices", lambda: devices)
    info = usb_topology_report()
    assert info["shares_root_hub_with_super_speed"] is False
    assert info["super_speed_devices"] == []


def test_a_t95_behind_a_usb3_hub_is_flagged(monkeypatch):
    """Even with no camera, a SuperSpeed parent hub is worth knowing about."""
    devices = [
        UsbDevice(name="usb1", vendor="1d6b", product="0002", speed="480", busnum="1"),
        UsbDevice(name="1-1", vendor="1d6b", product="0003", speed="5000", busnum="1"),
        UsbDevice(name="1-1.2", vendor="16da", product="0002", speed="480", busnum="1"),
    ]
    monkeypatch.setattr("linkam.diagnostics.usb_devices", lambda: devices)
    info = usb_topology_report()
    assert info["parent_is_super_speed"] is True


def test_no_t95_means_no_topology(monkeypatch):
    monkeypatch.setattr("linkam.diagnostics.usb_devices", lambda: [])
    info = usb_topology_report()
    assert info["t95"] is None
    assert info["shares_root_hub_with_super_speed"] is False


def test_the_read_usb_devices_helper_does_not_blow_up():
    """On a machine with no USB at all it must return something sane."""
    from linkam.diagnostics import usb_devices

    devices = usb_devices()
    assert isinstance(devices, list)
