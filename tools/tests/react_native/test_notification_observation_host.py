"""Opt-in CLI, proof preflight and bundle diagnostics, using synthetic data only."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.peripherals.nrf52840 import notification_observation_capture as observation
from tests.peripherals.nrf52840.support.notification_observation_capture import (
    attach,
    synthetic_profile_admission,
)
from tools.react_native import rf_evidence_bundle as evidence
from tools.react_native import rf_scenario_runner
from tools.react_native import run_rf_ccc_disable_reject as cli
from tools.tests.react_native.support import evidence_ccc_disable_reject as samples
from tools.tests.react_native.support.ccc_disable_reject_parent import Harness


class NotificationObservationHostTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(synthetic_profile_admission())

    def test_cli_default_off_explicit_opt_in_and_other_modes_reject_before_preflight(
        self,
    ):
        args = ("ios", "phone", Path("app"), Path("contract"), Path("profile"))
        with patch.object(
            cli.rf_scenario_runner, "run", return_value=Path("manifest")
        ) as run:
            cli.run(*args)
            self.assertNotIn("observe_notification_tx", run.call_args.kwargs)
            cli.run(*args, observe_notification_tx=True)
            self.assertIs(run.call_args.kwargs["observe_notification_tx"], True)
        argv = [
            "run",
            "--platform",
            "ios",
            "--device",
            "phone",
            "--artifact",
            "app",
            "--fixture-contract",
            "contract",
            "--fixture-profile",
            "profile",
            "--observe-notification-tx",
        ]
        with (
            patch("sys.argv", argv),
            patch.object(cli, "run", return_value=Path("manifest")) as run,
        ):
            self.assertEqual(cli.main(), 0)
            self.assertIs(run.call_args.kwargs["observe_notification_tx"], True)
        with patch.object(rf_scenario_runner, "preflight_fixture") as preflight:
            for scenario in (
                "full-suite",
                "recovery-ten",
                "write-reject",
                "ccc-enable-reject",
            ):
                with self.assertRaisesRegex(ValueError, "CCC disable only"):
                    rf_scenario_runner.run(
                        *args, scenario=scenario, observe_notification_tx=True
                    )
            preflight.assert_not_called()

    def test_missing_capability_never_starts_provider_or_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary), "android")
            with self.assertRaisesRegex(ValueError, "capability"):
                h.api.run(
                    "android",
                    "synthetic-device",
                    h.artifact,
                    h.contract,
                    h.profile,
                    scenario="ccc-disable-reject",
                    timeout=180,
                    observe_notification_tx=True,
                )
            self.assertFalse(
                any("direct-start" in command for command, _ in h.commands)
            )
            self.assertNotIn("install", h.events)
            h.capture.assert_not_called()

    def test_requested_capture_bundle_roundtrip_hash_drift_and_semantic_replay(self):
        with tempfile.TemporaryDirectory() as temporary:

            def owner(replay):
                attach(replay.phone)
                return replay.phone.owner

            with patch.object(samples.Replay, "_new_owner", owner):
                bundle, _ = samples.ready(Path(temporary) / "source", "android")
            self.finish(bundle)
            manifest = bundle.attempt / evidence.MANIFEST
            evidence.verify_bundle(manifest)
            document = samples.load(manifest)
            self.assertIn("notification_observation_request", document["references"])
            self.assertIn("notification_observation", document["references"])
            capture = bundle.attempt / "coordination" / observation.CAPTURE_NAME
            original = capture.read_bytes()
            capture.write_bytes(original + b" ")
            with self.assertRaisesRegex(ValueError, "inventory/hash drift"):
                evidence.verify_bundle(manifest)
            capture.write_bytes(original)
            changed = json.loads(original)
            index = changed["updates"][0]["reads"][0]
            changed["events"][index]["wire"]["decoded"]["tx_errno"] = 123
            samples.write(capture, changed)
            samples.rehash(bundle.attempt)
            with self.assertRaisesRegex(ValueError, "raw wire"):
                evidence.verify_bundle(manifest)
            with self.assertRaises(ValueError):
                self.finish(bundle)
            self.assertEqual(samples.load(manifest)["status"], "failed")

    def test_failed_bundle_missing_runtime_still_replays_diagnostics_and_legacy_absence_unchanged(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            bundle, _ = samples.ready(Path(temporary) / "legacy", "ios")
            self.finish(bundle)
            manifest = bundle.attempt / evidence.MANIFEST
            before = manifest.read_bytes()
            evidence.verify_bundle(manifest)
            self.assertEqual(before, manifest.read_bytes())
            self.assertNotIn(
                "notification_observation", samples.load(manifest)["references"]
            )
            request = bundle.attempt / "coordination" / observation.REQUEST_NAME
            samples.write(
                request, {"schema_version": 1, "session": "foreign", "request": True}
            )
            bundle.document = copy.deepcopy(bundle.document)
            bundle.finish(
                completed=False,
                primary_error=RuntimeError("runtime failed first"),
                cleanup_errors=[],
                runtime_log=bundle.attempt / "missing-runtime.log",
            )
            self.assertEqual(
                samples.load(manifest)["primary_error"]["message"],
                "runtime failed first",
            )
            with self.assertRaisesRegex(ValueError, "session"):
                evidence.verify_bundle(manifest)

    @staticmethod
    def finish(bundle):
        bundle.finish(
            completed=True,
            primary_error=None,
            cleanup_errors=[],
            runtime_log=bundle.attempt / samples.LOG,
        )
