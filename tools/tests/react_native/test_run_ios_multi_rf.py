from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tools.react_native import run_ios_multi_rf as runner


class IosMultiRfDeviceTests(unittest.TestCase):
    def test_cycles_stop_on_first_failure_and_keep_suffixes(self) -> None:
        with (
            patch.object(
                runner,
                "run",
                side_effect=[Path("cycle-1.log"), runner.IosMultiRfError("boom")],
            ) as run,
            self.assertRaisesRegex(runner.IosMultiRfError, "boom"),
        ):
            runner.run_cycles("device", 3)
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(run.call_args_list[0].args[1])
        self.assertEqual(run.call_args_list[0].kwargs["runtime_mode"], "rf-multi")
        self.assertEqual(run.call_args_list[0].kwargs["soak_cycles"], 1)
        self.assertEqual(run.call_args_list[0].kwargs["log_suffix"], "-cycle-1")
        self.assertEqual(run.call_args_list[1].kwargs["log_suffix"], "-cycle-2")

    def test_default_soak_budget_covers_all_requested_cycles(self) -> None:
        devices = [
            SimpleNamespace(name="dk-a", profile="profile-a"),
            SimpleNamespace(name="dk-b", profile="profile-b"),
        ]
        profiles = {
            "profile-a": SimpleNamespace(
                service_uuid="00000000-0000-0000-0000-00000000000a"
            ),
            "profile-b": SimpleNamespace(
                service_uuid="00000000-0000-0000-0000-00000000000b"
            ),
        }
        controller = MagicMock()
        pid_file = MagicMock()
        pid_file.exists.return_value = False
        subprocess_calls: list[tuple[list[str], float]] = []

        def record_run(command: list[str], *, timeout: float) -> None:
            subprocess_calls.append((command, timeout))

        with TemporaryDirectory() as temporary_directory:
            build_root = Path(temporary_directory)
            app = build_root / "BleHubNitroHost.app"
            app.mkdir()
            with (
                patch.object(runner, "HOST_BUILD", build_root),
                patch.object(runner, "APP", app),
                patch.object(runner.provider, "PID_FILE", pid_file),
                patch.object(runner, "_device_ready"),
                patch.object(
                    runner.multi_fixture, "load_devices", return_value=devices
                ),
                patch.object(
                    runner,
                    "load_profile",
                    side_effect=lambda profile: profiles[profile],
                ),
                patch.object(
                    runner.multi_fixture,
                    "MultiFixtureController",
                    return_value=controller,
                ),
                patch.object(runner.shutil, "which", return_value="/usr/bin/xcrun"),
                patch.object(runner, "_run", side_effect=record_run),
            ):
                log_path = runner.run(
                    "device",
                    runtime_mode="rf-multi-soak",
                    soak_cycles=20,
                )

        self.assertEqual(log_path, build_root / "device-rf-multi-soak.log")
        self.assertEqual(len(subprocess_calls), 3)
        runtime_command, runtime_timeout = subprocess_calls[1]
        self.assertEqual(runtime_timeout, 645.0)
        self.assertIn("600.0", runtime_command)

    def test_cycles_are_bounded(self) -> None:
        for cycles in (0, 21):
            with (
                self.subTest(cycles=cycles),
                self.assertRaisesRegex(runner.IosMultiRfError, "between 1 and 20"),
            ):
                runner.run_cycles("device", cycles)


if __name__ == "__main__":
    unittest.main()
