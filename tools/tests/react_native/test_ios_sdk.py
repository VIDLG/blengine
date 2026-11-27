"""Shared iOS SDK command API boundaries; no device or subprocess execution."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.react_native import ios_sdk


class IosSdkCommandTests(unittest.TestCase):
    def test_xctest_builder_has_one_explicit_test_and_no_repeat_or_quiet_by_default(
        self,
    ) -> None:
        command = ios_sdk.xcodebuild_test_without_building(
            xctestrun=Path("candidate.xctestrun"),
            device="00008110-000C61160C39401E",
            result=Path("result.xcresult"),
            only_testing="Target/Target/testOne",
        )
        self.assertNotIn("-quiet", command)
        self.assertEqual(
            command[-2:],
            ["-only-testing:Target/Target/testOne", "test-without-building"],
        )
        self.assertNotIn("-test-iterations", command)
        self.assertNotIn("-retry-tests-on-failure", command)

    def test_coredevice_builders_keep_device_receipts_explicit(self) -> None:
        device = "00008110-000C61160C39401E"
        self.assertEqual(
            ios_sdk.devicectl_lock_state(device)[-2:], ["--json-output", "-"]
        )
        pair = ios_sdk.devicectl_pair(device, Path("pair.json"))
        self.assertIn("manage", pair)
        self.assertEqual(pair[-2:], ["--json-output", "pair.json"])
        install = ios_sdk.devicectl_install_app(
            device, Path("Host.app"), Path("install.json")
        )
        self.assertEqual(
            install[
                install.index("--json-output") : install.index("--json-output") + 2
            ],
            ["--json-output", "install.json"],
        )
        reboot = ios_sdk.devicectl_reboot(device, Path("reboot.json"))
        self.assertIn("--wait-for-device", reboot)
        self.assertEqual(reboot[-2:], ["--json-output", "reboot.json"])
        screenshot = ios_sdk.devicectl_screenshot(device, Path("screen.png"))
        self.assertEqual(screenshot[-3:], ["--timeout", "30", "--quiet"])
        self.assertIn("screen.png", screenshot)
        self.assertEqual(
            ios_sdk.devicectl_device_details(device)[-2:], ["--json-output", "-"]
        )
        self.assertEqual(
            ios_sdk.devicectl_device_apps(device)[-2:], ["--json-output", "-"]
        )
        notification = ios_sdk.devicectl_post_notification(
            device, "BleHubControl", Path("notification.json")
        )
        self.assertEqual(notification[-1], "notification.json")
        self.assertIn("BleHubControl", notification)
        terminate = ios_sdk.devicectl_terminate_process(
            device, 1234, Path("terminate.json")
        )
        self.assertEqual(terminate[-1], "--quiet")
        self.assertIn("1234", terminate)
        processes = ios_sdk.devicectl_processes(device, Path("processes.json"))
        self.assertIn("devicectl", processes)
        self.assertEqual(processes[-1], "processes.json")
        launch = ios_sdk.devicectl_launch_payload(
            device, "bundle", "scheme://ready", Path("payload.json")
        )
        self.assertEqual(launch[-1], "bundle")
        self.assertIn("scheme://ready", launch)
        copy_to = ios_sdk.devicectl_copy_to(
            device,
            Path("control.json"),
            "Documents/bleng-rf-run/control.json",
            "bundle",
            Path("copy-to.json"),
            timeout=15,
        )
        self.assertEqual(copy_to[-1], "copy-to.json")
        self.assertIn("appDataContainer", copy_to)
        copy = ios_sdk.devicectl_copy_from(
            device,
            "Library/Caches/run.json",
            "bundle",
            Path("journal.json"),
            Path("copy.json"),
            timeout=15,
        )
        self.assertEqual(copy[-1], "copy.json")
        self.assertIn("appDataContainer", copy)

    def test_run_command_bounds_execution_and_reports_failure(self) -> None:
        completed = subprocess.CompletedProcess(["xcrun"], 0, "output")
        with patch.object(ios_sdk.subprocess, "run", return_value=completed) as run:
            result = ios_sdk.run_command(
                ["xcrun", "devicectl"], cwd=Path("/work"), timeout=3, check=True
            )
        self.assertIs(result, completed)
        run.assert_called_once_with(
            ["xcrun", "devicectl"],
            cwd=Path("/work"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=3,
        )
        with (
            patch.object(
                ios_sdk.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 2, "failed"),
            ),
            self.assertRaisesRegex(ios_sdk.IosSdkError, "exit=2"),
        ):
            ios_sdk.run_command(["xcrun"], timeout=1, check=True)

    def test_run_logged_creates_one_complete_log_and_closes_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "command.log"
            result = ios_sdk.run_logged(
                ["sh", "-c", "printf command-ok"],
                cwd=Path(directory),
                log=log,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(log.read_text(), "command-ok")


if __name__ == "__main__":
    unittest.main()
