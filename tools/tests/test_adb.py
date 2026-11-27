"""Contract for the one adb boundary: argv shape, bounded run, typed failure."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.android import adb


class AdbCommandTests(unittest.TestCase):
    def test_command_uses_selected_serial_and_preserves_every_argument(self) -> None:
        with patch.object(
            adb, "adb_executable", return_value=Path("/sdk/platform-tools/adb")
        ):
            self.assertEqual(
                adb.adb_command("phone", "shell", "pidof", "com.example"),
                [
                    "/sdk/platform-tools/adb",
                    "-s",
                    "phone",
                    "shell",
                    "pidof",
                    "com.example",
                ],
            )


class AdbRunCommandTests(unittest.TestCase):
    def test_bounded_run_merges_stderr_and_returns_the_result(self) -> None:
        completed = subprocess.CompletedProcess(["tool"], 0, "output")
        with patch.object(adb.subprocess, "run", return_value=completed) as run:
            result = adb.run_command(
                ["tool"], cwd=Path("/work"), timeout=3.5, check=True
            )
        self.assertIs(result, completed)
        run.assert_called_once_with(
            ["tool"],
            cwd=Path("/work"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=3.5,
        )

    def test_check_true_rejects_nonzero_exit_with_bounded_tail(self) -> None:
        with (
            patch.object(
                adb.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 7, "failure"),
            ),
            self.assertRaisesRegex(adb.AdbError, "exit=7"),
        ):
            adb.run_command(["tool"], timeout=1, check=True)

    def test_check_false_returns_a_nonzero_exit_as_the_devices_answer(self) -> None:
        failed = subprocess.CompletedProcess([], 2, "device said no")
        with patch.object(adb.subprocess, "run", return_value=failed):
            self.assertIs(adb.run_command(["tool"], timeout=1), failed)

    def test_a_command_that_cannot_start_is_never_a_device_answer(self) -> None:
        cause = FileNotFoundError("no adb")
        with (
            patch.object(adb.subprocess, "run", side_effect=cause),
            self.assertRaises(adb.AdbError) as caught,
        ):
            adb.run_command(["tool"], timeout=1)
        self.assertIs(caught.exception.__cause__, cause)
        self.assertIn("tool", str(caught.exception))

    def test_a_timeout_keeps_its_type_and_partial_output(self) -> None:
        expire = subprocess.TimeoutExpired(["tool"], 1, output=b"partial")
        with (
            patch.object(adb.subprocess, "run", side_effect=expire),
            self.assertRaises(subprocess.TimeoutExpired) as caught,
        ):
            adb.run_command(["tool"], timeout=1)
        self.assertIs(caught.exception, expire)
        self.assertEqual(caught.exception.stdout, b"partial")

    def test_started_commands_run_with_a_finite_budget_and_merged_stderr(self) -> None:
        process = Mock()
        with patch.object(adb.subprocess, "Popen", return_value=process) as popen:
            self.assertIs(adb.start_command(["tool"], cwd=Path("/work")), process)
        popen.assert_called_once_with(
            ["tool"],
            cwd=Path("/work"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def test_a_streaming_command_that_cannot_start_is_a_typed_failure(self) -> None:
        with (
            patch.object(
                adb.subprocess, "Popen", side_effect=FileNotFoundError("no adb")
            ),
            self.assertRaises(adb.AdbError),
        ):
            adb.start_command(["tool"])


if __name__ == "__main__":
    unittest.main()
