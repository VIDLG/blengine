"""Tests for the physical installed-image attestation capture boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .attestation import AttestationCaptureError, capture
from .fixture_attestation import InstalledImageAttestation
from .fixture_contract import MULTIFUNCTION_CAPABILITIES
from .resource_lease import ResourceLeaseError


class AttestationCaptureTests(unittest.TestCase):
    def test_capture_writes_wire_attestation_and_provenance_atomically(self) -> None:
        probe = SimpleNamespace(
            pid=0x1061,
            location="1-2",
            ports=("/dev/cu.usbmodemBTP",),
        )
        installed = InstalledImageAttestation(
            image_id="nrf-multifunction-rf-v2-a",
            image_version="2",
            image_sha256="a" * 64,
            board="nrf52840dk/nrf52840",
            profile_id="blehub-nrf52840-multifunction-v2-a",
            service_uuid="fd600000-0000-4000-8000-000000000000",
            local_name="BleHubMultiRfV2DKA",
            capabilities=frozenset(MULTIFUNCTION_CAPABILITIES),
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "installed-attestation.json"
            with (
                patch("tests.peripherals.nrf52840.attestation.owned_lease"),
                patch(
                    "tests.peripherals.nrf52840.attestation.detect_btp_port",
                    return_value=(probe.ports[0], probe),
                ),
                patch(
                    "tests.peripherals.nrf52840.attestation.verify_probe",
                    side_effect=[probe, probe],
                ),
                patch(
                    "tests.peripherals.nrf52840.attestation.open_btp_port",
                ) as open_port,
                patch(
                    "tests.peripherals.nrf52840.attestation.VendorCommands"
                ) as vendor,
            ):
                open_port.return_value.__enter__.return_value = object()
                vendor.return_value.read_image_attestation.return_value = installed
                document, provenance_path = capture(
                    probe_serial="1050252028",
                    requested_port=probe.ports[0],
                    output=output,
                )

            self.assertEqual(document["image_id"], installed.image_id)
            self.assertEqual(json.loads(output.read_text()), document)
            provenance = json.loads(provenance_path.read_text())
            self.assertEqual(provenance["probe_serial"], "1050252028")
            self.assertEqual(provenance["port"], probe.ports[0])
            self.assertEqual(provenance["attestation"], document)
            vendor.return_value.read_image_attestation.assert_called_once_with()

    def test_capture_rejects_probe_identity_change_and_writes_nothing(self) -> None:
        first = SimpleNamespace(
            pid=0x1061, location="1-2", ports=("/dev/cu.usbmodemBTP",)
        )
        second = SimpleNamespace(pid=0x1061, location="1-3", ports=first.ports)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "installed-attestation.json"
            with (
                patch("tests.peripherals.nrf52840.attestation.owned_lease"),
                patch(
                    "tests.peripherals.nrf52840.attestation.detect_btp_port",
                    return_value=(first.ports[0], first),
                ),
                patch(
                    "tests.peripherals.nrf52840.attestation.verify_probe",
                    side_effect=[second],
                ),
                self.assertRaisesRegex(
                    AttestationCaptureError, "changed before attestation read"
                ),
            ):
                capture(probe_serial="1050252028", output=output)
            self.assertFalse(output.exists())
            self.assertFalse(
                output.with_name(output.name + ".provenance.json").exists()
            )

    def test_conflict_fails_before_probe_or_serial_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "attestation.json"
            with (
                patch(
                    "tests.peripherals.nrf52840.attestation.owned_lease"
                ) as ownership,
                patch(
                    "tests.peripherals.nrf52840.attestation.detect_btp_port"
                ) as detect,
                patch(
                    "tests.peripherals.nrf52840.attestation.open_btp_port"
                ) as open_port,
                self.assertRaisesRegex(ResourceLeaseError, "busy"),
            ):
                ownership.return_value.__enter__.side_effect = ResourceLeaseError(
                    "busy"
                )
                capture(probe_serial="123", output=output)
            detect.assert_not_called()
            open_port.assert_not_called()
            self.assertFalse(output.exists())

    def test_capture_refuses_symlink_output(self) -> None:
        probe = SimpleNamespace(
            pid=0x1061, location="1-2", ports=("/dev/cu.usbmodemBTP",)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("keep\n", encoding="utf-8")
            output = root / "installed-attestation.json"
            output.symlink_to(target)
            with self.assertRaisesRegex(AttestationCaptureError, "symlink output"):
                from .attestation import _atomic_json

                _atomic_json(output, {"schema_version": 1})
            self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(probe.pid, 0x1061)


if __name__ == "__main__":
    unittest.main()
