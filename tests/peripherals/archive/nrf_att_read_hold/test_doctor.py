"""Hardware-free ownership and read-only boundary tests for the DK doctor."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests.peripherals.archive.nrf_att_read_hold import device, doctor
from tests.peripherals.archive.nrf_att_read_hold.read_hold import (
    HoldState,
    Identity,
    Snapshot,
)
from tests.peripherals.nrf52840.resource_lease import ResourceLeaseError


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ownership = self.enterContext(patch.object(doctor, "owned_lease"))

    def test_conflicting_physical_owner_precedes_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(device, "FIXTURE_PID", Path(temp) / "absent.pid"),
                patch.object(device, "RN_LOCK", Path(temp) / "absent.lock"),
                patch.object(device, "detect_btp_port") as detect,
                patch.object(device, "open_btp_port") as open_port,
                self.assertRaisesRegex(ResourceLeaseError, "busy"),
            ):
                self.ownership.return_value.__enter__.side_effect = ResourceLeaseError(
                    "busy"
                )
                doctor.inspect("123")
            detect.assert_not_called()
            open_port.assert_not_called()

    def test_refuses_provider_and_rn_ownership_before_opening_serial(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pid = Path(temp) / "provider.pid"
            lock = Path(temp) / "rn.lock"
            with (
                patch.object(device, "FIXTURE_PID", pid),
                patch.object(device, "RN_LOCK", lock),
                patch.object(device, "detect_btp_port") as detect,
            ):
                for owner in (pid, lock):
                    owner.touch()
                    with self.assertRaisesRegex(device.DiagnosticError, "owns the DK"):
                        doctor.inspect("001050252028")
                    owner.unlink()
                detect.assert_not_called()

    def test_only_reads_core_capabilities_and_hold_status(self) -> None:
        probe = device.ProbeIdentity(4193, "2-1.1", ("/dev/cu.usbmodem1",))
        snapshot = Snapshot(HoldState.RELEASED, Identity(22, 41))
        requests: list[str] = []

        @contextmanager
        def fake_open(_port: str):
            yield object()

        class FakeCore:
            def __init__(self, _session: object) -> None:
                pass

            def read_supported_services(self) -> int:
                requests.append("services")
                return 1 | (1 << doctor.GAP) | (1 << doctor.GATT)

            def read_supported_commands(self, service: int) -> int:
                assert service == doctor.GATT
                requests.append("commands")
                return device.READ_HOLD_COMMAND_MASK

        class FakeHold:
            def __init__(self, _session: object) -> None:
                pass

            def status(self) -> Snapshot:
                requests.append("status")
                return snapshot

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(device, "FIXTURE_PID", Path(temp) / "provider.pid"),
            patch.object(device, "RN_LOCK", Path(temp) / "rn.lock"),
            patch.object(
                device, "detect_btp_port", return_value=(probe.ports[0], probe)
            ) as detect,
            patch.object(device, "verify_probe", return_value=probe),
            patch.object(device, "open_btp_port", side_effect=fake_open),
            patch.object(
                doctor, "SerialByteTransport", side_effect=lambda value: value
            ),
            patch.object(doctor, "FramedTransport", side_effect=lambda value: value),
            patch.object(doctor, "BtpSession", side_effect=lambda value: value),
            patch.object(doctor, "CoreCommands", FakeCore),
            patch.object(doctor, "ReadHold", FakeHold),
        ):
            result = doctor.inspect("001050252028", probe.ports[0])
        self.assertEqual(result.snapshot, snapshot)
        self.assertEqual(requests, ["services", "commands", "status"])
        detect.assert_called_once_with(
            "001050252028",
            probe.ports[0],
            lease=self.ownership.return_value.__enter__.return_value,
        )
        self.ownership.return_value.__exit__.assert_called_once()

    def test_fails_closed_on_missing_commands_or_active_hold(self) -> None:
        mask = 0

        class FakeCore:
            def __init__(self, _session: object) -> None:
                pass

            def read_supported_services(self) -> int:
                return 1 | (1 << doctor.GAP) | (1 << doctor.GATT)

            def read_supported_commands(self, _service: int) -> int:
                return mask

        class FakeHold:
            def __init__(self, _session: object) -> None:
                pass

            def status(self) -> Snapshot:
                return Snapshot(HoldState.WAITING, Identity(22, 41))

        @contextmanager
        def fake_open(_port: str):
            yield object()

        probe = device.ProbeIdentity(4193, "2-1.1", ("/dev/cu.usbmodem1",))
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch.object(device, "FIXTURE_PID", Path(temp) / "provider.pid"),
                patch.object(device, "RN_LOCK", Path(temp) / "rn.lock"),
                patch.object(
                    device, "detect_btp_port", return_value=(probe.ports[0], probe)
                ),
                patch.object(device, "open_btp_port", side_effect=fake_open),
                patch.object(
                    doctor, "SerialByteTransport", side_effect=lambda value: value
                ),
                patch.object(
                    doctor, "FramedTransport", side_effect=lambda value: value
                ),
                patch.object(doctor, "BtpSession", side_effect=lambda value: value),
                patch.object(doctor, "CoreCommands", FakeCore),
                self.assertRaisesRegex(
                    device.DiagnosticError, "capabilities are missing"
                ),
            ):
                doctor.inspect("001050252028")
            mask = device.READ_HOLD_COMMAND_MASK
            with (
                patch.object(device, "FIXTURE_PID", Path(temp) / "provider.pid"),
                patch.object(device, "RN_LOCK", Path(temp) / "rn.lock"),
                patch.object(
                    device, "detect_btp_port", return_value=(probe.ports[0], probe)
                ),
                patch.object(device, "open_btp_port", side_effect=fake_open),
                patch.object(
                    doctor, "SerialByteTransport", side_effect=lambda value: value
                ),
                patch.object(
                    doctor, "FramedTransport", side_effect=lambda value: value
                ),
                patch.object(doctor, "BtpSession", side_effect=lambda value: value),
                patch.object(doctor, "CoreCommands", FakeCore),
                patch.object(doctor, "ReadHold", FakeHold),
                self.assertRaisesRegex(device.DiagnosticError, "firmware is busy"),
            ):
                doctor.inspect("001050252028")


if __name__ == "__main__":
    unittest.main()
