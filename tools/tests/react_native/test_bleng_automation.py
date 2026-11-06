"""Managed Appium session ownership, without an actual server or device."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.android.state_control import AndroidRadioController
from tools.react_native.bleng_automation import ScanAutomationOwner
from tools.react_native.ios_radio_lease import SCAN_OBSERVATION_SCOPE, IosRadioLease
from tools.tests.react_native.support.appium_session import automation_sdk


class AutomationOwnerTests(unittest.TestCase):
    run_id = "12345678-1234-4234-8234-123456789abc"

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = 100.0
        self.enterContext(patch("time.monotonic", side_effect=lambda: self.clock))
        self.sdk = self.enterContext(automation_sdk())
        self.platform_active = True
        self.platform_checks = []

    def platform_guard(self, deadline):
        self.platform_checks.append(deadline)
        if not self.platform_active:
            raise RuntimeError("platform owner lost")

    def platform_finalize(self, deadline):
        self.platform_guard(deadline)
        self.platform_active = False
        self.sdk.events.append("platform-finalized")

    def owner(self, platform="android", *, simulate_driver_contract=True, **changes):
        if platform == "ios":
            phone = IosRadioLease(
                "00008110-001234567890ABCD",
                self.run_id,
                self.root / "phone.json",
                lock_root=self.root / "locks",
                scope=SCAN_OBSERVATION_SCOPE,
            )
        else:
            phone = AndroidRadioController(
                "synthetic-phone",
                self.run_id,
                self.root / "phone.json",
                lock_root=self.root / "locks",
            )
        phone.acquire()
        self.phone = phone
        if platform == "android" and simulate_driver_contract:
            # Isolate downstream ownership from the currently blocked real driver.
            # Device-facing Remote/Popen/socket are mocked by automation_sdk above.
            self.enterContext(
                patch("tools.android.android_appium.require_prepared_driver_contract")
            )
        args = {
            "phone": phone,
            "run_id": self.run_id,
            "app_id": "example.formal",
            "node": "node",
            "appium_entry": self.root / "installed-appium.js",
            "port": 18765,
            "output": self.root / "automation",
            "cwd": self.root,
            "deadline": 160.0,
            "require_platform_owner": self.platform_guard,
            "finalize_platform_owner": self.platform_finalize,
            "activity": ".MainActivity" if platform == "android" else None,
            "wda_url": "http://127.0.0.1:18101" if platform == "ios" else None,
        }
        args.update(changes)
        return ScanAutomationOwner(**args)

    def test_unreviewed_android_driver_is_blocked_before_acquiring_automation(self):
        from tools.mobile_appium import AppiumError

        owner = self.owner(simulate_driver_contract=False)
        with self.assertRaisesRegex(AppiumError, "prepared.*UiAutomator2"):
            owner.start()
        self.assertEqual(self.platform_checks, [])
        self.sdk.spawn.assert_not_called()
        self.sdk.remote.assert_not_called()
        self.sdk.probe.assert_not_called()
        failed = json.loads((self.root / "automation/start-failed.json").read_bytes())
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["session_id"])
        self.assertTrue(self.phone.lock.exists())
        owner.close(deadline=180.0)
        self.assertTrue(self.platform_active)
        with self.assertRaises(RuntimeError):
            owner.require_closed()
        with self.assertRaises(RuntimeError):
            owner.start()
        self.sdk.remote.assert_not_called()
        self.assertEqual(self.phone.document["actions"], [])

    def test_android_admission_remains_primary_when_failure_evidence_cannot_publish(
        self,
    ):
        from tools.mobile_appium import AppiumError

        owner = self.owner(simulate_driver_contract=False)
        with (
            patch(
                "tools.react_native.bleng_automation.publish_new_json",
                side_effect=OSError("evidence disk unavailable"),
            ),
            self.assertRaisesRegex(AppiumError, "prepared.*UiAutomator2") as raised,
        ):
            owner.start()
        self.assertIn("evidence disk unavailable", " ".join(raised.exception.__notes__))
        self.sdk.spawn.assert_not_called()
        self.sdk.remote.assert_not_called()
        self.assertEqual(self.platform_checks, [])
        owner.close(deadline=180.0)
        self.assertTrue(self.platform_active)
        self.assertTrue(self.phone.lock.exists())
        with self.assertRaises(RuntimeError):
            owner.require_closed()

    def test_android_owns_exact_server_session_and_closes_before_lease_release(self):
        owner = self.owner()
        driver = owner.start()
        owner.require_handoff(
            driver=driver,
            phone=self.phone,
            run_id=self.run_id,
            app_id="example.formal",
            deadline=150.0,
        )
        owner.require_running(deadline=150.0)
        self.assertTrue(all(value <= 160 for value in self.platform_checks))
        self.assertIn("--base-path", self.sdk.spawn.call_args.args[0])
        self.assertIs(driver.capabilities["appium:autoLaunch"], False)
        self.assertIs(driver.capabilities["appium:skipServerInstallation"], True)
        self.assertIs(driver.capabilities["appium:skipDeviceInitialization"], True)
        self.assertTrue(self.phone.lock.exists())
        owner.close(deadline=180.0)
        self.assertLess(
            self.sdk.events.index("session-quit"),
            self.sdk.events.index("server-signal"),
        )
        self.assertEqual(self.sdk.events.count("server-reaped"), 1)
        self.assertTrue(
            self.phone.lock.exists(), "automation owner never releases the phone"
        )
        result = json.loads((self.root / "automation/close.json").read_bytes())
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["platform_backend_cleanup"], "passed")
        self.assertEqual(self.sdk.events.count("platform-finalized"), 1)
        with self.assertRaises(RuntimeError):
            owner.require_running(deadline=180.0)
        with self.assertRaises(RuntimeError):
            owner.start()

    def test_explicit_debug_logging_reaches_owned_server_without_changing_attach_caps(
        self,
    ):
        owner = self.owner(debug_logging=True)
        driver = owner.start()
        args = self.sdk.spawn.call_args.args[0]
        self.assertEqual(args[args.index("--log-level") + 1], "debug")
        self.assertIn("--log-timestamp", args)
        self.assertIs(driver.capabilities["appium:autoLaunch"], False)
        self.assertIs(driver.capabilities["appium:skipLogcatCapture"], False)
        owner.close(deadline=180.0)
        self.sdk.remote.assert_called_once()

    def test_invalid_debug_logging_rejected_before_server_or_device_io(self):
        with self.assertRaises(ValueError):
            self.owner(debug_logging="debug")
        self.sdk.spawn.assert_not_called()
        self.sdk.remote.assert_not_called()
        self.sdk.probe.assert_not_called()
        self.assertEqual(self.platform_checks, [])

    def test_platform_finalization_failure_is_not_a_local_server_success(self):
        def fail(deadline):
            raise RuntimeError("platform cleanup unverified")

        owner = self.owner(finalize_platform_owner=fail)
        owner.start()
        with self.assertRaisesRegex(RuntimeError, "platform cleanup unverified"):
            owner.close(deadline=180.0)
        self.assertEqual(self.sdk.events.count("server-reaped"), 1)
        with self.assertRaises(RuntimeError):
            owner.require_closed()

    def test_ios_uses_existing_wda_without_build_install_or_restart(self):
        owner = self.owner("ios")
        driver = owner.start()
        caps = driver.capabilities
        self.assertEqual(caps["appium:webDriverAgentUrl"], "http://127.0.0.1:18101")
        for key in ("xcodeOrgId", "derivedDataPath", "usePrebuiltWDA", "app"):
            self.assertNotIn("appium:" + key, caps)
        owner.close(deadline=180.0)
        self.assertEqual(self.sdk.remote.call_count, 1)

    def test_platform_guard_has_no_permissive_default(self):
        with self.assertRaises(TypeError):
            self.owner(require_platform_owner=None)
        self.sdk.spawn.assert_not_called()
        self.sdk.remote.assert_not_called()

    def test_platform_finalizer_is_required_before_any_remote_request(self):
        with self.assertRaises(TypeError):
            self.owner(finalize_platform_owner=None)
        self.sdk.spawn.assert_not_called()
        self.sdk.remote.assert_not_called()

    def test_startup_owner_guard_receives_the_server_budget_not_the_longer_scenario(
        self,
    ):
        owner = self.owner()
        owner.start()
        self.assertLessEqual(self.platform_checks[0], 130.0)
        owner.close(deadline=180.0)

    def test_invalid_wda_configuration_fails_before_creating_local_or_remote_resources(
        self,
    ):
        with self.assertRaises(ValueError):
            self.owner("ios", wda_url="http://127.0.0.1")
        self.sdk.spawn.assert_not_called()
        self.sdk.remote.assert_not_called()

    def test_invalid_android_package_is_rejected_before_server_or_remote_creation(self):
        with self.assertRaisesRegex(ValueError, "Android package"):
            self.owner(app_id="example.invalid-package")
        self.sdk.spawn.assert_not_called()
        self.sdk.remote.assert_not_called()

    def test_guard_loss_before_creation_never_creates_remote_session(self):
        owner = self.owner()
        self.platform_active = False
        with self.assertRaisesRegex(RuntimeError, "platform owner lost"):
            owner.start()
        self.platform_active = True
        with self.assertRaises(RuntimeError):
            owner.start()
        owner.close(deadline=180.0)
        self.sdk.spawn.assert_not_called()
        self.sdk.remote.assert_not_called()

    def test_creation_timeout_keeps_unknown_remote_outcome_and_reaps_local_server(self):
        owner = self.owner()
        self.sdk.remote.side_effect = TimeoutError("session request timeout")
        with self.assertRaisesRegex(TimeoutError, "session request timeout"):
            owner.start()
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            owner.close(deadline=180.0)
        self.assertEqual(self.sdk.remote.call_count, 1)
        self.assertEqual(self.sdk.events.count("server-reaped"), 1)
        self.assertTrue(self.phone.lock.exists())
        count = self.sdk.kill.call_count
        with self.assertRaises(RuntimeError):
            owner.close(deadline=190.0)
        self.assertEqual(self.sdk.kill.call_count, count)

    def test_lost_platform_authority_refuses_remote_quit_but_reaps_own_server(self):
        owner = self.owner()
        driver = owner.start()
        self.platform_active = False
        with self.assertRaisesRegex(RuntimeError, "platform owner lost"):
            owner.require_running(deadline=150.0)
        self.platform_active = True
        with self.assertRaises(RuntimeError):
            owner.close(deadline=180.0)
        driver.quit.assert_not_called()
        self.assertEqual(self.sdk.events.count("server-reaped"), 1)

    def test_driver_failure_does_not_skip_local_server_cleanup(self):
        owner = self.owner()
        driver = owner.start()
        driver.quit.side_effect = RuntimeError("DELETE failed")
        with self.assertRaisesRegex(RuntimeError, "DELETE failed"):
            owner.close(deadline=180.0)
        self.assertEqual(self.sdk.events.count("server-reaped"), 1)
        self.assertEqual(driver.quit.call_count, 1)
        with self.assertRaises(RuntimeError):
            owner.close(deadline=190.0)
        self.assertEqual(driver.quit.call_count, 1)

    def test_session_replacement_is_not_quit_or_adopted(self):
        owner = self.owner()
        driver = owner.start()
        driver.session_id = "foreign-session"
        with self.assertRaises(RuntimeError):
            owner.close(deadline=180.0)
        driver.quit.assert_not_called()
        self.assertEqual(self.sdk.events.count("server-reaped"), 1)

    def test_observed_driver_drift_cannot_be_restored_to_resume_authority(self):
        owner = self.owner()
        driver = owner.start()
        original = driver.session_id
        driver.session_id = "foreign"
        with self.assertRaises(RuntimeError):
            owner.require_running(deadline=150.0)
        driver.session_id = original
        with self.assertRaises(RuntimeError):
            owner.close(deadline=180.0)
        driver.quit.assert_not_called()
        self.assertEqual(self.sdk.events.count("server-reaped"), 1)

    def test_start_and_close_evidence_are_checked_before_release(self):
        owner = self.owner()
        owner.start()
        owner.close(deadline=180.0)
        path = self.root / "automation/close.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(RuntimeError, "evidence changed"):
            owner.require_closed()

    def test_wrong_handoff_arguments_do_not_transfer_ownership(self):
        owner = self.owner()
        driver = owner.start()
        with self.assertRaises(RuntimeError):
            owner.require_handoff(
                driver=driver,
                phone=self.phone,
                run_id=self.run_id,
                app_id="foreign.app",
                deadline=150.0,
            )
        owner.require_running(deadline=150.0)
        driver.quit.assert_not_called()
        owner.close(deadline=180.0)

    def test_expired_scenario_allows_independent_bounded_local_cleanup(self):
        owner = self.owner()
        driver = owner.start()
        self.clock = 161.0
        with self.assertRaises(TimeoutError):
            owner.require_running(deadline=160.0)
        owner.close(deadline=180.0)
        driver.quit.assert_called_once()
        self.assertEqual(self.sdk.events.count("server-reaped"), 1)

    def test_post_create_deadline_failure_keeps_exact_known_session_for_cleanup(self):
        owner = self.owner()
        original = self.sdk.remote.side_effect

        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.clock = 161.0
            return result

        self.sdk.remote.side_effect = delayed
        with self.assertRaises(TimeoutError):
            owner.start()
        owner.close(deadline=180.0)
        self.sdk.created[0].quit.assert_called_once()

    def test_close_publication_error_remains_failure_after_process_is_reaped(self):
        owner = self.owner()
        owner.start()
        original = Path.open

        def fail(path, *args, **kwargs):
            if path.name.startswith(".close.json."):
                raise OSError("close evidence failed")
            return original(path, *args, **kwargs)

        with (
            patch.object(Path, "open", fail),
            self.assertRaisesRegex(RuntimeError, "close evidence failed"),
        ):
            owner.close(deadline=180.0)
        self.assertEqual(self.sdk.events.count("server-reaped"), 1)
        with self.assertRaises(RuntimeError):
            owner.close(deadline=190.0)
