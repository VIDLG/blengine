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
                runner.android_sdk,
                "run_command",
                side_effect=runner.android_sdk.AndroidSdkError("start failed"),
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
            patch.object(runner.android_sdk, "run_command") as command,
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
            patch.object(runner.android_sdk, "run_command", side_effect=command),
            patch.object(runner.android_sdk, "grant_bluetooth_permissions"),
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
            patch.object(runner.android_sdk, "run_command", side_effect=command),
            patch.object(runner.android_sdk, "grant_bluetooth_permissions"),
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
                        runner.android_sdk,
                        "logcat",
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
        with patch.object(runner.android_sdk, "logcat", return_value=content):
            runner._wait_for_runtime("device", "rf", self.log_path, 20)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), content)

    def test_runtime_exit_after_launch_is_fail_fast(self) -> None:
        content = (
            "BLEHUB_NITRO_RUNTIME rf:listener:registered-before-initialize\n"
            "BLEHUB_NITRO_RUNTIME rf:step:initialize:passed:commandId=9001\n"
        )
        with (
            patch.object(runner.android_sdk, "logcat", return_value=content),
            patch.object(runner.android_sdk, "runtime_alive", return_value=False),
            self.assertRaisesRegex(runner.AndroidRfError, "host exited"),
        ):
            runner._wait_for_runtime("device", "rf", self.log_path, 20)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), content)

    def test_runtime_alive_uses_pidof(self) -> None:
        with patch.object(
            runner.android_sdk,
            "run_command",
            return_value=subprocess.CompletedProcess(["adb"], 0, "1234\n"),
        ) as command:
            self.assertTrue(runner.android_sdk.runtime_alive("device"))
        self.assertIn("pidof", command.call_args.args[0])

    def test_runtime_marker_timeout_is_bounded(self) -> None:
        with (
            patch.object(
                runner.android_sdk, "logcat", return_value="no runtime marker"
            ),
            self.assertRaisesRegex(runner.AndroidRfError, "marker timed out"),
        ):
            runner._wait_for_runtime("device", "rf", self.log_path, 0.01)
        self.assertTrue(self.log_path.is_file())

    def test_recovery_accumulates_runtime_markers_without_system_log_noise(
        self,
    ) -> None:
        coordination = self.work / "recovery-coordination"
        for cycle in range(3):
            directory = coordination / f"cycle-{cycle}"
            directory.mkdir(parents=True)
            for marker in (
                "fixture-ready",
                "value-applied",
                "fault-triggered",
                "recovery-ready",
            ):
                (directory / marker).write_text("ready\n", encoding="ascii")
        lines = [
            "I/SystemNoise: this line may evict old logcat entries\n",
            "I/ReactNativeJS: BLEHUB_NITRO_RUNTIME rf-recovery:listener:registered-before-initialize\n",
            "I/ReactNativeJS: BLEHUB_NITRO_RUNTIME rf-recovery:initialize:passed:commandId=10000\n",
        ]
        for cycle in range(3):
            lines.extend(
                (
                    f"I/ReactNativeJS: BLEHUB_NITRO_RUNTIME rf-recovery:cycle:{cycle}:notification-enable:passed:\n",
                    f"I/ReactNativeJS: BLEHUB_NITRO_RUNTIME rf-recovery:cycle:{cycle}:notification-value:passed:\n",
                    f"I/ReactNativeJS: BLEHUB_NITRO_RUNTIME rf-recovery:cycle:{cycle}:passive-disconnect:passed:\n",
                    f"I/ReactNativeJS: BLEHUB_NITRO_RUNTIME rf-recovery:cycle:{cycle}:cycle:passed:\n",
                )
            )
        lines.append(
            "I/ReactNativeJS: BLEHUB_NITRO_RUNTIME rf-recovery:close:passed:\n"
        )
        with (
            patch.object(runner.android_sdk, "logcat", return_value="".join(lines)),
            patch.object(runner.android_sdk, "runtime_alive", return_value=True),
        ):
            runner._wait_for_recovery(
                "device", self.log_path, coordination, "rf-recovery", 3, 10
            )
        captured = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn("SystemNoise", captured)
        self.assertEqual(captured.count("BLEHUB_NITRO_RUNTIME"), 15)


if __name__ == "__main__":
    unittest.main()
