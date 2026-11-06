from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import multi_fixture


class MultiFixtureConfigTests(unittest.TestCase):
    def test_load_devices_requires_distinct_explicit_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile_a = root / "a.json"
            profile_b = root / "b.json"
            profile_a.write_text("{}", encoding="utf-8")
            profile_b.write_text("{}", encoding="utf-8")
            config = root / "blehub.local.toml"
            config.write_text(
                """
[peripheral_nrf52840.devices.dk_a]
probe_serial = "001050252028"
port = "/dev/cu.a"
profile = "a.json"

[peripheral_nrf52840.devices.dk_b]
probe_serial = "001050251479"
port = "/dev/cu.b"
profile = "b.json"
""",
                encoding="utf-8",
            )
            with (
                patch.object(multi_fixture, "LOCAL_CONFIG_PATH", config),
                patch.object(multi_fixture, "PROJECT_ROOT", root),
            ):
                devices = multi_fixture.load_devices()
        self.assertEqual([device.name for device in devices], ["dk_a", "dk_b"])
        self.assertEqual(
            [device.probe_serial for device in devices],
            [
                "001050252028",
                "001050251479",
            ],
        )
        self.assertEqual(
            [device.port for device in devices], ["/dev/cu.a", "/dev/cu.b"]
        )
        self.assertEqual(
            [device.profile.name for device in devices], ["a.json", "b.json"]
        )

    def test_load_devices_can_select_one_explicit_device_for_profile_diagnosis(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "b.json"
            profile.write_text("{}", encoding="utf-8")
            config = root / "blehub.local.toml"
            config.write_text(
                """
[peripheral_nrf52840.devices.dk_b]
probe_serial = "001050251479"
port = "/dev/cu.b"
profile = "b.json"
""",
                encoding="utf-8",
            )
            with (
                patch.object(multi_fixture, "LOCAL_CONFIG_PATH", config),
                patch.object(multi_fixture, "PROJECT_ROOT", root),
            ):
                devices = multi_fixture.load_devices(names=("dk_b",))
        self.assertEqual([device.name for device in devices], ["dk_b"])

    def test_load_devices_rejects_duplicate_ports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a.json", "b.json"):
                (root / name).write_text("{}", encoding="utf-8")
            config = root / "blehub.local.toml"
            config.write_text(
                """
[peripheral_nrf52840.devices.dk_a]
probe_serial = "a"
port = "/dev/cu.same"
profile = "a.json"

[peripheral_nrf52840.devices.dk_b]
probe_serial = "b"
port = "/dev/cu.same"
profile = "b.json"
""",
                encoding="utf-8",
            )
            with (
                patch.object(multi_fixture, "LOCAL_CONFIG_PATH", config),
                patch.object(multi_fixture, "PROJECT_ROOT", root),
                self.assertRaisesRegex(
                    multi_fixture.MultiFixtureError, "ports must be distinct"
                ),
            ):
                multi_fixture.load_devices()


class MultiFixtureControllerTests(unittest.TestCase):
    def test_single_device_requires_explicit_opt_in(self) -> None:
        device = multi_fixture.DeviceConfig("dk_b", "b", "/dev/b", Path("b.json"))
        with self.assertRaisesRegex(multi_fixture.MultiFixtureError, "at least two"):
            multi_fixture.MultiFixtureController((device,))
        controller = multi_fixture.MultiFixtureController(
            (device,), allow_single_device=True
        )
        self.assertEqual(controller.devices, (device,))

    def test_readiness_owns_and_releases_the_multi_fixture_controller(self) -> None:
        controller = SimpleNamespace(
            devices=(object(), object()),
            start=Mock(),
            stop=Mock(),
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(
                multi_fixture, "MultiFixtureController", return_value=controller
            ),
        ):
            multi_fixture.readiness(work_root=Path(temporary))
        controller.start.assert_called_once_with()
        controller.stop.assert_called_once_with()

    def test_port_serial_mismatch_fails_closed(self) -> None:
        devices = (
            multi_fixture.DeviceConfig("dk_a", "serial-a", "/dev/a", Path("a.json")),
            multi_fixture.DeviceConfig("dk_b", "serial-b", "/dev/b", Path("b.json")),
        )
        with self.assertRaisesRegex(
            multi_fixture.MultiFixtureError, "mapping mismatch"
        ):
            multi_fixture._validate_discovered_mappings(
                devices, {"/dev/a": "serial-a", "/dev/b": "wrong"}
            )

    def test_start_is_failure_atomic_when_second_device_is_not_ready(self) -> None:
        devices = (
            multi_fixture.DeviceConfig("dk_a", "a", "/dev/a", Path("a.json")),
            multi_fixture.DeviceConfig("dk_b", "b", "/dev/b", Path("b.json")),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            processes: list[SimpleNamespace] = []

            def launch(*_: object, **__: object) -> SimpleNamespace:
                process = SimpleNamespace(pid=100 + len(processes), poll=lambda: None)
                processes.append(process)
                return process

            with (
                patch.object(multi_fixture, "validate_device_mappings"),
                patch.object(multi_fixture.subprocess, "Popen", side_effect=launch),
                patch.object(multi_fixture.os, "getpgid", side_effect=lambda pid: pid),
                patch.object(
                    multi_fixture,
                    "_wait_ready",
                    side_effect=[
                        None,
                        multi_fixture.MultiFixtureError("dk_b not ready"),
                    ],
                ),
                patch.object(multi_fixture, "_stop_process") as stop_process,
            ):
                controller = multi_fixture.MultiFixtureController(
                    devices, work_root=root / "fixture"
                )
                with self.assertRaisesRegex(
                    multi_fixture.MultiFixtureError, "dk_b not ready"
                ):
                    controller.start()

        self.assertEqual([owned.config.name for owned in controller.owned], [])
        self.assertEqual(stop_process.call_count, 2)

    def test_check_health_reports_child_failure_with_log_tail(self) -> None:
        device = multi_fixture.DeviceConfig("dk_b", "b", "/dev/b", Path("b.json"))
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "dk_b.log"
            log_path.write_text("subscription timeout\n", encoding="utf-8")
            process = cast(
                subprocess.Popen[str], cast(object, SimpleNamespace(poll=lambda: 2))
            )
            owned = multi_fixture._OwnedProcess(
                config=device,
                log_path=log_path,
                process=process,
                pid=1,
                pgid=1,
            )
            controller = multi_fixture.MultiFixtureController(
                (device,), allow_single_device=True
            )
            controller._owned.append(owned)
            with self.assertRaisesRegex(
                multi_fixture.MultiFixtureError,
                r"(?s)dk_b fixture exited with status 2:.*subscription timeout",
            ):
                controller.check_health()

    def test_stop_device_releases_only_the_selected_fixture(self) -> None:
        devices = (
            multi_fixture.DeviceConfig("dk_a", "a", "/dev/a", Path("a.json")),
            multi_fixture.DeviceConfig("dk_b", "b", "/dev/b", Path("b.json")),
        )
        first = SimpleNamespace(config=devices[0])
        second = SimpleNamespace(config=devices[1])
        controller = multi_fixture.MultiFixtureController(devices)
        controller._owned.extend((first, second))
        with patch.object(multi_fixture, "_stop_process") as stop_process:
            controller.stop_device("dk_a")
        stop_process.assert_called_once_with(first)
        self.assertEqual(controller.owned, (second,))

    def test_restart_device_replaces_only_the_selected_fixture(self) -> None:
        devices = (
            multi_fixture.DeviceConfig("dk_a", "a", "/dev/a", Path("a.json")),
            multi_fixture.DeviceConfig("dk_b", "b", "/dev/b", Path("b.json")),
        )
        first = SimpleNamespace(config=devices[0])
        second = SimpleNamespace(config=devices[1])
        controller = multi_fixture.MultiFixtureController(devices)
        controller._owned.extend((first, second))
        with (
            patch.object(multi_fixture, "_stop_process"),
            patch.object(multi_fixture, "validate_device_mappings"),
            patch.object(controller, "_start_device") as start_device,
        ):
            controller.restart_device("dk_a")
        start_device.assert_called_once_with(devices[0])
        self.assertEqual(controller.owned, (second,))

    def test_restart_device_rejects_unknown_fixture(self) -> None:
        devices = (
            multi_fixture.DeviceConfig("dk_a", "a", "/dev/a", Path("a.json")),
            multi_fixture.DeviceConfig("dk_b", "b", "/dev/b", Path("b.json")),
        )
        controller = multi_fixture.MultiFixtureController(devices)
        with self.assertRaisesRegex(
            multi_fixture.MultiFixtureError, "unknown multi-DK device"
        ):
            controller.restart_device("dk_c")


if __name__ == "__main__":
    unittest.main()
