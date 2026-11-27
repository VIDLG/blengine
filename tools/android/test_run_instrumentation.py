"""Regression tests for the Android instrumentation runner."""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.android import adb
from tools.android import run_instrumentation as runner

SUCCESS_OUTPUT = """\
INSTRUMENTATION_STATUS: numtests=2
INSTRUMENTATION_STATUS_CODE: 1
INSTRUMENTATION_STATUS_CODE: 0
INSTRUMENTATION_RESULT: numtests=2
INSTRUMENTATION_RESULT: stream=OK
INSTRUMENTATION_CODE: -1
"""


class InstrumentationParserTests(unittest.TestCase):
    def test_android_result_ok_requires_positive_test_count_and_completion(
        self,
    ) -> None:
        runner.parse_instrumentation_output(SUCCESS_OUTPUT)

    def test_zero_tests_are_rejected(self) -> None:
        output = SUCCESS_OUTPUT.replace("numtests=2", "numtests=0")
        with self.assertRaisesRegex(
            runner.AndroidInstrumentationError, "invalid test count"
        ):
            runner.parse_instrumentation_output(output)

    def test_negative_per_test_statuses_are_failures(self) -> None:
        for status in (-1, -2, -3, -99):
            with self.subTest(status=status):
                output = SUCCESS_OUTPUT.replace(
                    "INSTRUMENTATION_STATUS_CODE: 1",
                    f"INSTRUMENTATION_STATUS_CODE: {status}",
                )
                with self.assertRaisesRegex(
                    runner.AndroidInstrumentationError,
                    "per-test instrumentation status",
                ):
                    runner.parse_instrumentation_output(output)

    def test_failures_and_missing_markers_are_rejected(self) -> None:
        cases = (
            (
                SUCCESS_OUTPUT.replace(
                    "INSTRUMENTATION_CODE: -1", "FAILURES!!!\nINSTRUMENTATION_CODE: -1"
                ),
                "failure marker",
            ),
            (
                SUCCESS_OUTPUT.replace(
                    "INSTRUMENTATION_CODE: -1", "INSTRUMENTATION_CODE: -2"
                ),
                "completion code",
            ),
            (
                SUCCESS_OUTPUT.replace("INSTRUMENTATION_CODE: -1", ""),
                "completion marker",
            ),
            (
                SUCCESS_OUTPUT.replace(
                    "INSTRUMENTATION_RESULT: numtests=2\n", ""
                ).replace("INSTRUMENTATION_RESULT: stream=OK\n", ""),
                "result marker",
            ),
            (SUCCESS_OUTPUT.replace("numtests=2", "numtests=0"), "invalid test count"),
        )
        for output, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(runner.AndroidInstrumentationError, message),
            ):
                runner.parse_instrumentation_output(output)

    def test_crash_and_truncated_output_are_rejected(self) -> None:
        with self.assertRaisesRegex(runner.AndroidInstrumentationError, "crash"):
            runner.parse_instrumentation_output("FATAL EXCEPTION: main\n", returncode=1)
        with self.assertRaisesRegex(
            runner.AndroidInstrumentationError, "completion marker"
        ):
            runner.parse_instrumentation_output(
                "INSTRUMENTATION_STATUS_CODE: 1\nINSTRUMENTATION_RESULT: numtests=1\n"
            )


class InstrumentationProcessTests(unittest.TestCase):
    def _run_child(self, script: str, *, timeout: int = 10) -> tuple[io.StringIO, Mock]:
        log = io.StringIO()
        cleanup = Mock()
        runner._run_instrumentation(
            [sys.executable, "-u", "-c", script],
            log=log,
            timeout=timeout,
            progress_label="test-component",
            cleanup=cleanup,
        )
        return log, cleanup

    def test_zero_exit_junit_failure_fails_fast_and_cleans_up(self) -> None:
        script = (
            "print('INSTRUMENTATION_STATUS: numtests=1', flush=True); "
            "print('INSTRUMENTATION_STATUS_CODE: -3', flush=True); "
            "print('INSTRUMENTATION_CODE: -1', flush=True)"
        )
        log = io.StringIO()
        cleanup = Mock()
        with self.assertRaisesRegex(runner.AndroidInstrumentationError, "status -3"):
            runner._run_instrumentation(
                [sys.executable, "-u", "-c", script],
                log=log,
                timeout=10,
                progress_label="test-component",
                cleanup=cleanup,
            )
        cleanup.assert_called_once_with()
        self.assertIn("INSTRUMENTATION_STATUS_CODE: -3", log.getvalue())

    def test_streaming_success_accepts_android_result_ok(self) -> None:
        script = (
            "import sys; sys.stdout.write("
            + repr(SUCCESS_OUTPUT)
            + "); sys.stdout.flush()"
        )
        log, cleanup = self._run_child(script)
        cleanup.assert_called_once_with()
        self.assertIn("INSTRUMENTATION_CODE: -1", log.getvalue())

    def test_timeout_preserves_original_failure_and_cleans_up(self) -> None:
        log = io.StringIO()
        cleanup = Mock()
        with self.assertRaisesRegex(runner.AndroidInstrumentationError, "timed out"):
            runner._run_instrumentation(
                [sys.executable, "-u", "-c", "import time; time.sleep(60)"],
                log=log,
                timeout=1,
                progress_label="test-component",
                cleanup=cleanup,
            )
        cleanup.assert_called_once_with()


class InstallTests(unittest.TestCase):
    def test_guarded_root_install_shares_deadline_and_exact_upload_cleanup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "tests.apk"
            apk.write_bytes(b"fixture")
            clock = 100.0
            events = []
            commands = []

            def owner():
                nonlocal clock
                events.append("guard")
                clock += 0.1

            def command(argv, **kwargs):
                nonlocal clock
                events.append("command")
                commands.append(argv)
                self.assertEqual(kwargs["cwd"], Path(directory))
                self.assertEqual(set(kwargs), {"cwd", "check", "timeout"})
                self.assertFalse(kwargs["check"])
                self.assertGreater(kwargs["timeout"], 0)
                self.assertLessEqual(kwargs["timeout"], 105.0 - clock)
                clock += 0.2
                return subprocess.CompletedProcess(
                    argv, 0, "Success\n" if "su" in argv else ""
                )

            class SlowLog(io.StringIO):
                def write(self, text):
                    nonlocal clock
                    clock += 0.05
                    return super().write(text)

            with (
                patch.object(runner.time, "monotonic", side_effect=lambda: clock),
                patch.object(adb, "run_command", side_effect=command),
            ):
                runner.install_apk(
                    "synthetic",
                    apk,
                    test_only=True,
                    use_root=True,
                    log=SlowLog(),
                    deadline=105.0,
                    require_owner=owner,
                    cwd=Path(directory),
                )
            self.assertEqual(events, ["guard", "command", "guard"] * 5)
            self.assertEqual(commands[2][3], "push")
            self.assertEqual(commands[3][3:6], ["shell", "su", "-c"])
            self.assertEqual(commands[3][-1], "pm install -r -t " + commands[2][-1])
            self.assertEqual(commands[4][3:], ["shell", "rm", "-f", commands[2][-1]])

    def test_no_cleanup_for_upload_never_attempted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "tests.apk"
            apk.write_bytes(b"fixture")
            with patch.object(adb, "run_command") as run:
                with self.assertRaisesRegex(RuntimeError, "owner lost"):
                    runner.install_apk(
                        "synthetic",
                        apk,
                        test_only=True,
                        log=io.StringIO(),
                        require_owner=Mock(side_effect=RuntimeError("owner lost")),
                    )
                run.assert_not_called()

    def test_install_timeout_partial_output_and_cleanup_failure_both_survive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "tests.apk"
            apk.write_bytes(b"fixture")
            log = io.StringIO()
            responses = [
                subprocess.CompletedProcess([], 0, ""),
                subprocess.TimeoutExpired("install", 1, output=b"partial install"),
                subprocess.CompletedProcess([], 9, "cleanup denied"),
            ]
            with (
                patch.object(adb, "run_command", side_effect=responses) as run,
                self.assertRaises(subprocess.TimeoutExpired) as caught,
            ):
                runner.install_apk("synthetic", apk, test_only=True, log=log)
            self.assertEqual(run.call_count, 3)
            self.assertIn("cleanup denied", " ".join(caught.exception.__notes__))
            self.assertIn("partial install", log.getvalue())

    def test_logging_failure_does_not_replace_install_timeout(self) -> None:
        class BrokenLog(io.StringIO):
            def write(self, text):
                if "partial install" in text or "temporary APK cleanup failed" in text:
                    raise OSError("log storage failed")
                return super().write(text)

        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "tests.apk"
            apk.write_bytes(b"fixture")
            with (
                patch.object(
                    adb,
                    "run_command",
                    side_effect=[
                        subprocess.CompletedProcess([], 0, ""),
                        subprocess.TimeoutExpired(
                            "install", 1, output=b"partial install"
                        ),
                        subprocess.CompletedProcess([], 9, "cleanup denied"),
                    ],
                ),
                self.assertRaises(subprocess.TimeoutExpired) as caught,
            ):
                runner.install_apk("synthetic", apk, test_only=True, log=BrokenLog())
            self.assertIn("log storage failed", " ".join(caught.exception.__notes__))
            self.assertIn("cleanup denied", " ".join(caught.exception.__notes__))

    def test_failed_push_cleans_only_attempted_upload_and_never_installs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "tests.apk"
            apk.write_bytes(b"fixture")
            with (
                patch.object(
                    adb,
                    "run_command",
                    side_effect=[
                        subprocess.CompletedProcess([], 1, "push failed"),
                        subprocess.CompletedProcess([], 0, ""),
                    ],
                ) as run,
                self.assertRaisesRegex(runner.AndroidInstrumentationError, "push"),
            ):
                runner.install_apk("synthetic", apk, test_only=True, log=io.StringIO())
            self.assertEqual(run.call_count, 2)
            self.assertEqual(
                run.call_args_list[0].args[0][-1], run.call_args_list[1].args[0][-1]
            )
            self.assertEqual(run.call_args_list[1].args[0][3:6], ["shell", "rm", "-f"])

    def test_install_requires_success_and_removes_unique_remote_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "tests.apk"
            apk.write_bytes(b"fixture")
            responses = [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess(
                    [], 0, "Failure [INSTALL_FAILED_TEST_ONLY]", ""
                ),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
            log = io.StringIO()
            with (
                patch.object(adb, "run_command", side_effect=responses) as run,
                self.assertRaisesRegex(runner.AndroidInstrumentationError, "Success"),
            ):
                runner.install_apk(
                    "serial",
                    apk,
                    test_only=True,
                    log=log,
                )
            self.assertEqual(run.call_count, 3)
            push_command = run.call_args_list[0].args[0]
            install_command = run.call_args_list[1].args[0]
            cleanup_command = run.call_args_list[2].args[0]
            self.assertEqual(push_command[3], "push")
            self.assertEqual(install_command[3:7], ["shell", "pm", "install", "-r"])
            self.assertIn("-t", install_command)
            self.assertEqual(cleanup_command[3:6], ["shell", "rm", "-f"])
            self.assertEqual(push_command[-1], install_command[-1])
            self.assertEqual(push_command[-1], cleanup_command[-1])


class ComponentTests(unittest.TestCase):
    def test_force_stop_package_is_exact_component_prefix(self) -> None:
        self.assertEqual(
            runner._package_from_component(
                "dev.blehub.tests/androidx.test.runner.AndroidJUnitRunner"
            ),
            "dev.blehub.tests",
        )
        for component in (
            "dev.blehub.tests",
            "/Runner",
            "dev.blehub.tests/",
            "bad package/Runner",
        ):
            with (
                self.subTest(component=component),
                self.assertRaises(runner.AndroidInstrumentationError),
            ):
                runner._package_from_component(component)


if __name__ == "__main__":
    unittest.main()
