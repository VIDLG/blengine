"""Exercise live host handoff before child exit; never invoke the Android SDK."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from tools.android import run_instrumentation as instrumentation


class InstrumentationCaptureTests(unittest.TestCase):
    def test_small_status_is_delivered_before_child_waits_for_host_ack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ack = Path(directory) / "host-ack"

            class HandshakeLog(io.StringIO):
                def write(self, text: str) -> int:
                    size = super().write(text)
                    if "SUBSCRIBED" in self.getvalue().splitlines():
                        ack.write_text("observed-before-exit", encoding="ascii")
                    return size

            script = dedent("""\
                import sys, time
                from pathlib import Path
                ack = Path(sys.argv[1])
                print('INSTRUMENTATION_STATUS: numtests=1')
                print('SUBSCRIBED', flush=True)
                deadline = time.monotonic() + 2
                while not ack.is_file():
                    if time.monotonic() >= deadline:
                        print('FAILURES!!! host never saw status before child exit', flush=True)
                        sys.exit(1)
                    time.sleep(0.01)
                print('INSTRUMENTATION_STATUS_CODE: 0')
                print('INSTRUMENTATION_RESULT: stream=')
                print('OK (1 test)')
                print('INSTRUMENTATION_CODE: -1', flush=True)
                """)
            output = HandshakeLog()
            instrumentation._run_instrumentation(
                [sys.executable, "-c", script, str(ack)],
                log=output,
                timeout=5,
                progress_label="synthetic-live-handoff",
            )
            self.assertEqual(ack.read_text(encoding="ascii"), "observed-before-exit")
            self.assertIn("OK (1 test)", output.getvalue())
