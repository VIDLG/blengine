from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, final
from unittest.mock import patch

from tests.peripherals.nrf52840 import provider
from tools.react_native import run_android_rf as runner


@final
class AndroidRfRuntimeTests(unittest.TestCase):
    tempdir: tempfile.TemporaryDirectory[str]
    work: Path
    pid_file: Path
    log_path: Path
    pid_file_patch: Any
    project_root_patch: Any

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.work = Path(self.tempdir.name)
        self.pid_file = self.work / "fixture.pid"
        self.log_path = self.work / "runtime.log"
        self.pid_file_patch = patch.object(provider, "PID_FILE", self.pid_file)
        self.pid_file_patch.start()
        self.project_root_patch = patch.object(runner, "PROJECT_ROOT", self.work)
        self.project_root_patch.start()
        self.addCleanup(self.project_root_patch.stop)
        self.addCleanup(self.pid_file_patch.stop)
        self.addCleanup(self.tempdir.cleanup)

    def _fixture_started(self) -> None:
        self.pid_file.write_text("1234 1234\n", encoding="utf-8")

    def test_fixture_start_failure_does_not_report_ready(self) -> None:
        output: list[str] = []
        with (
            patch.object(
                runner, "_run", side_effect=runner.AndroidRfError("start failed")
            ),
            patch.object(
                runner,
                "print",
                side_effect=lambda *args, **_: output.append(str(args[0])),
            ),
            self.assertRaisesRegex(runner.AndroidRfError, "start failed"),
        ):
            runner.run("device", "rf", "service", 1)
        self.assertIn("android-rf:fixture-start", output)
        self.assertNotIn("android-rf:fixture-ready", output)

    def test_existing_fixture_is_never_taken_over_or_stopped(self) -> None:
        self.pid_file.write_text("9999 9999\n", encoding="utf-8")
        with (
            patch.object(runner, "_run") as command,
            self.assertRaisesRegex(runner.AndroidRfError, "refusing to take over"),
        ):
            runner.run("device", "rf", "service", 1)
        command.assert_not_called()

    def test_cleanup_attempts_app_and_owned_fixture_and_preserves_primary_failure(
        self,
    ) -> None:
        commands: list[list[str]] = []
        force_stop_calls = 0

        def command(
            arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal force_stop_calls
            commands.append(arguments)
            if arguments[-1] == "direct-start":
                self._fixture_started()
                return subprocess.CompletedProcess(arguments, 0, "")
            if "stop-owned" in arguments:
                raise runner.AndroidRfError("fixture cleanup failed")
            if "force-stop" in arguments:
                force_stop_calls += 1
                if force_stop_calls == 2:
                    raise runner.AndroidRfError("app cleanup failed")
            return subprocess.CompletedProcess(arguments, 0, "")

        with (
            patch.object(runner, "_run", side_effect=command),
            patch.object(runner, "_grant_bluetooth_permissions"),
            patch.object(
                runner,
                "_wait_for_runtime",
                side_effect=runner.AndroidRfError("primary failure"),
            ),
            patch.object(runner, "print") as output,
            self.assertRaisesRegex(runner.AndroidRfError, "primary failure"),
        ):
            runner.run("device", "rf", "service", 1)

        self.assertTrue(any("force-stop" in arguments for arguments in commands))
        self.assertTrue(any("stop-owned" in arguments for arguments in commands))
        output.assert_any_call("android-rf:fixture-ready", flush=True)

    def test_permissions_follow_api_level_and_root_capability(self) -> None:
        for api, root, required in (
            (34, True, ("BLUETOOTH_SCAN", "BLUETOOTH_CONNECT")),
            (30, False, ("ACCESS_FINE_LOCATION",)),
        ):
            with self.subTest(api=api, root=root):
                commands: list[list[str]] = []

                def command(
                    arguments: list[str],
                    *,
                    expected_api: int = api,
                    command_log: list[list[str]] = commands,
                    **_: object,
                ) -> subprocess.CompletedProcess[str]:
                    command_log.append(arguments)
                    return subprocess.CompletedProcess(
                        arguments, 0, f"{expected_api}\n"
                    )

                with (
                    patch.object(runner, "_run", side_effect=command),
                    patch.object(runner, "adb_executable", return_value=Path("/adb")),
                    patch.object(runner, "has_root", return_value=root),
                ):
                    runner._grant_bluetooth_permissions("device")

                grants = [" ".join(arguments) for arguments in commands[1:]]
                self.assertEqual(len(grants), len(required))
                for permission in required:
                    self.assertTrue(any(permission in grant for grant in grants))
                self.assertFalse(
                    any(
                        "ACCESS_FINE_LOCATION" in grant
                        for grant in grants
                        if "BLUETOOTH" in grant
                    )
                )

    def test_run_checks_checker_after_runtime_marker(self) -> None:
        commands: list[list[str]] = []

        def command(
            arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            commands.append(arguments)
            if arguments[-1] == "direct-start":
                self._fixture_started()
            return subprocess.CompletedProcess(arguments, 0, "")

        with (
            patch.object(runner, "_run", side_effect=command),
            patch.object(runner, "_grant_bluetooth_permissions"),
            patch.object(runner, "_wait_for_runtime"),
        ):
            result = runner.run("device", "rf", "service", 1)

        self.assertEqual(
            result,
            self.work
            / "tests/consumers/react-native-internal/android/build/rf-runtime.log",
        )
        self.assertTrue(
            any(
                "tools.react_native.check_runtime_log" in " ".join(command)
                for command in commands
            )
        )

    def test_runtime_failure_markers_are_immediate(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        for marker in ("rf:failed:boom", "rf:failure:boom"):
            with self.subTest(marker=marker):
                with (
                    patch.object(
                        runner,
                        "_logcat",
                        return_value=f"BLEHUB_NITRO_RUNTIME {marker}",
                    ),
                    self.assertRaisesRegex(runner.AndroidRfError, marker.split(":")[1]),
                ):
                    runner._wait_for_runtime("device", "rf", self.log_path, 20)
                self.assertIn(
                    marker,
                    self.log_path.read_text(encoding="utf-8"),
                )

    def test_runtime_marker_success_writes_log(self) -> None:
        content = (
            "BLEHUB_NITRO_RUNTIME rf:listener:registered-before-initialize\n"
            "BLEHUB_NITRO_RUNTIME rf:step:initialize:passed:commandId=9001\n"
            "BLEHUB_NITRO_RUNTIME rf:passed:"
        )
        with patch.object(runner, "_logcat", return_value=content):
            runner._wait_for_runtime("device", "rf", self.log_path, 20)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), content)

    def test_runtime_exit_after_launch_is_fail_fast(self) -> None:
        content = (
            "BLEHUB_NITRO_RUNTIME rf:listener:registered-before-initialize\n"
            "BLEHUB_NITRO_RUNTIME rf:step:initialize:passed:commandId=9001\n"
        )
        with (
            patch.object(runner, "_logcat", return_value=content),
            patch.object(runner, "runtime_alive", return_value=False),
            self.assertRaisesRegex(runner.AndroidRfError, "host exited"),
        ):
            runner._wait_for_runtime("device", "rf", self.log_path, 20)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), content)

    def test_runtime_alive_uses_pidof(self) -> None:
        with patch.object(
            runner,
            "_run",
            return_value=subprocess.CompletedProcess(["adb"], 0, "1234\n"),
        ) as command:
            self.assertTrue(runner.runtime_alive("device"))
        self.assertIn("pidof", command.call_args.args[0])

    def test_runtime_marker_timeout_is_bounded(self) -> None:
        with (
            patch.object(runner, "_logcat", return_value="no runtime marker"),
            self.assertRaisesRegex(runner.AndroidRfError, "marker timed out"),
        ):
            runner._wait_for_runtime("device", "rf", self.log_path, 0.01)
        self.assertTrue(self.log_path.is_file())


if __name__ == "__main__":
    unittest.main()
