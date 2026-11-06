"""Managed lifecycle CLI delegates ownership; no synthetic identity reaches a device."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.react_native import run_bleng_android_lifecycle as cli
from tools.tests.react_native.test_recipe_wiring import ROOT, dry_run


class AndroidLifecycleCliTests(unittest.TestCase):
    def setUp(self):
        self.arguments = [
            "--device",
            "synthetic-cli-only",
            "--app-id",
            "example.formal",
            "--activity",
            "example.formal.MainActivity",
            "--apk",
            "candidate.apk",
            "--apk-sha256",
            "a" * 64,
            "--appium-home",
            "appium home",
            "--appium-entry",
            "appium home/main.js",
            "--node",
            "node",
            "--appium-port",
            "4729",
            "--system-port",
            "8219",
            "--output",
            "output",
            "--timeout",
            "240",
            "--phase-timeout",
            "25",
            "--cleanup-seconds",
            "60",
            "--authorize-install",
            "--authorize-helper-settings",
            "--use-root",
        ]
        self.helpers = {
            "io.appium.settings": "b" * 64,
            "io.appium.uiautomator2.server": "c" * 64,
            "io.appium.uiautomator2.server.test": "d" * 64,
        }
        for package, digest in self.helpers.items():
            self.arguments += ["--helper-sha256", f"{package}={digest}"]
        # Patch the named runner, not a global successful device/owner mock.
        self.run = self.enterContext(patch.object(cli, "run_android_lifecycle"))
        self.path = Path("/synthetic-cli-only/result.json")
        self.run.return_value = self.path
        self.verify = self.enterContext(patch.object(cli, "verify_lifecycle_bundle"))
        self.verify.return_value = {
            "scope": "formal-android-managed-lifecycle",
            "status": "passed",
            "host_gate_status": "pending",
            "rf": "not_run",
        }
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def test_complete_arguments_delegate_once_and_replay_before_reporting(self):
        self.assertEqual(cli.main(self.arguments), 0)
        self.run.assert_called_once_with(
            device="synthetic-cli-only",
            app_id="example.formal",
            activity="example.formal.MainActivity",
            apk=Path("candidate.apk").absolute(),
            apk_sha256="a" * 64,
            appium_home=Path("appium home").absolute(),
            appium_entry=Path("appium home/main.js").absolute(),
            node="node",
            expected_helper_sha256=self.helpers,
            appium_port=4729,
            system_port=8219,
            output_root=Path("output").absolute(),
            timeout=240,
            phase_timeout=25,
            cleanup_seconds=60,
            authorize_install=True,
            authorize_helper_settings=True,
            use_root=True,
            cwd=cli.PROJECT_ROOT,
            close_diagnostics=False,
            automation_idle_control=False,
            automation_start_hold_control=False,
        )
        self.verify.assert_called_once_with(self.path.parent)
        self.assertIn("host_gate=pending rf=not_run", self.stdout.getvalue())

    def test_close_diagnostics_requires_explicit_cli_opt_in(self):
        self.assertEqual(cli.main(self.arguments + ["--close-diagnostics"]), 0)
        self.assertIs(self.run.call_args.kwargs["close_diagnostics"], True)

    def test_idle_control_requires_explicit_diagnostics_and_passes_distinct_flag(self):
        with self.assertRaises(SystemExit) as error:
            cli.main(self.arguments + ["--automation-idle-control"])
        self.assertEqual(error.exception.code, 2)
        self.run.assert_not_called()
        self.verify.return_value["scope"] = "formal-android-managed-idle-control"
        self.assertEqual(
            cli.main(
                self.arguments + ["--automation-idle-control", "--close-diagnostics"]
            ),
            0,
        )
        self.assertIs(self.run.call_args.kwargs["automation_idle_control"], True)
        self.assertIs(self.run.call_args.kwargs["close_diagnostics"], True)
        self.assertIn(
            "formal_android_managed_idle_control=passed", self.stdout.getvalue()
        )
        self.assertNotIn(
            "formal_android_managed_lifecycle=passed", self.stdout.getvalue()
        )

    def test_idle_control_rejects_a_verified_lifecycle_scope(self):
        self.assertEqual(
            cli.main(
                self.arguments + ["--automation-idle-control", "--close-diagnostics"]
            ),
            2,
        )
        self.assertIn(
            "formal_android_managed_idle_control=failed", self.stderr.getvalue()
        )
        self.assertIn("scope differs", self.stderr.getvalue())
        self.assertNotIn("=passed", self.stdout.getvalue())

    def test_start_hold_requires_diagnostics_and_is_mutually_exclusive(self):
        for flags in (
            ["--automation-start-hold-control"],
            [
                "--automation-start-hold-control",
                "--automation-idle-control",
                "--close-diagnostics",
            ],
        ):
            with self.subTest(flags=flags), self.assertRaises(SystemExit) as error:
                cli.main(self.arguments + flags)
            self.assertEqual(error.exception.code, 2)
        self.run.assert_not_called()
        self.verify.assert_not_called()

    def test_start_hold_reports_only_its_requested_scope_and_result_key(self):
        arguments = self.arguments + [
            "--automation-start-hold-control",
            "--close-diagnostics",
        ]
        self.assertEqual(cli.main(arguments), 2)
        self.assertIn("scope differs", self.stderr.getvalue())
        self.assertNotIn("=passed", self.stdout.getvalue())
        self.verify.return_value["scope"] = "formal-android-managed-start-hold-control"
        self.assertEqual(cli.main(arguments), 0)
        self.assertIs(self.run.call_args.kwargs["automation_start_hold_control"], True)
        self.assertIs(self.run.call_args.kwargs["automation_idle_control"], False)
        self.assertIs(self.run.call_args.kwargs["close_diagnostics"], True)
        self.assertIn(
            "formal_android_managed_start_hold_control=passed", self.stdout.getvalue()
        )
        self.assertNotIn(
            "formal_android_managed_lifecycle=passed", self.stdout.getvalue()
        )

    def test_non_root_choice_is_not_implicitly_upgraded(self):
        arguments = [item for item in self.arguments if item != "--use-root"]
        self.assertEqual(cli.main(arguments), 0)
        self.assertIs(self.run.call_args.kwargs["use_root"], False)

    def test_duplicate_unknown_missing_or_malformed_helper_rejected_before_runner(self):
        variants = [
            self.arguments + ["--helper-sha256", f"io.appium.settings={'b' * 64}"],
            self.arguments + ["--helper-sha256", f"other.package={'b' * 64}"],
            self.arguments[:-2],
            self.arguments[:-1] + ["malformed"],
            self.arguments[:-1] + ["io.appium.uiautomator2.server.test=BAD"],
        ]
        for arguments in variants:
            with (
                self.subTest(arguments=arguments[-2:]),
                self.assertRaises(SystemExit) as error,
            ):
                cli.main(arguments)
            self.assertEqual(error.exception.code, 2)
        self.run.assert_not_called()
        self.verify.assert_not_called()

    def test_authorization_flags_are_explicit_and_required(self):
        for flag in ("--authorize-install", "--authorize-helper-settings"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit) as error:
                cli.main([item for item in self.arguments if item != flag])
            self.assertEqual(error.exception.code, 2)
        self.run.assert_not_called()

    def test_failed_result_and_publication_or_validation_errors_exit_two(self):
        self.verify.return_value = {
            "scope": "formal-android-managed-lifecycle",
            "status": "failed",
            "host_gate_status": "failed",
            "rf": "not_run",
        }
        self.assertEqual(cli.main(self.arguments), 2)
        for error in (
            ValueError("bad input"),
            RuntimeError("publication failed"),
            OSError("missing"),
        ):
            with self.subTest(error=error):
                self.run.side_effect = error
                self.assertEqual(cli.main(self.arguments), 2)
                self.assertIn(str(error), self.stderr.getvalue())
        self.run.side_effect = None
        self.verify.side_effect = ValueError("tampered evidence")
        self.assertEqual(cli.main(self.arguments), 2)

    def test_interrupt_is_not_converted_to_an_ordinary_failure(self):
        self.run.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            cli.main(self.arguments)
        self.verify.assert_not_called()

    def test_just_entries_have_no_build_or_device_setup_dependencies(self):
        commands = dry_run(
            "formal-host-android-managed-lifecycle",
            *self.arguments,
            include_dependencies=True,
        )
        self.assertEqual(
            commands,
            [
                [
                    "pixi",
                    "run",
                    "python",
                    "-m",
                    "tools.react_native.run_bleng_android_lifecycle",
                    "$@",
                ]
            ],
        )
        # Execute only Just's forwarding shell with a local argv recorder in place
        # of Pixi; no Python runner or device SDK is reached, even with shell syntax.
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "pixi"
            executable.write_text(
                f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            forwarded = self.arguments + ["spaces ; ' quotes & syntax"]
            completed = subprocess.run(
                [
                    "just",
                    "react-native",
                    "formal-host-android-managed-lifecycle",
                    *forwarded,
                ],
                cwd=ROOT,
                env=os.environ | {"PATH": directory + os.pathsep + os.environ["PATH"]},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            self.assertEqual(
                json.loads(completed.stdout),
                [
                    "run",
                    "python",
                    "-m",
                    "tools.react_native.run_bleng_android_lifecycle",
                    *forwarded,
                ],
            )
        self.assertEqual(
            dry_run(
                "formal-host-lifecycle-verify",
                "bundle with spaces",
                include_dependencies=True,
            ),
            [
                [
                    "pixi",
                    "run",
                    "python",
                    "-m",
                    "tools.react_native.bleng_lifecycle_evidence",
                    "bundle with spaces",
                ]
            ],
        )
