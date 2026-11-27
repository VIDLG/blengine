"""Regression tests for the opt-in terminal-free quiet runner."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[3] / "tools/react_native/run_quiet.py"


@unittest.skipUnless(os.name == "posix", "process-group isolation requires POSIX")
class RunQuietTests(unittest.TestCase):
    def test_no_tty_closes_stdin_and_detaches_controlling_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "quiet.log"
            child_code = (
                "import os\n"
                "assert os.read(0, 1) == b''\n"
                "try:\n"
                "    os.open('/dev/tty', os.O_RDONLY)\n"
                "except OSError:\n"
                "    print('no controlling terminal')\n"
                "else:\n"
                "    raise AssertionError('child inherited a controlling terminal')\n"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--no-tty",
                    str(log),
                    "--",
                    sys.executable,
                    "-c",
                    child_code,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no controlling terminal", log.read_text(encoding="utf-8"))

    def test_no_tty_preserves_failure_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "quiet.log"
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--no-tty",
                    str(log),
                    "--",
                    sys.executable,
                    "-c",
                    "import sys; print('test failure'); sys.exit(7)",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 7)
            self.assertIn("test failure", log.read_text(encoding="utf-8"))
            self.assertIn("test failure", result.stderr)

    def test_progress_preserves_quiet_logs_and_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "quiet.log"
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--no-tty",
                    "--progress-seconds",
                    "1",
                    str(log),
                    "--",
                    sys.executable,
                    "-c",
                    "import time; print('private build detail'); time.sleep(2.2)",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("quiet command running:", result.stdout)
            self.assertIn("elapsed=", result.stdout)
            self.assertNotIn("private build detail", result.stdout)
            self.assertIn("private build detail", log.read_text(encoding="utf-8"))

    def test_interrupt_terminates_detached_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "quiet.log"
            ready = Path(directory) / "ready"
            stopped = Path(directory) / "stopped"
            child_code = (
                "import signal, sys, time\n"
                "from pathlib import Path\n"
                f"ready = Path({str(ready)!r})\n"
                f"stopped = Path({str(stopped)!r})\n"
                "def stop(_signum, _frame):\n"
                "    stopped.write_text('terminated', encoding='utf-8')\n"
                "    sys.exit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "ready.write_text('ready', encoding='utf-8')\n"
                "time.sleep(60)\n"
            )
            runner = subprocess.Popen(
                [
                    sys.executable,
                    str(RUNNER),
                    "--no-tty",
                    str(log),
                    "--",
                    sys.executable,
                    "-c",
                    child_code,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    self.assertIsNone(
                        runner.poll(), "quiet runner exited before starting its child"
                    )
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), "detached command never started")
                runner.terminate()
                _, stderr = runner.communicate(timeout=10)
                self.assertEqual(runner.returncode, 128 + 15, stderr)
                deadline = time.monotonic() + 5
                while not stopped.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(stopped.exists(), "detached command was left running")
            finally:
                if runner.poll() is None:
                    runner.kill()
                    runner.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
