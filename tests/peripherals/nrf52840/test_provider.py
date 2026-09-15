from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import provider


class ProviderRegressionTests(unittest.TestCase):
    def test_start_uses_canonical_profile_after_source_move(self) -> None:
        # Exercise command construction without launching nrftest or opening a port.
        profile = json.loads(provider.PROFILE.read_text(encoding="utf-8"))
        self.assertEqual(profile["profile_id"], "blehub-nrf52840-indication-v2")
        self.assertTrue((provider.PROJECT_ROOT / "Cargo.toml").is_file())
        self.assertEqual(
            provider.WORK_ROOT.relative_to(provider.PROJECT_ROOT).parts,
            (".work", "peripheral", "nrf52840"),
        )

        def launch(*args: object, **kwargs: object) -> SimpleNamespace:
            log = kwargs["stdout"]
            log.write(provider.READY_MARKER + "test\n")
            log.flush()
            return SimpleNamespace(pid=43210)

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(provider, "WORK_ROOT", Path(temporary)),
            patch.object(provider, "PID_FILE", Path(temporary) / "fixture.pid"),
            patch.object(provider, "LOG_FILE", Path(temporary) / "fixture.log"),
            patch.object(provider, "_nrftest_root", return_value=Path(temporary)),
            patch.object(provider, "_port", return_value="test-port"),
            patch.object(provider, "_alive", return_value=True),
            patch.object(provider.shutil, "which", return_value="pixi"),
            patch.object(provider.subprocess, "Popen", side_effect=launch) as popen,
            redirect_stdout(io.StringIO()) as output,
        ):
            provider.start(ready_timeout=1, rf_timeout=10)
            command = popen.call_args.args[0]
            self.assertEqual(
                command[command.index("--profile") + 1], str(provider.PROFILE)
            )
            self.assertIn("nrf52840_fixture=ready", output.getvalue())

    def test_doctor_is_explicit_and_closes_serial_transport(self) -> None:
        serial = SimpleNamespace(close=Mock())
        result = SimpleNamespace(
            profile_id="blehub-nrf52840-basic-gatt-v1",
            supported_services=6,
            role_handles={"read-write": 37},
        )
        with (
            patch.object(provider, "_port", return_value="test-port"),
            patch.object(provider, "open_serial", return_value=serial),
            patch.object(provider, "run_profile_doctor", return_value=result) as doctor,
            patch.object(provider, "BtpSession") as session,
            patch.object(provider, "FramedTransport") as framed,
            redirect_stdout(io.StringIO()) as output,
        ):
            provider.doctor()
        doctor.assert_called_once()
        serial.close.assert_called_once_with()
        framed.assert_called_once_with(serial)
        session.assert_called_once_with(framed.return_value)
        self.assertIn("nrf52840_doctor=passed", output.getvalue())

    def test_exited_process_cannot_reuse_old_ready_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "fixture.log"
            log.write_text(provider.READY_MARKER + "old-run\n", encoding="utf-8")
            with (
                patch.object(provider, "LOG_FILE", log),
                patch.object(provider, "_alive", return_value=False),
                self.assertRaisesRegex(
                    provider.Nrf52840Error, "exited before readiness"
                ),
            ):
                provider._wait_ready(43210, 1)

    def test_running_process_without_log_reports_not_ready(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(provider, "LOG_FILE", Path(temporary) / "missing.log"),
            patch.object(provider, "_pid", return_value=43210),
            patch.object(provider, "_alive", return_value=True),
            redirect_stdout(io.StringIO()) as output,
        ):
            provider.status()
            self.assertIn("nrf52840_fixture=running", output.getvalue())
            self.assertIn("nrf52840_fixture_ready=false", output.getvalue())


if __name__ == "__main__":
    unittest.main()
