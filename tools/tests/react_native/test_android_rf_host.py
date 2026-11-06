"""Android APK identity and first-PID boundaries; no retired suite harness."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.react_native import android_rf_host, android_sdk


class AndroidInitialPidTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "runtime.log"

    @contextmanager
    def boot(
        self, responses: list[tuple[int, str] | BaseException]
    ) -> Iterator[SimpleNamespace]:
        clock = [0.0]
        calls: list[float] = []

        def query(
            command: list[str], *, timeout: float, check: bool
        ) -> subprocess.CompletedProcess[str]:
            self.assertFalse(check)
            self.assertEqual(
                command, ["fake-adb", "phone", "shell", "pidof", "com.blehubnitrohost"]
            )
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 1)
            response = responses[min(len(calls), len(responses) - 1)]
            calls.append(timeout)
            clock[0] += min(0.05, timeout)
            if isinstance(response, BaseException):
                raise response
            code, output = response
            return subprocess.CompletedProcess(command, code, output)

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        guard = Mock()
        with (
            patch.object(
                android_rf_host.android_sdk,
                "adb_command",
                side_effect=lambda serial, *args: ["fake-adb", serial, *args],
            ),
            patch.object(
                android_rf_host.android_sdk, "run_command", side_effect=query
            ) as command,
            patch.object(
                android_rf_host.time, "monotonic", side_effect=lambda: clock[0]
            ),
            patch.object(android_rf_host.time, "sleep", side_effect=sleep),
        ):
            yield SimpleNamespace(
                guard=guard, command=command, clock=clock, calls=calls
            )

    def test_delayed_process_entry_pins_first_pid_and_retains_boot_identity(
        self,
    ) -> None:
        with self.boot([(1, ""), (1, "\n"), (0, "42\n"), (0, "43")]) as boot:
            self.assertEqual(
                android_rf_host.wait_for_initial_pid(
                    "phone", self.log, 900, guard=boot.guard
                ),
                "42",
            )
            self.assertEqual(boot.command.call_count, 3)
        identity = json.loads(self.log.with_suffix(".identity.json").read_text())
        self.assertEqual(identity["status"], "pinned")
        self.assertEqual(identity["pid"], "42")
        self.assertEqual(
            [record["exit_code"] for record in identity["observations"]], [1, 1, 0]
        )
        self.assertEqual(identity["boot_budget_seconds"], 4)

    def test_boot_respects_shorter_suite_budget_and_owner_loss(self) -> None:
        with (
            self.boot([(1, "")]) as boot,
            self.assertRaisesRegex(RuntimeError, "boot deadline"),
        ):
            android_rf_host.wait_for_initial_pid(
                "phone", self.log, 0.2, guard=boot.guard
            )
        self.assertLessEqual(boot.clock[0], 0.200001)
        with self.boot([(1, "")]) as boot:
            boot.guard.side_effect = RuntimeError("fixture owner lost")
            with self.assertRaisesRegex(RuntimeError, "fixture owner lost"):
                android_rf_host.wait_for_initial_pid(
                    "phone", self.log, 900, guard=boot.guard
                )
            boot.command.assert_not_called()

    def test_ambiguous_pid_adb_failure_and_transport_failure_never_retry(self) -> None:
        for response in (
            (0, "42 43"),
            (0, ""),
            (0, "not-a-pid"),
            (1, "device offline"),
            (2, ""),
            android_sdk.AndroidSdkError("ADB transport failed"),
        ):
            with (
                self.subTest(response=response),
                self.boot([response]) as boot,
                self.assertRaises(RuntimeError),
            ):
                android_rf_host.wait_for_initial_pid(
                    "phone", self.log, 900, guard=boot.guard
                )
            boot.command.assert_called_once()
            self.assertEqual(
                json.loads(self.log.with_suffix(".identity.json").read_text())[
                    "status"
                ],
                "failed",
            )

    def test_absent_process_is_bounded_and_retains_failed_identity(self) -> None:
        with (
            self.boot([(1, "")]) as boot,
            self.assertRaisesRegex(RuntimeError, "boot deadline"),
        ):
            android_rf_host.wait_for_initial_pid(
                "phone", self.log, 900, guard=boot.guard
            )
        self.assertLessEqual(boot.clock[0], 4.000001)
        self.assertGreater(boot.command.call_count, 1)
        self.assertTrue(all(0 < timeout <= 1 for timeout in boot.calls))
        identity = json.loads(self.log.with_suffix(".identity.json").read_text())
        self.assertEqual(identity["status"], "failed")
        self.assertIsNone(identity["pid"])
        self.assertTrue(identity["observations"])


class InstallIdentityTests(unittest.TestCase):
    def test_foreign_missing_or_duplicate_install_identity_is_rejected(self) -> None:
        package = "package: name='com.blehubnitrohost'\n"
        activity = "launchable-activity: name='com.blehubnitrohost.MainActivity'\n"
        for output in (
            activity,
            package,
            package.replace("com.blehubnitrohost", "foreign.package") + activity,
            package + activity.replace("MainActivity", "ForeignActivity"),
            package + package + activity,
            package + activity + activity,
        ):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as tmp:
                attempt = Path(tmp)
                apk = attempt / "candidate.apk"
                with (
                    patch.object(
                        android_rf_host, "sdk_root", return_value=attempt / "sdk"
                    ),
                    patch.object(
                        android_rf_host.android_sdk,
                        "run_command",
                        return_value=subprocess.CompletedProcess([], 0, output),
                    ) as command,
                    self.assertRaisesRegex(
                        android_rf_host.AndroidRfHostError, "does not match"
                    ),
                ):
                    android_rf_host.verify_install_identity(apk, attempt)
                command.assert_called_once()
                self.assertEqual(
                    json.loads(
                        (attempt / "artifact-install-identity.json").read_text()
                    )["status"],
                    "failed",
                )
                self.assertEqual((attempt / "artifact-badging.log").read_text(), output)

    def test_android_install_identity_uses_pinned_aapt2_and_retains_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = Path(tmp)
            apk = attempt / "candidate/host.apk"
            apk.parent.mkdir()
            apk.write_bytes(b"synthetic frozen APK")
            sdk = attempt / "sdk"
            build_tools = next(
                package
                for package in android_rf_host.PACKAGES
                if package.package.startswith("build-tools/")
            )
            command = [
                str(sdk / build_tools.directory / build_tools.required_files[0]),
                "dump",
                "badging",
                str(apk),
            ]
            output = (
                "package: name='com.blehubnitrohost' versionCode='1' versionName='1.0'\n"
                "sdkVersion:'24'\n"
                "launchable-activity: name='com.blehubnitrohost.MainActivity' label=''\n"
            )
            with (
                patch.object(android_rf_host, "sdk_root", return_value=sdk) as sdk_root,
                patch.object(
                    android_rf_host.android_sdk,
                    "run_command",
                    return_value=subprocess.CompletedProcess(command, 0, output),
                ) as run_command,
            ):
                android_rf_host.verify_install_identity(apk, attempt)
            sdk_root.assert_called_once_with()
            run_command.assert_called_once_with(command, timeout=30, check=True)
            self.assertEqual(
                (attempt / "artifact-badging.log").read_text(encoding="utf-8"), output
            )
            self.assertEqual(
                json.loads(
                    (attempt / "artifact-install-identity.json").read_text(
                        encoding="utf-8"
                    )
                ),
                {
                    "schema_version": 1,
                    "status": "passed",
                    "apk": "candidate/host.apk",
                    "method": "pinned-sdk-aapt2-dump-badging",
                    "package_names": ["com.blehubnitrohost"],
                    "launchable_activities": ["com.blehubnitrohost.MainActivity"],
                    "expected_package": "com.blehubnitrohost",
                    "expected_activity": "com.blehubnitrohost.MainActivity",
                },
            )
