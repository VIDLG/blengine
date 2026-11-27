"""Bounded toolchain probes, including CLI background metrics pipe inheritance."""

from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools import android_toolchain


class AndroidToolchainOutputTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX inherited-output regression")
    def test_parent_exit_does_not_wait_for_background_output_handles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "child.pid"
            script = (
                "import subprocess,sys; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(10)']); "
                "Path(sys.argv[1]).write_text(str(child.pid)); "
                "print('1.0.123',flush=True)"
            )
            started = time.monotonic()
            try:
                result = android_toolchain._run_output(
                    [sys.executable, "-c", script, str(pid_path)], timeout=2
                )
                self.assertEqual(result, (0, "1.0.123"))
                self.assertLess(time.monotonic() - started, 2)
                child_pid = int(pid_path.read_text())
                os.kill(child_pid, 0)
            finally:
                if pid_path.exists():
                    try:
                        os.kill(int(pid_path.read_text()), signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def test_nonzero_status_preserves_both_outputs_and_closes_stdin(self) -> None:
        script = (
            "import sys; assert sys.stdin.read()==''; "
            "print('out'); print('err',file=sys.stderr); sys.exit(7)"
        )
        self.assertEqual(
            android_toolchain._run_output([sys.executable, "-c", script]),
            (7, "out\nerr"),
        )

    def test_hung_probe_has_bounded_actionable_failure(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "toolchain probe timed out.*0.1"):
            android_toolchain._run_output(
                [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1
            )


if __name__ == "__main__":
    unittest.main()
