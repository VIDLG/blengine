from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import final, override
from unittest.mock import patch

from tools.react_native import run_android_runtime as runner


@final
class AndroidRuntimeControllerTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.tempdir = tempfile.TemporaryDirectory()
        self.work = Path(self.tempdir.name)
        self.log = self.work / "runtime.log"
        self.project_root = patch.object(runner, "PROJECT_ROOT", self.work)

    @override
    def setUp(self) -> None:
        _ = self.project_root.start()
        self.addCleanup(self.project_root.stop)
        self.addCleanup(self.tempdir.cleanup)

    def test_reload_recreates_context_runs_checker_and_cleans_up(self) -> None:
        commands: list[list[str]] = []
        first_generation = "BLEHUB_NITRO_RUNTIME reload:ready\n"
        second_generation = (
            "BLEHUB_NITRO_RUNTIME reload:cleanup-started\n"
            "BLEHUB_NITRO_RUNTIME reload:ready\n"
            "BLEHUB_NITRO_RUNTIME reload:ready\n"
        )

        def command(
            arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            commands.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "")

        with (
            patch.object(runner, "_run", side_effect=command),
            patch.object(
                runner,
                "_logcat",
                side_effect=[first_generation, second_generation],
            ),
        ):
            result = runner.run("device", "reload", self.log, timeout_seconds=5)

        self.assertEqual(result, self.log)
        self.assertTrue(any("blehub.triggerReload" in command for command in commands))
        self.assertTrue(any("--reload" in command for command in commands))
        self.assertTrue(any("force-stop" in command for command in commands))

    def test_runtime_failure_is_detected_before_completion(self) -> None:
        with (
            patch.object(runner, "_run") as command,
            patch.object(
                runner,
                "_logcat",
                return_value="BLEHUB_NITRO_RUNTIME stress:failed:late callback",
            ),
            self.assertRaisesRegex(runner.AndroidRuntimeError, "stress:failed"),
        ):
            _ = runner.run("device", "stress", self.log, timeout_seconds=5)
        self.assertTrue(self.log.is_file())
        self.assertIn("stress:failed", self.log.read_text(encoding="utf-8"))
        self.assertTrue(
            any("force-stop" in call.args[0] for call in command.call_args_list)
        )

    def test_stress_completion_runs_checker_and_cleans_up(self) -> None:
        commands: list[list[str]] = []
        content = (
            "BLEHUB_NITRO_RUNTIME stress:start:8\n"
            "BLEHUB_NITRO_RUNTIME stress:cycle:0:passed\n"
            "BLEHUB_NITRO_RUNTIME stress:cycle:7:passed\n"
            "BLEHUB_NITRO_RUNTIME stress:recreate-close:passed\n"
            "BLEHUB_NITRO_RUNTIME stress:late-callback:passed\n"
            "BLEHUB_NITRO_RUNTIME stress:post-close-dispatch:passed\n"
            "BLEHUB_NITRO_RUNTIME stress:complete:passed\n"
        )

        def command(
            arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            commands.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "")

        with (
            patch.object(runner, "_run", side_effect=command),
            patch.object(runner, "_logcat", return_value=content),
        ):
            result = runner.run("device", "stress", self.log, timeout_seconds=5)

        self.assertEqual(result, self.log)
        self.assertTrue(
            any("check_runtime_log" in " ".join(command) for command in commands)
        )
        self.assertTrue(any("--stress" in command for command in commands))
        self.assertTrue(any("force-stop" in command for command in commands))

    def test_timeout_is_bounded_and_preserves_log(self) -> None:
        with (
            patch.object(runner, "_run"),
            patch.object(runner, "_logcat", return_value="no runtime marker"),
            patch("tools.react_native.run_android_runtime.time.sleep"),
            patch.object(runner, "HEARTBEAT_SECONDS", 0),
            self.assertRaisesRegex(runner.AndroidRuntimeError, "marker timed out"),
        ):
            _ = runner.run("device", "basic", self.log, timeout_seconds=0.01)
        self.assertEqual(self.log.read_text(encoding="utf-8"), "no runtime marker")


if __name__ == "__main__":
    _ = unittest.main()
