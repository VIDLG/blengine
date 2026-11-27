"""Configured DK inventory tests without hardware."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import inventory
from .jlink_probe import EnumeratedProbe


class InventoryTests(unittest.TestCase):
    def _config(self, text: str) -> Path:
        directory = Path(tempfile.mkdtemp())
        path = directory / "blehub.local.toml"
        path.write_text(text, encoding="utf-8")
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        return path

    def test_inventory_matches_configured_devices_without_names_in_logic(self) -> None:
        config = self._config(
            """
[peripheral_nrf52840.devices.alpha]
probe_serial = "00123"
port = "/dev/cu.alpha1"
profile = "alpha.json"

[peripheral_nrf52840.devices.beta]
probe_serial = "00456"
port = "/dev/cu.beta1"
profile = "beta.json"
"""
        )
        observed = (
            EnumeratedProbe("123", 1, "usb-a", ("/dev/cu.alpha1", "/dev/cu.alpha3")),
            EnumeratedProbe("456", 2, "usb-b", ("/dev/cu.beta9",)),
        )
        with patch.object(inventory, "enumerate_probes", return_value=observed):
            result = inventory.inspect(config)
        self.assertEqual(result["status"], "passed-port-drift")
        self.assertEqual(result["configured_device_count"], 2)
        self.assertEqual(result["observed_probe_count"], 2)
        self.assertEqual(result["devices"][0]["configured_port_status"], "match")
        self.assertEqual(result["devices"][1]["configured_port_status"], "drift")
        self.assertEqual(result["unconfigured_probes"], [])

    def test_inventory_fails_when_one_configured_serial_is_absent(self) -> None:
        config = self._config(
            """
[peripheral_nrf52840.devices.first]
probe_serial = "123"
"""
        )
        observed = (EnumeratedProbe("999", 1, "usb-a", ("/dev/cu.other",)),)
        with patch.object(inventory, "enumerate_probes", return_value=observed):
            result = inventory.inspect(config)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["devices"][0]["connected"])
        self.assertEqual(len(result["unconfigured_probes"]), 1)

    def test_inventory_rejects_duplicate_or_invalid_configuration_shape(self) -> None:
        config = self._config(
            """
[peripheral_nrf52840.devices.first]
probe_serial = "123"

[peripheral_nrf52840.devices.second]
probe_serial = "123"
"""
        )
        with self.assertRaisesRegex(ValueError, "duplicate configured probe_serial"):
            inventory.load_configured_devices(config)

        invalid = self._config(
            """
[peripheral_nrf52840.devices.first]
probe_serial = "auto"
"""
        )
        with self.assertRaisesRegex(ValueError, "probe_serial"):
            inventory.load_configured_devices(invalid)


if __name__ == "__main__":
    unittest.main()
