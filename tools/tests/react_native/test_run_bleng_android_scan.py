"""Thin CLI admission, verdict and recipe checks; the live runner is isolated."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.react_native import run_bleng_android_scan as cli
from tools.react_native.bleng_scan_evidence import SCOPE
from tools.react_native.rf_runner import ProgressReporter


class ManagedScanCliTests(unittest.TestCase):
    def setUp(self):
        self.output = Path(self.enterContext(tempfile.TemporaryDirectory())) / "runs"
        self.runner = self.enterContext(patch.object(cli, "run_android_scan"))
        self.verify = self.enterContext(patch.object(cli, "verify_scan_bundle"))
        self.runner.return_value = Path("/isolated/result.json")
        self.verify.return_value = {
            "scope": SCOPE,
            "status": "passed",
            "host_gate_status": "pending",
            "rf": "scan-ui-observation",
        }
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def arguments(self):
        arguments = [
            "--device",
            "ISOLATEDPHONE",
            "--app-id",
            "dev.blehub.formal",
            "--activity",
            "dev.blehub.formal.MainActivity",
            "--apk",
            "candidate.apk",
            "--apk-sha256",
            "1" * 64,
            "--test-apk",
            "candidate-test.apk",
            "--test-apk-sha256",
            "3" * 64,
            "--fixture-contract",
            "contract.json",
            "--fixture-profile",
            "profile.json",
            "--fixture-port",
            "isolated-port",
            "--fixture-resource-identity",
            "jlink:123456",
            "--output",
            str(self.output),
            "--authorize-install",
            "--authorize-fixture-start",
            "--authorize-bluetooth-permissions",
        ]
        return arguments

    def test_requires_all_mutation_authorizations_before_runner(self):
        for flag in (
            "--authorize-install",
            "--authorize-fixture-start",
            "--authorize-bluetooth-permissions",
        ):
            arguments = self.arguments()
            arguments.remove(flag)
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                cli.main(arguments)
        self.runner.assert_not_called()
        self.verify.assert_not_called()

    def test_permission_authorization_help_describes_exact_scope(self):
        with self.assertRaises(SystemExit) as raised:
            cli.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = self.stdout.getvalue()
        self.assertIn("--authorize-bluetooth-permissions", help_text)
        self.assertIn("BLUETOOTH_SCAN/CONNECT", help_text)
        self.assertIn("root grant", help_text)
        self.assertIn("readback and exact restore", help_text)
        self.assertIn("does not change radio", help_text)

    def test_explicit_root_is_not_implicit_and_inputs_are_forwarded(self):
        self.assertEqual(cli.main(self.arguments()), 0)
        values = self.runner.call_args.kwargs
        self.assertFalse(values["use_root"])
        self.assertEqual(values["fixture_resource_identity"], "jlink:123456")
        self.assertTrue(values["authorize_fixture_start"])
        self.assertTrue(values["authorize_bluetooth_permissions"])
        self.assertTrue(values["apk"].is_absolute())
        self.assertTrue(values["test_apk"].is_absolute())
        self.assertEqual(values["test_apk_sha256"], "3" * 64)
        self.verify.assert_called_once_with(Path("/isolated"))
        self.assertIn(
            "host_gate=pending rf=scan-ui-observation", self.stdout.getvalue()
        )
        self.assertEqual(cli.main(self.arguments() + ["--use-root"]), 0)
        self.assertTrue(self.runner.call_args.kwargs["use_root"])

    def test_a_blocking_run_narrates_its_stages_instead_of_polling_a_log(self):
        self.assertEqual(cli.main(self.arguments()), 0)
        progress = self.runner.call_args.kwargs["progress"]
        self.assertIsInstance(progress, ProgressReporter)
        self.assertEqual(progress.scenario, "scan")
        self.assertEqual(progress.prefix, "formal_rn")
        self.assertTrue(progress.auto_heartbeat)
        # The context manager owns the ticker: it never outlives the run it narrates.
        self.assertIsNone(progress._ticker)

    def test_a_run_journals_its_progress_beside_its_evidence_by_default(self):
        # A blocking run's state can be asked for by reading one file, not by tailing a log.
        def narrate(*_, progress, **__):
            with progress.phase("scene", kind="scan"):
                pass
            return Path("/isolated/result.json")

        self.runner.side_effect = narrate
        self.assertEqual(cli.main(self.arguments()), 0)
        events = [
            json.loads(line)
            for line in (self.output / "progress.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [(event["phase"], event["status"]) for event in events],
            [("scene", "started"), ("scene", "passed")],
        )
        self.assertEqual(events[0]["scenario"], "scan")
        self.assertEqual(events[0]["kind"], "scan")

    def test_the_caller_may_name_another_journal(self):
        named = self.output.parent / "elsewhere.jsonl"
        self.assertEqual(
            cli.main(self.arguments() + ["--progress-journal", str(named)]), 0
        )
        self.assertTrue(named.is_file())
        self.assertFalse((self.output / "progress.jsonl").exists())

    def test_an_in_process_scene_requires_its_test_apk(self):
        arguments = self.arguments()
        for flag in ("--test-apk", "--test-apk-sha256"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                trimmed = list(arguments)
                index = trimmed.index(flag)
                del trimmed[index : index + 2]
                cli.main(trimmed)
        self.runner.assert_not_called()

    def test_verified_failed_bundle_and_wrong_scope_never_report_success(self):
        self.verify.return_value = {
            "scope": SCOPE,
            "status": "failed",
            "host_gate_status": "failed",
            "rf": "scan-ui-observation",
        }
        self.assertEqual(cli.main(self.arguments()), 2)
        self.verify.return_value["scope"] = "formal-android-managed-lifecycle"
        self.assertEqual(cli.main(self.arguments()), 2)
        self.assertNotIn("formal_android_managed_scene=passed", self.stdout.getvalue())
        self.assertIn("scope differs", self.stderr.getvalue())

    def test_interrupt_is_not_converted_to_an_ordinary_verdict(self):
        interruption = KeyboardInterrupt("owned run interrupted")
        self.runner.side_effect = interruption
        with self.assertRaises(KeyboardInterrupt) as raised:
            cli.main(self.arguments())
        self.assertIs(raised.exception, interruption)
        self.verify.assert_not_called()

    def test_just_entries_have_no_setup_dependencies(self):
        recipes = (cli.PROJECT_ROOT / "just/react-native/mod.just").read_text()
        for name in (
            "formal-host-android-managed-scan",
            "formal-host-android-managed-notification",
            "formal-host-android-managed-indication",
            "formal-host-android-native-notification",
        ):
            with self.subTest(recipe=name):
                self.assertIn(f"\n{name} +args:\n", recipes)
                self.assertNotIn(f"\n{name} +args: ", recipes)
        self.assertIn("\nformal-host-scan-verify attempt:\n", recipes)
        self.assertNotIn("\nformal-host-scan-verify attempt: ", recipes)
