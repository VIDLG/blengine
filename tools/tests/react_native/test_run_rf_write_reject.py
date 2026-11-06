"""Named runner lifecycle/CLI boundaries only; not App/bundle/RF validation."""

from __future__ import annotations

import signal
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tests.peripherals.nrf52840 import write_reject as protocol
from tests.peripherals.nrf52840.cancellation_control import load_control
from tools.react_native import run_rf_write_reject as cli
from tools.tests.react_native.support.write_reject_parent import Harness


class RunWriteRejectTests(unittest.TestCase):
    def make(self, platform="android"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Harness(Path(temporary.name), platform)

    def test_both_platforms_named_control_snapshot_install_and_independent_check_before_cleanup(
        self,
    ):
        for platform in ("android", "ios"):
            with self.subTest(platform=platform):
                h = self.make(platform)
                result = h.run()
                self.assertEqual(result, h.attempt / "evidence-bundle.json")
                control = load_control(h.plan.root.parent / protocol.CONTROL_NAME)
                self.assertEqual(
                    (control.flow, control.mode, control.session),
                    (10, "rf-write-reject", h.plan.session_id),
                )
                start = next(argv for argv, _ in h.commands if "direct-start" in argv)
                self.assertEqual(
                    start[start.index("--coordination-mode") + 1], protocol.MODE
                )
                install = next(
                    argv
                    for argv, _ in h.commands
                    if "tools.android.install_apk" in argv or "install" in argv
                )
                self.assertEqual(
                    Path(install[install.index("--apk") + 1])
                    if platform == "android"
                    else Path(install[-1]),
                    h.snapshot,
                )
                self.assertNotEqual(h.snapshot, h.artifact)
                self.assertLess(h.events.index("receipt"), h.events.index("install"))
                self.assertLess(
                    h.events.index("checker"), h.events.index("fixture-stop")
                )
                self.assertLess(
                    h.events.index("fixture-stop"), h.events.index("finish")
                )
                self.assertEqual(
                    h.capture.call_args.kwargs["fixture_identity"], (1234, 1234)
                )
                self.assertEqual(
                    h.checker.call_args.kwargs["physical_path"],
                    h.plan.root.parent / protocol.PHYSICAL_NAME,
                )
                self.assertNotIn("host_cycles", h.checker.call_args.kwargs)
                self.assertTrue(h.finish_arguments["completed"])
                self.assertIsNone(h.finish_arguments["primary_error"])
                self.assertEqual(h.finish_arguments["cleanup_errors"], [])
                h.api.recovery_ten_capture.capture.assert_not_called()
                h.api.recovery_ten_ledger.verify.assert_not_called()
                self.assertFalse(
                    (h.plan.root.parent / "recovery-ten-control.json").exists()
                )
                self.assertEqual(
                    h.handlers.call_args_list[-1].args,
                    (signal.SIGTERM, h.previous_handler),
                )

    def test_missing_or_foreign_live_receipt_prevents_install_and_capture(self):
        for fault in ("missing", "foreign"):
            with self.subTest(fault=fault):
                h = self.make()
                publish = h._publish_receipt

                def receipt(fault=fault, publish=publish, h=h):
                    if fault == "foreign":
                        publish()
                        h.corrupt_receipt()

                h._publish_receipt = receipt
                with self.assertRaises(RuntimeError):
                    h.run()
                self.assertNotIn("install", h.events)
                h.capture.assert_not_called()
                self.assertFalse(h.finish_arguments["completed"])
                self.assertIsNotNone(h.finish_arguments["primary_error"])

    def test_primary_cleanup_and_finalization_errors_remain_distinct(self):
        for boundary in ("install", "capture", "checker"):
            with self.subTest(boundary=boundary):
                h = self.make()
                setattr(h.faults, boundary, RuntimeError("primary " + boundary))
                h.faults.stop = RuntimeError("cleanup stop")
                with self.assertRaisesRegex(
                    RuntimeError, "primary " + boundary
                ) as raised:
                    h.run()
                self.assertFalse(h.finish_arguments["completed"])
                self.assertIn(
                    "cleanup stop", str(getattr(raised.exception, "__notes__", []))
                )
                self.assertEqual(
                    h.handlers.call_args_list[-1].args,
                    (signal.SIGTERM, h.previous_handler),
                )
        h = self.make()
        h.faults.finish = RuntimeError("bundle boundary failure")
        with self.assertRaisesRegex(RuntimeError, "bundle boundary failure"):
            h.run()

    def test_candidate_drift_and_foreign_cleanup_owner_fail_closed(self):
        h = self.make()
        h.faults.before_verify = lambda: h.snapshot.write_bytes(b"candidate drift")
        with self.assertRaises((ValueError, RuntimeError, zipfile.BadZipFile)):
            h.run()
        self.assertNotIn("install", h.events)
        h = self.make()
        h.faults.capture = RuntimeError("capture failed")
        h.faults.after_capture = h.replace_owner
        # Replace ownership at capture entry, without touching shared modules.
        capture = h.capture.side_effect

        def replaced(*args, **kwargs):
            h.replace_owner()
            return capture(*args, **kwargs)

        h.capture.side_effect = replaced
        with self.assertRaises(RuntimeError):
            h.run()
        self.assertNotIn("fixture-stop", h.events)

    def test_timeout_and_cli_select_only_write_reject(self):
        h = self.make()
        for timeout in (0, -1, 301, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                h.run(timeout=timeout)
        self.assertEqual(h.events, [])
        args = ("ios", "device", Path("app"), Path("contract"), Path("profile"))
        with patch.object(
            cli.rf_scenario_runner, "run", return_value=Path("manifest")
        ) as run:
            self.assertEqual(cli.run(*args), Path("manifest"))
        run.assert_called_once_with(
            *args, timeout=180, scenario="write-reject", port=None
        )
        argv = [
            "run_rf_write_reject",
            "--platform",
            "android",
            "--device",
            "device",
            "--artifact",
            "app",
            "--fixture-contract",
            "contract",
            "--fixture-profile",
            "profile",
        ]
        with (
            patch("sys.argv", argv),
            patch.object(cli, "run", side_effect=ValueError("rejected")),
        ):
            self.assertEqual(cli.main(), 2)
