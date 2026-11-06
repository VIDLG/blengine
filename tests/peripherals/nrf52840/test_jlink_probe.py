"""USB identity, unique VCOM and physical-lease regressions without hardware."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from . import jlink_probe as device
from .resource_lease import ResourceLeaseError


class JlinkProbeTests(unittest.TestCase):
    def test_probe_serial_validation_is_explicit(self) -> None:
        for value in ("", "auto", "1234x", "1.0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                device.validate_probe_serial(value)
        device.validate_probe_serial("123456789")

    def test_probe_serial_matching_ignores_only_decimal_leading_zero_formatting(
        self,
    ) -> None:
        ports = [
            SimpleNamespace(
                device="/dev/cu.usbmodem0010502520281",
                vid=0x1366,
                pid=0x1061,
                serial_number="001050252028",
                location="2-1.1",
            ),
            SimpleNamespace(
                device="/dev/cu.usbmodem0010502520283",
                vid=0x1366,
                pid=0x1061,
                serial_number="001050252028",
                location="2-1.1",
            ),
        ]
        with patch.object(device, "_list_ports", return_value=ports):
            probe = device.verify_probe("1050252028")
        self.assertEqual(probe.pid, 0x1061)
        self.assertEqual(probe.location, "2-1.1")
        self.assertEqual(probe.ports, tuple(port.device for port in ports))

    def test_enumerate_probes_groups_all_visible_vcoms_by_stable_serial(self) -> None:
        ports = [
            SimpleNamespace(
                device="/dev/cu.usbmodem0010502514793",
                vid=0x1366,
                pid=0x1061,
                serial_number="001050251479",
                location="2-1.2",
            ),
            SimpleNamespace(
                device="/dev/cu.usbmodem0010502520281",
                vid=0x1366,
                pid=0x1061,
                serial_number="001050252028",
                location="2-1.1",
            ),
            SimpleNamespace(
                device="/dev/cu.usbmodem0010502514791",
                vid=0x1366,
                pid=0x1061,
                serial_number="001050251479",
                location="2-1.2",
            ),
        ]
        with patch.object(device, "_list_ports", return_value=ports):
            probes = device.enumerate_probes()
        self.assertEqual([probe.serial_number for probe in probes], ["1050251479", "1050252028"])
        self.assertEqual(probes[0].ports, ("/dev/cu.usbmodem0010502514791", "/dev/cu.usbmodem0010502514793"))
        self.assertEqual(probes[1].location, "2-1.1")

    def test_detect_btp_port_probes_all_vcoms_before_rejecting_two_responders(
        self,
    ) -> None:
        probe = device.ProbeIdentity(1, "usb-location", ("/dev/A", "/dev/B"))
        opened: list[str] = []
        required = 1 | (1 << device.GAP) | (1 << device.GATT)

        @contextmanager
        def fake_open(port: str):
            opened.append(port)
            yield port

        class FakeCore:
            def __init__(self, session: str) -> None:
                self.session = session

            def read_supported_services(self) -> int:
                return required

        with (
            patch.object(device, "owned_lease"),
            patch.object(device, "verify_probe", return_value=probe),
            patch.object(device, "open_btp_port", side_effect=fake_open),
            patch.object(
                device, "SerialByteTransport", side_effect=lambda value: value
            ),
            patch.object(device, "FramedTransport", side_effect=lambda value: value),
            patch.object(device, "BtpSession", side_effect=lambda value: value),
            patch.object(device, "CoreCommands", FakeCore),
            self.assertRaisesRegex(device.ProbeDetectionError, "exactly one BTP VCOM"),
        ):
            device.detect_btp_port("123456789", requested_port="/dev/A")
        self.assertEqual(opened, ["/dev/A", "/dev/B"])

    def test_detect_btp_port_requires_requested_port_to_be_unique_responder(
        self,
    ) -> None:
        probe = device.ProbeIdentity(1, "usb-location", ("/dev/A", "/dev/B"))
        required = 1 | (1 << device.GAP) | (1 << device.GATT)

        @contextmanager
        def fake_open(port: str):
            yield port

        class FakeCore:
            def __init__(self, session: str) -> None:
                self.session = session

            def read_supported_services(self) -> int:
                return required if self.session == "/dev/A" else 0

        with (
            patch.object(device, "verify_probe", return_value=probe),
            patch.object(device, "open_btp_port", side_effect=fake_open),
            patch.object(
                device, "SerialByteTransport", side_effect=lambda value: value
            ),
            patch.object(device, "FramedTransport", side_effect=lambda value: value),
            patch.object(device, "BtpSession", side_effect=lambda value: value),
            patch.object(device, "CoreCommands", FakeCore),
            patch.object(device, "owned_lease"),
            self.assertRaisesRegex(
                device.ProbeDetectionError, "not the unique BTP responder"
            ),
        ):
            device.detect_btp_port("123456789", requested_port="/dev/B")

    def test_detection_conflict_precedes_probe_and_serial_access(self) -> None:
        with (
            patch.object(device, "owned_lease") as ownership,
            patch.object(device, "verify_probe") as probe,
            patch.object(device, "open_btp_port") as open_port,
            self.assertRaisesRegex(ResourceLeaseError, "busy"),
        ):
            ownership.return_value.__enter__.side_effect = ResourceLeaseError("busy")
            device.detect_btp_port("123")
        probe.assert_not_called()
        open_port.assert_not_called()
