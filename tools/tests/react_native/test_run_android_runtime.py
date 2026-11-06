from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import final, override
from unittest.mock import Mock, patch

from tools.react_native import run_android_runtime as runner


@final
class AndroidRuntimeControllerTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.log = self.work / "runtime.log"
        self.content = ""
        self.order: list[str] = []
        self.process = Mock(spec=subprocess.Popen)
        self.process.poll.return_value = None
        project_root = patch.object(runner, "PROJECT_ROOT", self.work)
        project_root.start()
        self.addCleanup(project_root.stop)

        @contextmanager
        def capture(serial, path):
            self.assertEqual(serial, "device")
            self.assertEqual(path, self.log)
            self.order.append("capture-start")
            path.write_text(self.content, encoding="utf-8")
            try:
                yield self.process
            finally:
                self.order.append("capture-stop")

        capture_patch = patch.object(runner, "_capture_logcat", side_effect=capture)
        capture_patch.start()
        self.addCleanup(capture_patch.stop)

    def test_reload_recreates_context_runs_checker_and_cleans_up(self) -> None:
        commands: list[list[str]] = []
        self.content = "BLEHUB_NITRO_RUNTIME reload:ready\n"

        def command(arguments: list[str], **_: object):
            commands.append(arguments)
            if "blehub.triggerReload" in arguments:
                with self.log.open("a", encoding="utf-8") as output:
                    output.write(
                        "BLEHUB_NITRO_RUNTIME reload:cleanup-started\n"
                        "BLEHUB_NITRO_RUNTIME reload:ready\n"
                    )
            return subprocess.CompletedProcess(arguments, 0, "")

        with (
            patch.object(runner, "_run", side_effect=command),
            patch.object(runner.time, "sleep"),
        ):
            result = runner.run("device", "reload", self.log, timeout_seconds=5)

        self.assertEqual(result, self.log)
        self.assertTrue(any("blehub.triggerReload" in command for command in commands))
        self.assertTrue(any("--reload" in command for command in commands))
        self.assertEqual(sum("force-stop" in command for command in commands), 1)
        self.assertEqual(self.log.read_text().count("reload:ready"), 2)
        self.assertEqual(self.order, ["capture-start", "capture-stop"])

    def test_runtime_failure_is_detected_before_completion(self) -> None:
        self.content = "BLEHUB_NITRO_RUNTIME stress:failed:late callback\n"
        with (
            patch.object(runner, "_run") as command,
            self.assertRaisesRegex(runner.AndroidRuntimeError, "stress:failed"),
        ):
            runner.run("device", "stress", self.log, timeout_seconds=5)
        self.assertIn("stress:failed", self.log.read_text(encoding="utf-8"))
        self.assertEqual(
            sum("force-stop" in call.args[0] for call in command.call_args_list), 2
        )
        self.assertEqual(self.order, ["capture-start", "capture-stop"])

    def test_capture_precedes_launch_and_is_frozen_before_checker(self) -> None:
        commands: list[list[str]] = []
        self.content = (
            "BLEHUB_NITRO_RUNTIME stress:start:8\n"
            "BLEHUB_NITRO_RUNTIME stress:complete:passed\n"
        )

        def command(arguments: list[str], **_: object):
            commands.append(arguments)
            if "blehub.runtimeMode" in arguments:
                self.order.append("launch")
            if "tools.react_native.check_runtime_log" in arguments:
                self.order.append("checker")
            return subprocess.CompletedProcess(arguments, 0, "")

        with patch.object(runner, "_run", side_effect=command):
            result = runner.run("device", "stress", self.log, timeout_seconds=5)

        self.assertEqual(result, self.log)
        self.assertEqual(
            self.order, ["capture-start", "launch", "capture-stop", "checker"]
        )
        self.assertEqual(self.log.read_text(), self.content)
        self.assertTrue(any("--stress" in command for command in commands))
        self.assertEqual(sum("force-stop" in command for command in commands), 1)

    def test_timeout_is_bounded_and_preserves_log(self) -> None:
        self.content = "no runtime marker\n"
        with (
            patch.object(runner, "_run"),
            patch.object(runner.time, "sleep"),
            patch.object(runner, "ProgressReporter"),
            self.assertRaisesRegex(runner.AndroidRuntimeError, "marker timed out"),
        ):
            runner.run("device", "basic", self.log, timeout_seconds=0.01)
        self.assertEqual(self.log.read_text(), self.content)
        self.assertEqual(self.order, ["capture-start", "capture-stop"])


class AndroidRuntimeCaptureTests(unittest.TestCase):
    def test_stream_retains_early_markers_and_reaps_only_owned_child(self) -> None:
        script = (
            "import time; print('early',flush=True); time.sleep(0.1); "
            "print('complete',flush=True); time.sleep(10)"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            with (
                patch.object(
                    runner, "_adb", return_value=[sys.executable, "-u", "-c", script]
                ) as adb,
                runner._capture_logcat("synthetic-device", path) as process,
            ):
                deadline = time.monotonic() + 3
                content = ""
                while "complete" not in content and time.monotonic() < deadline:
                    content = runner._logcat(path, process)
                    time.sleep(0.01)
                self.assertEqual(content, "early\ncomplete\n")
            self.assertIsNotNone(process.poll())
            self.assertEqual(path.read_text(), "early\ncomplete\n")
            adb.assert_called_once_with(
                "synthetic-device",
                "logcat",
                "-v",
                "brief",
                "ReactNativeJS:V",
                "AndroidRuntime:E",
                "*:S",
            )

    def test_capture_failure_reaps_child_and_preserves_primary(self) -> None:
        failure = RuntimeError("synthetic runtime failure")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            with (
                patch.object(
                    runner,
                    "_adb",
                    return_value=[sys.executable, "-c", "import time; time.sleep(10)"],
                ),
                self.assertRaises(RuntimeError) as raised,
                runner._capture_logcat("synthetic-device", path) as process,
            ):
                raise failure
            self.assertIs(raised.exception, failure)
            self.assertIsNotNone(process.poll())

    def test_partial_utf8_is_deferred_and_exit_or_overflow_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            path.write_bytes(b"complete\npartial\xe4")
            process = Mock(spec=subprocess.Popen)
            process.poll.return_value = None
            self.assertEqual(runner._logcat(path, process), "complete\n")
            process.poll.return_value = 0
            with self.assertRaisesRegex(runner.AndroidRuntimeError, "logcat exited"):
                runner._logcat(path, process)
            process.poll.return_value = None
            with (
                patch.object(runner, "MAX_BYTES", 8),
                self.assertRaisesRegex(runner.AndroidRuntimeError, "exceeds"),
            ):
                runner._logcat(path, process)


if __name__ == "__main__":
    unittest.main()
