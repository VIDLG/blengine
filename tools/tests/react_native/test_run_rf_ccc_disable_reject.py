"""CCC named runner lifecycle/CLI boundaries, not bundle or RF validation."""

from __future__ import annotations

import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.peripherals.nrf52840.cancellation_control import load_control
from tools.react_native import run_rf_ccc_disable_reject as cli
from tools.tests.react_native.support.ccc_disable_reject_parent import Harness


class RunCccDisableRejectTests(unittest.TestCase):
    def make(self, platform="android"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Harness(Path(temporary.name), platform)

    def test_named_control_frozen_install_receipt_and_independent_checker_before_cleanup(
        self,
    ):
        for platform in ("android", "ios"):
            with self.subTest(platform=platform):
                h = self.make(platform)
                self.assertEqual(h.run(), h.attempt / "evidence-bundle.json")
                control = load_control(h.plan.root.parent / h.protocol.CONTROL_NAME)
                self.assertEqual(
                    (control.flow, control.mode, control.session),
                    (12, "rf-ccc-disable-reject", h.plan.session_id),
                )
                h.protocol.prepare_control.assert_called_once_with(
                    h.plan.root.parent, h.plan
                )
                start = next(argv for argv, _ in h.commands if "direct-start" in argv)
                self.assertEqual(
                    start[start.index("--coordination-mode") + 1],
                    "ccc-disable-reject-attested",
                )
                install = next(
                    argv
                    for argv, _ in h.commands
                    if "tools.android.install_apk" in argv or "install" in argv
                )
                installed = (
                    Path(install[install.index("--apk") + 1])
                    if platform == "android"
                    else Path(install[-1])
                )
                self.assertEqual(installed, h.snapshot)
                self.assertNotEqual(installed, h.artifact)
                self.assertLess(h.events.index("receipt"), h.events.index("install"))
                self.assertLess(
                    h.events.index("checker"), h.events.index("fixture-stop")
                )
                self.assertLess(
                    h.events.index("fixture-stop"), h.events.index("finish")
                )
                self.assertEqual(h.capture.call_args.kwargs["timeout"], 180)
                self.assertEqual(
                    h.capture.call_args.kwargs["fixture_identity"], (1234, 1234)
                )
                self.assertEqual(
                    h.checker.call_args.kwargs["physical_path"],
                    h.plan.root.parent / h.protocol.PHYSICAL_NAME,
                )
                self.assertNotIn("host_cycles", h.checker.call_args.kwargs)
                self.assertTrue(h.finish_arguments["completed"])
                self.assertIsNone(h.finish_arguments["primary_error"])
                self.assertEqual(h.finish_arguments["cleanup_errors"], [])
                h.api.write_reject_capture.capture.assert_not_called()
                h.api.write_reject_ledger.verify.assert_not_called()
                h.api.recovery_ten_capture.capture.assert_not_called()
                self.assertFalse(
                    (h.plan.root.parent / "recovery-ten-control.json").exists()
                )
                self.assertEqual(
                    h.handlers.call_args_list[-1].args,
                    (signal.SIGTERM, h.previous_handler),
                )

    def test_candidate_drift_or_recovery_cycle_result_cannot_pass(self):
        h = self.make()
        h.faults.before_verify = lambda: h.snapshot.write_bytes(b"candidate drift")
        with self.assertRaises((ValueError, RuntimeError)):
            h.run()
        self.assertNotIn("install", h.events)
        h = self.make()
        h.capture.side_effect = lambda *args, **kwargs: (
            "opaque capture boundary",
            (0,),
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "ccc-disable-reject capture returned recovery cycle identities",
        ):
            h.run()
        h.checker.assert_not_called()
        self.assertFalse(h.finish_arguments["completed"])

    def test_timeout_bounds_and_cli_select_only_ccc_disable_reject(self):
        h = self.make()
        for timeout in (0, -1, 301, float("inf"), float("nan"), True, "180"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                h.run(timeout=timeout)
        self.assertEqual(h.events, [])
        args = ("ios", "device", Path("app"), Path("contract"), Path("profile"))
        with patch.object(
            cli.rf_scenario_runner, "run", return_value=Path("manifest")
        ) as run:
            self.assertEqual(cli.run(*args), Path("manifest"))
        run.assert_called_once_with(
            *args, timeout=180, scenario="ccc-disable-reject", port=None
        )
        h = self.make()
        h.run(timeout=300)
        self.assertEqual(h.capture.call_args.kwargs["timeout"], 300)
        argv = [
            "run_rf_ccc_disable_reject",
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
