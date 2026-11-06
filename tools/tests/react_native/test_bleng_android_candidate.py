"""Formal Android candidate tests: real temporary leases, mocked SDK subprocesses."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.android.state_control import AndroidRadioController
from tools.react_native import android_sdk
from tools.react_native.bleng_android_candidate import AndroidCandidateOwner


class AndroidCandidateTests(unittest.TestCase):
    serial = "synthetic-physical-phone"
    package = "com.blehubblenghost"
    activity = "com.blehubblenghost.MainActivity"
    run_id = "12345678-1234-4234-8234-123456789abc"
    boot = "23456789-2345-4345-8345-23456789abcd"
    installed_apk = "/data/app/~~candidate==/com.blehubblenghost-token==/base.apk"

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.apk = self.root / "candidate.apk"
        self.apk.write_bytes(b"synthetic APK bytes; aapt2 is mocked")
        self.digest = hashlib.sha256(self.apk.read_bytes()).hexdigest()
        self.clock = 100.0
        self.deadline = 160.0
        self.calls: list[list[str]] = []
        self.rows: list[tuple[int, int, str]] = []
        self.packages: list[tuple[str, int]] = []
        self.ticks = 700
        self.hook = None
        self.enterContext(patch("time.monotonic", side_effect=lambda: self.clock))
        self.enterContext(
            patch.object(android_sdk, "adb_executable", return_value=Path("/sdk/adb"))
        )
        self.sdk = self.enterContext(
            patch.object(android_sdk.subprocess, "run", side_effect=self.command)
        )
        self.phone = AndroidRadioController(
            self.serial,
            self.run_id,
            self.root / "phone.json",
            lock_root=self.root / "locks",
        )
        self.phone.acquire()
        self.driver = SimpleNamespace(
            session_id="driver-1",
            capabilities={
                "platformName": "Android",
                "appium:udid": self.serial,
                "appium:appPackage": self.package,
            },
        )

    def owner(self, **changes):
        values = {
            "apk": self.apk,
            "expected_sha256": self.digest,
            "package": self.package,
            "activity": self.activity,
            "phone": self.phone,
            "serial": self.serial,
            "run_id": self.run_id,
            "attempt": self.root / "attempt",
            "cwd": self.root,
            "deadline": 160.0,
            "test_only": True,
        }
        values.update(changes)
        return AndroidCandidateOwner(**values)

    def command(self, argv, **kwargs):
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], self.deadline - self.clock)
        self.calls.append(argv.copy())
        if argv[1:3] == ["dump", "badging"]:
            output = f"package: name='{self.package}' versionCode='1'\nlaunchable-activity: name='{self.activity}' label='' icon=''\n"
            code = 0
        elif argv[1:3] == ["dump", "xmltree"]:
            code, output = (
                0,
                'N: android=http://schemas.android.com/apk/res/android\n  E: manifest (line=1)\n    A: package="com.blehubblenghost"\n',
            )
        else:
            self.assertEqual(argv[:3], ["/sdk/adb", "-s", self.serial])
            args = argv[3:]
            code, output = 0, ""
            if args[:1] == ["push"]:
                self.assertEqual(args[1], str(self.apk))
                self.assertRegex(
                    args[2],
                    r"^/data/local/tmp/blehub-instrumentation-[0-9a-f]{32}\.apk$",
                )
            elif args[:3] == ["shell", "pm", "install"] or args[:3] == [
                "shell",
                "su",
                "-c",
            ]:
                self.assertNotIn("-g", " ".join(args).split())
                self.assertIn("-t", " ".join(args).split())
                self.packages = [(self.package, 10123)]
                output = "Success\n"
            elif args[:3] == ["shell", "rm", "-f"]:
                self.assertRegex(
                    args[3],
                    r"^/data/local/tmp/blehub-instrumentation-[0-9a-f]{32}\.apk$",
                )
            elif args[:3] == ["shell", "am", "force-stop"] and args[-1] in (
                "com.android.packageinstaller",
                "com.oplus.appdetail",
            ):
                pass
            elif args == ["get-state"]:
                output = "device\n"
            elif args == ["shell", "am", "get-current-user"]:
                output = "0\n"
            elif args == ["shell", "pm", "list", "users"]:
                output = "Users:\n\tUserInfo{0:Owner:13} running\n"
            elif args == ["shell", "pm", "list", "packages", "-U", "--user", "0"]:
                output = "".join(
                    f"package:{name} uid:{uid}\n" for name, uid in self.packages
                )
            elif args == ["shell", "ps", "-A", "-o", "PID,UID,ARGS"]:
                output = "PID UID ARGS\n" + "".join(
                    f"{pid} {uid} {name}\n" for pid, uid, name in self.rows
                )
            elif args[:3] == ["shell", "am", "start"]:
                self.assertEqual(
                    args,
                    [
                        "shell",
                        "am",
                        "start",
                        "-W",
                        "--user",
                        "0",
                        "-n",
                        self.package + "/" + self.activity,
                    ],
                )
                self.rows = [(321, 10123, self.package)]
                output = f"Starting: Intent {{ cmp={self.package}/.MainActivity }}\nStatus: ok\nLaunchState: COLD\nActivity: {self.package}/.MainActivity\nTotalTime: 100\nWaitTime: 105\nComplete\n"
            elif args == ["shell", "am", "force-stop", "--user", "0", self.package]:
                self.rows = [row for row in self.rows if row[1] != 10123]
            elif args == ["shell", "pidof", self.package]:
                matches = [
                    str(pid) for pid, _, name in self.rows if name == self.package
                ]
                code, output = (0, " ".join(matches) + "\n") if matches else (1, "")
            elif args == ["shell", "pm", "path", self.package]:
                output = "package:" + self.installed_apk + "\n"
            elif args == ["shell", "sha256sum", self.installed_apk]:
                output = self.digest + "  " + self.installed_apk + "\n"
            elif args == ["shell", "cat", "/proc/sys/kernel/random/boot_id"]:
                output = self.boot + "\n"
            elif args == ["shell", "cat", "/proc/321/stat"]:
                output = "321 (app ) worker) S " + "0 " * 18 + f"{self.ticks} 0\n"
            elif args == ["shell", "cat", "/proc/321/status"]:
                output = "Name:\tapp\nPid:\t321\nUid:\t10123\t10123\t10123\t10123\n"
            elif args == ["shell", "cat", "/proc/321/cmdline"]:
                output = self.package + "\0"
            else:
                self.fail(f"unexpected SDK command: {argv}")
        if self.hook:
            code, output = self.hook(argv, code, output)
        self.clock += 0.01
        return subprocess.CompletedProcess(argv, code, output)

    def launch(self):
        owner = self.owner()
        owner.install_and_launch()
        binding = owner.bind(self.driver, require_owner=lambda: None)
        return owner, binding

    def finish(self, owner, primary=None):
        self.deadline = 210.0
        return self.phone.finish_app_observation(
            primary, finalize_owned=lambda: owner.finalize(primary, deadline=210.0)
        )

    def prepared_scan(self):
        def sleep(seconds):
            self.clock += seconds

        self.enterContext(patch("time.sleep", side_effect=sleep))
        from tools.tests.react_native.support.formal_scan import (
            prepared_fixture,
            scan_driver,
        )

        fixture, plan, wire, events = self.enterContext(
            prepared_fixture(self.root / "fixture", self.run_id)
        )
        owner = self.owner()
        owner.install_and_launch()
        self.driver = scan_driver("android", self.serial, self.package, plan)
        self.deadline = 300.0  # Mock SDK rejects calls beyond this outer safety bound.
        self.automation, self.automation_sdk = self.managed_automation(plan)
        return owner, fixture, plan, wire, events

    def run_prepared_scan(self, owner, fixture, plan, *, automation=None):
        from tools.react_native.bleng_scan_runner import run_prepared_scan

        return run_prepared_scan(
            candidate=owner,
            phone=self.phone,
            fixture=fixture,
            plan=plan,
            driver=self.driver,
            app_id=self.package,
            output=self.root / "scan-result",
            deadline=160.0,
            cleanup_seconds=30.0,
            automation=self.automation if automation is None else automation,
        )

    def managed_automation(self, plan):
        from tools.react_native.bleng_automation import ScanAutomationOwner
        from tools.tests.react_native.support.appium_session import automation_sdk

        sdk = self.enterContext(automation_sdk(self.driver))
        # Simulate driver admission only inside this isolated downstream handoff.
        # The real prepared Android path remains blocked before Remote or spawn.
        self.enterContext(
            patch("tools.android.android_appium.require_prepared_driver_contract")
        )

        platform_held = True

        def platform_owner(deadline):
            self.assertGreater(deadline, self.clock)
            self.assertTrue(platform_held)
            self.phone.require_owner()

        def finalize_platform(deadline):
            nonlocal platform_held
            platform_owner(deadline)
            failure = getattr(self, "platform_cleanup_failure", None)
            if failure is not None:
                raise failure
            platform_held = False
            sdk.events.append("platform-finalized")

        automation = ScanAutomationOwner(
            phone=self.phone,
            run_id=plan.session_id,
            app_id=self.package,
            node="node",
            appium_entry=self.root / "installed-appium.js",
            port=18765,
            output=self.root / "automation",
            cwd=self.root,
            deadline=160.0,
            require_platform_owner=platform_owner,
            finalize_platform_owner=finalize_platform,
            activity=self.activity,
        )
        self.assertIs(automation.start(), self.driver)
        return automation, sdk

    def test_prepared_scan_with_managed_automation_reaps_server_before_phone_release(
        self,
    ):
        owner, fixture, plan, _, _ = self.prepared_scan()
        automation, sdk = self.automation, self.automation_sdk
        path = self.run_prepared_scan(owner, fixture, plan, automation=automation)
        result = json.loads(path.read_bytes())
        self.assertEqual(result["session_status"], "passed")
        self.assertEqual(result["host_gate_status"], "pending")
        self.assertEqual(result["cleanup_order"][0], "automation.close")
        self.assertEqual(sdk.events.count("session-quit"), 1)
        self.assertEqual(sdk.events.count("server-reaped"), 1)
        self.assertFalse(self.phone.lock.exists())
        automation.require_closed()

    def test_prepared_scan_does_not_release_phone_when_platform_cleanup_is_unverified(
        self,
    ):
        self.platform_cleanup_failure = RuntimeError(
            "WDA or UiAutomator2 cleanup unverified"
        )
        owner, fixture, plan, _, _ = self.prepared_scan()
        result = json.loads(self.run_prepared_scan(owner, fixture, plan).read_bytes())
        self.assertEqual(result["scenario_status"], "passed")
        self.assertEqual(result["session_status"], "failed")
        self.assertIn("cleanup unverified", " ".join(result["cleanup_errors"]))
        self.assertEqual(self.automation_sdk.events.count("server-reaped"), 1)
        self.assertTrue(self.phone.lock.exists())
        self.assertEqual(self.phone.document["status"], "quarantined")

    def test_prepared_scan_detects_managed_server_loss_without_replacing_it(self):
        owner, fixture, plan, _, _ = self.prepared_scan()
        automation, sdk = self.automation, self.automation_sdk
        original = self.driver.start.click.side_effect

        def lose():
            original()
            sdk.process.returncode = 7

        self.driver.start.click.side_effect = lose
        result = json.loads(
            self.run_prepared_scan(
                owner, fixture, plan, automation=automation
            ).read_bytes()
        )
        self.assertEqual(result["session_status"], "failed")
        self.assertIn("owned child exited", result["primary_error"])
        self.assertTrue(result["cleanup_errors"])
        self.assertTrue(self.phone.lock.exists())
        sdk.spawn.assert_called_once()
        self.assertEqual(self.sdk.call_count, len(self.calls))

    def test_prepared_scan_runs_actual_candidate_ui_fixture_and_phone_finalizers(self):
        owner, fixture, plan, wire, events = self.prepared_scan()
        path = self.run_prepared_scan(owner, fixture, plan)
        result = json.loads(path.read_bytes())
        self.assertEqual(result["scenario_status"], "passed")
        self.assertEqual(result["session_status"], "passed")
        self.assertEqual(result["host_gate_status"], "pending")
        self.assertEqual(result["cleanup_errors"], [])
        self.assertTrue(result["fixture_close"]["advertising_inactive"])
        self.assertEqual(self.driver.page, "Evidence")
        self.driver.quit.assert_called_once()
        self.assertTrue(self.rows, "success keeps the App process")
        self.assertFalse(self.phone.lock.exists())
        self.assertEqual(events[-1], "release")
        wire.gap.disconnect.assert_not_called()
        wire.gatt.reset_server.assert_not_called()
        self.assertTrue((path.parent / "prepared-result.json").is_file())
        self.assertLess(
            len(self.calls), 1000, "UI field guards must not trigger full SDK snapshots"
        )

    def test_prepared_scan_primary_error_survives_independent_cleanup(self):
        owner, fixture, plan, _, events = self.prepared_scan()
        self.driver.start.click.side_effect = RuntimeError("original UI failure")
        result = json.loads(self.run_prepared_scan(owner, fixture, plan).read_bytes())
        self.assertIn("original UI failure", result["primary_error"])
        self.assertEqual(result["session_status"], "failed")
        self.assertEqual(result["cleanup_errors"], [])
        self.assertEqual(self.rows, [])
        self.assertFalse(self.phone.lock.exists())
        self.assertEqual(events[-1], "release")
        self.assertEqual(sum("force-stop" in c for c in self.calls), 1)

    def test_prepared_scan_driver_failure_retains_success_page_and_quarantines_phone(
        self,
    ):
        owner, fixture, plan, _, events = self.prepared_scan()
        self.driver.quit.side_effect = RuntimeError("quit failed")
        result = json.loads(self.run_prepared_scan(owner, fixture, plan).read_bytes())
        self.assertEqual(result["scenario_status"], "passed")
        self.assertEqual(result["session_status"], "failed")
        self.assertIsNone(result["primary_error"])
        self.assertIn("quit failed", " ".join(result["cleanup_errors"]))
        self.assertEqual(self.phone.document["status"], "quarantined")
        self.assertTrue(self.phone.lock.exists())
        self.assertTrue(self.rows)
        self.assertEqual(events[-1], "release")

    def test_prepared_scan_fixture_failure_is_not_hidden_by_legacy_stop(self):
        owner, fixture, plan, wire, events = self.prepared_scan()
        wire.gap.stop_advertising.side_effect = RuntimeError("BTP stop failed")
        result = json.loads(self.run_prepared_scan(owner, fixture, plan).read_bytes())
        self.assertEqual(result["scenario_status"], "passed")
        self.assertEqual(result["session_status"], "failed")
        self.assertIsNone(result["fixture_close"])
        self.assertIn("BTP stop failed", " ".join(result["cleanup_errors"]))
        self.assertTrue(self.phone.lock.exists())
        count = wire.gap.stop_advertising.call_count
        fixture.stop()
        self.assertEqual(wire.gap.stop_advertising.call_count, count)
        self.assertEqual(events[-1], "release")

    def test_prepared_scan_rejects_mutating_driver_before_taking_ownership(self):
        owner, fixture, plan, _, events = self.prepared_scan()
        self.driver.capabilities["appium:shouldTerminateApp"] = True
        with self.assertRaisesRegex(RuntimeError, "attach-only"):
            self.run_prepared_scan(owner, fixture, plan)
        self.driver.quit.assert_not_called()
        self.assertTrue(self.phone.lock.exists())
        self.assertNotIn("release", events)
        fixture.require_active_session(plan)

    def test_prepared_scan_bundle_publication_failure_keeps_phone_quarantined(self):
        owner, fixture, plan, _, _ = self.prepared_scan()
        original = Path.open

        def fail(path, *args, **kwargs):
            if path.name.startswith(".prepared-result.json."):
                raise OSError("bundle publication failed")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", fail):
            result = json.loads(
                self.run_prepared_scan(owner, fixture, plan).read_bytes()
            )
        self.assertEqual(result["session_status"], "failed")
        self.assertIn("bundle publication failed", " ".join(result["cleanup_errors"]))
        self.assertTrue(self.phone.lock.exists())

    def test_prepared_scan_cleanup_interruption_is_preserved_after_other_cleanup(self):
        owner, fixture, plan, _, events = self.prepared_scan()
        self.driver.quit.side_effect = KeyboardInterrupt("quit interrupted")
        with self.assertRaises(KeyboardInterrupt):
            self.run_prepared_scan(owner, fixture, plan)
        result = json.loads((self.root / "scan-result/result.json").read_bytes())
        self.assertEqual(result["session_status"], "failed")
        self.assertEqual(result["scenario_status"], "passed")
        self.assertIn("KeyboardInterrupt", " ".join(result["cleanup_errors"]))
        self.assertEqual(events[-1], "release")
        self.assertTrue(self.phone.lock.exists())

    def test_prepared_scan_preserves_cleanup_interruption_when_final_publication_fails(
        self,
    ):
        import os

        owner, fixture, plan, _, events = self.prepared_scan()
        interruption = KeyboardInterrupt("cleanup interrupted")
        self.driver.quit.side_effect = interruption
        original = os.link

        def fail(source, destination, *args, **kwargs):
            if Path(destination).name == "result.json":
                raise OSError("final publication failed")
            return original(source, destination, *args, **kwargs)

        with (
            patch.object(os, "link", fail),
            self.assertRaises(KeyboardInterrupt) as caught,
        ):
            self.run_prepared_scan(owner, fixture, plan)
        self.assertIs(caught.exception, interruption)
        self.assertIn("final publication failed", " ".join(interruption.__notes__))
        self.assertEqual(events[-1], "release")
        self.assertTrue(self.phone.lock.exists())

    def test_prepared_scan_final_publication_failure_leaves_only_pending_result(self):
        owner, fixture, plan, _, _ = self.prepared_scan()
        import os

        original = os.link

        def fail(source, destination, *args, **kwargs):
            if Path(destination).name == "result.json":
                raise OSError("final commit failed")
            return original(source, destination, *args, **kwargs)

        with (
            patch.object(os, "link", fail),
            self.assertRaisesRegex(OSError, "final commit failed"),
        ):
            self.run_prepared_scan(owner, fixture, plan)
        self.assertFalse((self.root / "scan-result/result.json").exists())
        prepared = json.loads(
            (self.root / "scan-result/prepared-result.json").read_bytes()
        )
        self.assertEqual(prepared["session_status"], "pending")
        self.assertFalse(self.phone.lock.exists())

    def test_narrow_binding_deadline_bounds_sdk_without_revoking_cleanup(self):
        owner = self.owner()
        owner.install_and_launch()
        self.deadline = 120.0
        binding = owner.bind(self.driver, require_owner=lambda: None, deadline=120.0)
        self.clock = 121.0
        with self.assertRaises(TimeoutError):
            binding()
        self.assertEqual(self.finish(owner, TimeoutError("short scan deadline")), [])
        self.assertEqual(sum("force-stop" in c for c in self.calls), 1)

    def test_explicit_root_reuses_sdk_installer_and_freezes_install_evidence(self):
        owner = self.owner(use_root=True)
        owner.install_and_launch()
        binding = owner.bind(self.driver, require_owner=lambda: None)
        installs = [c for c in self.calls if c[3:6] == ["shell", "su", "-c"]]
        self.assertEqual(len(installs), 1)
        self.assertRegex(
            installs[0][-1],
            r"^pm install -r -t /data/local/tmp/blehub-instrumentation-[0-9a-f]{32}\.apk$",
        )
        self.assertEqual(sum(c[3:4] == ["push"] for c in self.calls), 1)
        self.assertFalse(any(c[3:4] == ["install"] for c in self.calls))
        receipt = json.loads(
            next((self.root / "attempt").glob("*-install.json")).read_bytes()
        )
        self.assertIs(receipt["use_root"], True)
        self.assertIs(receipt["grant"], False)
        self.assertEqual(receipt["apk_sha256"], self.digest)
        self.assertEqual(receipt["status"], "passed")
        log = next((self.root / "attempt").glob("*-install.log"))
        self.assertIn("su -c pm install", log.read_text())
        self.assertIn("shell rm -f", log.read_text())
        log.write_bytes(log.read_bytes() + b"forged")
        before = len(self.calls)
        with self.assertRaisesRegex(RuntimeError, "receipt changed"):
            binding()
        self.assertEqual(len(self.calls), before)

    def test_root_install_commands_all_bracket_lease_hash_and_deadline(self):
        stages = ("installer-stop", "push", "install", "remove")
        for mutation in ("lease", "apk", "receipt", "deadline"):
            for stage in stages:
                # Independent real lease and attempt per injected boundary loss.
                with (
                    self.subTest(mutation=mutation, stage=stage),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    original_root, original_phone, original_apk = (
                        self.root,
                        self.phone,
                        self.apk,
                    )
                    self.root = Path(directory)
                    self.apk = self.root / "candidate.apk"
                    self.apk.write_bytes(original_apk.read_bytes())
                    self.phone = AndroidRadioController(
                        self.serial,
                        self.run_id,
                        self.root / "phone.json",
                        lock_root=self.root / "locks",
                    )
                    self.phone.acquire()
                    self.clock = 100.0
                    self.calls, self.rows, self.packages = [], [], []

                    def lose(argv, code, output, *, stage=stage, mutation=mutation):
                        args = argv[3:]
                        match = (
                            (
                                stage == "installer-stop"
                                and args[:3] == ["shell", "am", "force-stop"]
                            )
                            or (stage == "push" and args[:1] == ["push"])
                            or (
                                stage == "install" and args[:3] == ["shell", "su", "-c"]
                            )
                            or (stage == "remove" and args[:3] == ["shell", "rm", "-f"])
                        )
                        if match:
                            if mutation == "lease":
                                (self.phone.lock / "owner.json").write_bytes(b"foreign")
                            elif mutation == "apk":
                                self.apk.write_bytes(b"changed")
                            elif mutation == "receipt":
                                next(
                                    (self.root / "attempt").glob("*-badging.json")
                                ).write_bytes(b"changed")
                            else:
                                self.clock = 161.0
                        return code, output

                    self.hook = lose
                    try:
                        owner = self.owner(use_root=True)
                        with self.assertRaises((RuntimeError, TimeoutError)):
                            owner.install_and_launch()
                        last = self.calls[-1][3:]
                        self.assertTrue(
                            (
                                stage == "installer-stop"
                                and last[:3] == ["shell", "am", "force-stop"]
                            )
                            or (stage == "push" and last[:1] == ["push"])
                            or (
                                stage == "install" and last[:3] == ["shell", "su", "-c"]
                            )
                            or (stage == "remove" and last[:3] == ["shell", "rm", "-f"])
                        )
                        self.assertFalse(any("start" in c for c in self.calls))
                    finally:
                        self.root, self.phone, self.apk = (
                            original_root,
                            original_phone,
                            original_apk,
                        )
                        self.hook = None

    def test_root_install_timeout_preserves_primary_cleanup_and_partial_log(self):
        def fail(argv, code, output):
            if argv[3:6] == ["shell", "su", "-c"]:
                raise subprocess.TimeoutExpired(argv, 2, output=b"root install pending")
            if argv[3:6] == ["shell", "rm", "-f"]:
                return 9, "upload cleanup denied\n"
            return code, output

        self.hook = fail
        owner = self.owner(use_root=True)
        with self.assertRaises(android_sdk.AndroidSdkError) as caught:
            owner.install_and_launch()
        self.assertIsInstance(caught.exception.__cause__, subprocess.TimeoutExpired)
        self.assertIn("upload cleanup denied", " ".join(caught.exception.__notes__))
        self.assertEqual(sum(c[3:6] == ["shell", "su", "-c"] for c in self.calls), 1)
        self.assertFalse(any("start" in c for c in self.calls))
        log = next((self.root / "attempt").glob("*-install.log")).read_text()
        self.assertIn("root install pending", log)
        self.assertIn("upload cleanup denied", log)
        receipt = json.loads(
            next((self.root / "attempt").glob("*-install.json")).read_bytes()
        )
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("upload cleanup denied", receipt["error"])
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            owner.install_and_launch()

    def test_root_install_failure_with_owner_loss_cannot_remove_upload(self):
        def fail(argv, code, output):
            if argv[3:6] == ["shell", "su", "-c"]:
                (self.phone.lock / "owner.json").write_bytes(b"foreign")
                raise subprocess.TimeoutExpired(argv, 2, output="partial root install")
            return code, output

        self.hook = fail
        owner = self.owner(use_root=True)
        with self.assertRaises(android_sdk.AndroidSdkError) as caught:
            owner.install_and_launch()
        self.assertIn("ownership changed", " ".join(caught.exception.__notes__))
        self.assertEqual(self.calls[-1][3:6], ["shell", "su", "-c"])
        self.assertFalse(any(c[3:6] == ["shell", "rm", "-f"] for c in self.calls))

    def test_root_optin_requires_bool_before_any_sdk_command(self):
        for value in (None, 1, "true"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.owner(use_root=value)
        self.assertEqual(self.calls, [])

    def test_upload_cleanup_failure_never_becomes_install_success(self):
        def fail(argv, code, output):
            if argv[3:6] == ["shell", "rm", "-f"]:
                return 9, "permission denied\n"
            return code, output

        self.hook = fail
        owner = self.owner(use_root=True)
        with self.assertRaisesRegex(android_sdk.AndroidSdkError, "cleanup"):
            owner.install_and_launch()
        self.assertFalse(any("start" in c for c in self.calls))
        receipt = json.loads(
            next((self.root / "attempt").glob("*-install.json")).read_bytes()
        )
        self.assertEqual(receipt["status"], "failed")
        self.assertIn(
            "permission denied",
            next((self.root / "attempt").glob("*-install.log")).read_text(),
        )

    def test_install_launch_bind_and_success_retains_app(self):
        owner, binding = self.launch()
        binding()
        self.assertEqual(self.finish(owner), [])
        self.assertTrue(self.rows)
        self.assertFalse(self.phone.lock.exists())
        self.assertFalse(any("force-stop" in c for c in self.calls))
        self.assertFalse(
            any("su" in c or "grant" in c or "-g" in c for c in self.calls)
        )
        self.assertEqual(sum("install" in c for c in self.calls), 1)
        self.assertEqual(sum("start" in c for c in self.calls), 1)
        with self.assertRaises(RuntimeError):
            binding()
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            owner.finalize(None, deadline=210.0)

    def test_expired_scenario_does_not_disable_owned_failure_cleanup(self):
        owner, binding = self.launch()
        self.clock = 161.0
        with self.assertRaises(TimeoutError):
            binding()
        self.assertEqual(self.finish(owner, TimeoutError("scan deadline")), [])
        self.assertEqual(sum("force-stop" in c for c in self.calls), 1)
        self.assertEqual(self.rows, [])
        self.assertEqual(
            self.phone.document["primary_error"], "TimeoutError: scan deadline"
        )
        self.assertEqual(self.phone.document["actions"], [])

    def test_worker_and_custom_uid_process_are_refused_before_install(self):
        self.packages = [(self.package, 10123)]
        self.rows = [(400, 10123, "custom.global.worker")]
        owner = self.owner()
        with self.assertRaisesRegex(RuntimeError, "already running"):
            owner.install_and_launch()
        self.assertFalse(any("install" in c for c in self.calls))

    def test_bad_apk_identity_fails_before_adb(self):
        self.hook = lambda argv, code, output: (
            code,
            output.replace(self.package, "com.wrong") if "badging" in argv else output,
        )
        with self.assertRaisesRegex(RuntimeError, "APK identity"):
            self.owner().install_and_launch()
        self.assertTrue(all("badging" in c for c in self.calls))

    def test_secondary_user_or_shared_uid_is_refused_before_install(self):
        self.packages = [(self.package, 10123), ("com.other", 10123)]
        with self.assertRaisesRegex(RuntimeError, "shared UID"):
            self.owner().install_and_launch()
        self.assertFalse(any("install" in c for c in self.calls))

    def test_install_failure_is_one_shot_and_never_launches(self):
        def fail(argv, code, output):
            if "install" in argv:
                raise subprocess.TimeoutExpired(argv, 5, output="partial install")
            return code, output

        self.hook = fail
        owner = self.owner()
        with self.assertRaises(android_sdk.AndroidSdkError):
            owner.install_and_launch()
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            owner.install_and_launch()
        self.assertFalse(any("start" in c or "force-stop" in c for c in self.calls))
        self.assertTrue(self.finish(owner, RuntimeError("install uncertain")))
        self.assertTrue(self.phone.lock.exists())

    def test_warm_launch_not_accepted_even_with_status_ok(self):
        self.hook = lambda argv, code, output: (code, output.replace("COLD", "HOT"))
        owner = self.owner()
        with self.assertRaisesRegex(RuntimeError, "cold launch"):
            owner.install_and_launch()
        with self.assertRaises(RuntimeError):
            owner.bind(self.driver, require_owner=lambda: None)

    def test_bind_does_not_adopt_restarted_process(self):
        owner = self.owner()
        owner.install_and_launch()
        self.ticks += 1
        with self.assertRaisesRegex(RuntimeError, "identity"):
            owner.bind(self.driver, require_owner=lambda: None)
        count = len(self.calls)
        with self.assertRaises(RuntimeError):
            owner.bind(self.driver, require_owner=lambda: None)
        self.assertEqual(len(self.calls), count)

    def test_unbound_launched_candidate_success_retains_original_app(self):
        owner = self.owner()
        owner.install_and_launch()
        before = len(self.calls)
        self.assertEqual(self.finish(owner), [])
        cleanup = self.calls[before:]
        self.assertTrue(any(c[-1] == "/proc/321/stat" for c in cleanup))
        self.assertFalse(
            any("force-stop" in c or "start" in c or "install" in c for c in cleanup)
        )
        self.assertEqual(self.rows, [(321, 10123, self.package)])
        self.assertFalse(self.phone.lock.exists())
        result = json.loads((self.root / "attempt/finalization.json").read_bytes())
        self.assertEqual((result["action"], result["status"]), ("retain", "passed"))
        self.assertEqual(result["identity"]["pid"], 321)

    def test_unbound_launched_candidate_automation_failure_stops_only_original_app(
        self,
    ):
        owner = self.owner()
        owner.install_and_launch()
        foreign = (999, 10456, "com.other.app")
        self.rows.append(foreign)
        before = len(self.calls)
        self.assertEqual(
            self.finish(owner, RuntimeError("automation.start failed")), []
        )
        cleanup = self.calls[before:]
        stops = [c for c in cleanup if "force-stop" in c]
        self.assertEqual(
            stops,
            [
                [
                    "/sdk/adb",
                    "-s",
                    self.serial,
                    "shell",
                    "am",
                    "force-stop",
                    "--user",
                    "0",
                    self.package,
                ]
            ],
        )
        identity_read = next(
            i for i, c in enumerate(cleanup) if c[-1] == "/proc/321/stat"
        )
        self.assertLess(identity_read, cleanup.index(stops[0]))
        self.assertFalse(any("start" in c or "install" in c for c in cleanup))
        self.assertEqual(self.rows, [foreign])
        self.assertFalse(self.phone.lock.exists())
        self.assertEqual(
            self.phone.document["primary_error"],
            "RuntimeError: automation.start failed",
        )
        result = json.loads((self.root / "attempt/finalization.json").read_bytes())
        self.assertEqual((result["action"], result["status"]), ("force-stop", "passed"))
        self.assertEqual(result["identity"]["pid"], 321)

    def test_rejected_install_before_launch_has_no_device_teardown(self):
        owner = self.owner()
        self.hook = lambda argv, code, output: (
            (1, "rejected\n") if argv[1:3] == ["dump", "badging"] else (code, output)
        )
        with self.assertRaises(android_sdk.AndroidSdkError):
            owner.install_and_launch()
        before = len(self.calls)
        self.assertEqual(self.finish(owner, RuntimeError("startup rejected")), [])
        self.assertEqual(len(self.calls), before)
        result = json.loads((self.root / "attempt/finalization.json").read_text())
        self.assertEqual(result["action"], "no-launch")
        self.assertEqual(result["status"], "passed")
        self.assertFalse(self.phone.lock.exists())

    def test_unlaunched_candidate_cannot_adopt_identity_for_cleanup(self):
        owner = self.owner()
        self.rows = [(321, 10123, self.package)]
        self.assertTrue(self.finish(owner, RuntimeError("startup failed")))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows, [(321, 10123, self.package)])
        self.assertTrue(self.phone.lock.exists())

    def test_unbound_launch_without_known_os_identity_refuses_cleanup(self):
        def unknown(argv, code, output):
            if argv[3:5] == ["shell", "pidof"]:
                raise subprocess.TimeoutExpired(argv, 2)
            return code, output

        self.hook = unknown
        owner = self.owner()
        with self.assertRaises(android_sdk.AndroidSdkError):
            owner.install_and_launch()
        self.hook = None
        before = len(self.calls)
        self.assertTrue(self.finish(owner, RuntimeError("identity unverified")))
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.rows, [(321, 10123, self.package)])
        self.assertTrue(self.phone.lock.exists())

    def test_unbound_non_cold_launch_refuses_cleanup_adoption(self):
        self.hook = lambda argv, code, output: (code, output.replace("COLD", "HOT"))
        owner = self.owner()
        with self.assertRaisesRegex(RuntimeError, "cold launch"):
            owner.install_and_launch()
        before = len(self.calls)
        self.assertTrue(self.finish(owner, RuntimeError("not a fresh launch")))
        self.assertEqual(len(self.calls), before)
        self.assertTrue(self.phone.lock.exists())

    def test_unbound_cleanup_refuses_restarted_original_pid(self):
        owner = self.owner()
        owner.install_and_launch()
        self.ticks += 1
        before = len(self.calls)
        errors = self.finish(owner, RuntimeError("automation.start failed"))
        self.assertTrue(errors)
        self.assertIn("start_ticks changed (restart/PID reuse)", " ".join(errors))
        self.assertFalse(
            any(
                "force-stop" in c or "start" in c or "install" in c
                for c in self.calls[before:]
            )
        )
        self.assertTrue(self.phone.lock.exists())

    def test_unbound_cleanup_requires_immutable_launch_receipt(self):
        owner = self.owner()
        owner.install_and_launch()
        receipt = next((self.root / "attempt").glob("*-launch.json"))
        receipt.write_bytes(receipt.read_bytes() + b" ")
        before = len(self.calls)
        errors = self.finish(owner, RuntimeError("automation.start failed"))
        self.assertIn("receipt changed", " ".join(errors))
        self.assertEqual(len(self.calls), before)
        self.assertTrue(self.phone.lock.exists())

    def test_unbound_cleanup_requires_original_launch_log(self):
        owner = self.owner()
        owner.install_and_launch()
        next((self.root / "attempt").glob("*-launch.log")).unlink()
        before = len(self.calls)
        self.assertTrue(self.finish(owner, RuntimeError("automation.start failed")))
        self.assertEqual(len(self.calls), before)
        self.assertTrue(self.phone.lock.exists())

    def test_cleanup_refuses_pid_reuse_and_keeps_primary_error(self):
        owner, _ = self.launch()
        self.ticks += 1
        self.assertTrue(self.finish(owner, ValueError("UI failed")))
        self.assertFalse(any("force-stop" in c for c in self.calls))
        self.assertEqual(self.phone.document["primary_error"], "ValueError: UI failed")
        self.assertEqual(self.phone.document["status"], "quarantined")
        self.assertTrue(self.phone.lock.exists())

    def test_restored_receipt_cannot_restore_invalidated_authority(self):
        owner, binding = self.launch()
        receipt = next((self.root / "attempt").glob("*-install.json"))
        original = receipt.read_bytes()
        receipt.write_bytes(original + b" ")
        with self.assertRaises(RuntimeError):
            binding()
        receipt.write_bytes(original)
        before = len(self.calls)
        self.assertTrue(self.finish(owner, RuntimeError("receipt drift")))
        self.assertEqual(len(self.calls), before)
        self.assertTrue(self.phone.lock.exists())

    def test_detected_installed_identity_drift_cannot_be_repaired_for_cleanup(self):
        owner, binding = self.launch()
        original = self.digest
        self.digest = "cd" * 32
        with self.assertRaises(RuntimeError):
            binding()
        self.digest = original
        before = len(self.calls)
        self.assertTrue(self.finish(owner, RuntimeError("observed identity drift")))
        self.assertEqual(len(self.calls), before)
        self.assertTrue(self.phone.lock.exists())

    def test_bound_identity_remains_frozen(self):
        owner, binding = self.launch()
        identity = self.root / "attempt/bound-identity.json"
        original = identity.read_bytes()
        identity.write_bytes(original + b" ")
        with self.assertRaises(RuntimeError):
            binding()
        identity.write_bytes(original)
        before = len(self.calls)
        self.assertTrue(self.finish(owner, RuntimeError("identity evidence drift")))
        self.assertEqual(len(self.calls), before)

    def test_worker_name_even_without_package_mapping_is_refused(self):
        self.rows = [(999, 10123, self.package + ":worker")]
        with self.assertRaisesRegex(RuntimeError, "already running"):
            self.owner().install_and_launch()
        self.assertFalse(any("install" in c for c in self.calls))

    def test_multiple_users_are_refused_before_install(self):
        def users(argv, code, output):
            if argv[-3:] == ["pm", "list", "users"]:
                output += "\tUserInfo{10:Work:30} running\n"
            return code, output

        self.hook = users
        with self.assertRaisesRegex(RuntimeError, "single user"):
            self.owner().install_and_launch()
        self.assertFalse(any("install" in c for c in self.calls))

    def test_manifest_shared_uid_rejected_before_device_commands(self):
        def shared(argv, code, output):
            if "xmltree" in argv:
                output += '    A: android:sharedUserId(0x0101000b)="com.other"\n'
            return code, output

        self.hook = shared
        with self.assertRaisesRegex(RuntimeError, "shared UID"):
            self.owner().install_and_launch()
        self.assertTrue(all("dump" in c for c in self.calls))

    def test_installed_hash_mismatch_never_launches(self):
        def wrong_hash(argv, code, output):
            if "sha256sum" in argv:
                output = output.replace(self.digest, "cd" * 32)
            return code, output

        self.hook = wrong_hash
        with self.assertRaisesRegex(RuntimeError, "before launch"):
            self.owner().install_and_launch()
        self.assertFalse(any("start" in c for c in self.calls))

    def test_nonzero_install_keeps_sdk_failure_when_publication_also_fails(self):
        owner = self.owner()
        self.hook = lambda argv, code, output: (
            (7, "Failure [INSTALL_FAILED]\n") if "install" in argv else (code, output)
        )
        original_open = Path.open

        def fail_receipt(path, *args, **kwargs):
            if path.name.endswith("-install.json"):
                raise OSError("receipt storage unavailable")
            return original_open(path, *args, **kwargs)

        with (
            patch.object(Path, "open", fail_receipt),
            self.assertRaisesRegex(
                android_sdk.AndroidSdkError, "exit code 7"
            ) as caught,
        ):
            owner.install_and_launch()
        self.assertIn(
            "receipt storage unavailable", " ".join(caught.exception.__notes__)
        )
        self.assertFalse(any("start" in c for c in self.calls))

    def test_cleanup_deadline_does_not_allow_more_commands(self):
        owner, _ = self.launch()

        def expire(argv, code, output):
            if "force-stop" in argv:
                self.clock = 211.0
            return code, output

        self.hook = expire
        self.assertTrue(self.finish(owner, RuntimeError("original")))
        self.assertIn("force-stop", self.calls[-1])
        self.assertEqual(sum("force-stop" in c for c in self.calls), 1)
        self.assertTrue(self.phone.lock.exists())

    def test_empty_stdout_log_is_not_an_integrity_gap(self):
        owner, binding = self.launch()
        path = next(
            path
            for path in (self.root / "attempt").glob("*.log")
            if path.stat().st_size == 0
        )
        path.write_bytes(b"forged stdout")
        with self.assertRaisesRegex(RuntimeError, "receipt changed"):
            binding()
        count = len(self.calls)
        self.assertTrue(self.finish(owner, RuntimeError("changed empty log")))
        self.assertEqual(len(self.calls), count)

    def test_identity_sdk_failure_and_owner_loss_are_both_retained(self):
        owner, _ = self.launch()

        def lose(argv, code, output):
            if argv[-1] == "/proc/321/stat":
                (self.phone.lock / "owner.json").write_bytes(b"foreign")
                raise subprocess.TimeoutExpired(argv, 5)
            return code, output

        self.hook = lose
        errors = self.finish(owner, ValueError("UI primary"))
        self.assertIn("AndroidSdkError", " ".join(errors))
        self.assertIn("ownership changed", " ".join(errors))
        self.assertEqual(self.phone.document["primary_error"], "ValueError: UI primary")
        self.assertFalse(any("force-stop" in c for c in self.calls))
        self.assertEqual((self.phone.lock / "owner.json").read_bytes(), b"foreign")

    def test_cleanup_timeout_is_not_retried(self):
        owner, _ = self.launch()

        def fail(argv, code, output):
            if "force-stop" in argv:
                raise subprocess.TimeoutExpired(argv, 5)
            return code, output

        self.hook = fail
        self.assertTrue(self.finish(owner, RuntimeError("primary")))
        self.assertEqual(sum("force-stop" in c for c in self.calls), 1)
        self.assertTrue(self.phone.lock.exists())
        self.assertEqual(self.phone.document["status"], "quarantined")

    def test_failure_cleanup_verifies_all_package_processes_are_gone(self):
        owner, _ = self.launch()

        def survive(argv, code, output):
            if "force-stop" in argv:
                self.rows = [(500, 10123, self.package + ":worker")]
            return code, output

        self.hook = survive
        self.assertTrue(self.finish(owner, RuntimeError("primary")))
        self.assertEqual(sum("force-stop" in c for c in self.calls), 1)
        self.assertTrue(self.phone.lock.exists())
