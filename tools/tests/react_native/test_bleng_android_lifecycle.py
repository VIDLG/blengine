"""Managed lifecycle composition over real owners with strict SDK/HTTP boundaries."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from selenium.common.exceptions import NoSuchElementException

from tools.mobile_appium import AppiumError
from tools.react_native import bleng_android_lifecycle as runner
from tools.react_native.bleng_lifecycle_evidence import verify_lifecycle_bundle
from tools.tests.react_native.support.android_automation import APPOPS, PS
from tools.tests.react_native.support.android_lifecycle import LifecycleHarness
from tools.tests.react_native.test_bleng_android_preparation import (
    DELETE,
    DENY,
    READ,
)


class AndroidLifecycleTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.h = self.enterContext(LifecycleHarness(root / "harness"))

    def run_attempt(self, **changes):
        path = runner.run_android_lifecycle(**(self.h.arguments() | changes))
        self.assertEqual(path.name, "result.json")
        result = verify_lifecycle_bundle(path.parent)
        self.assertEqual(result, json.loads(path.read_bytes()))
        self.assertEqual(result["rf"], "not_run")
        self.assertIn(result["host_gate_status"], ("pending", "failed"))
        return result, path.parent

    def assert_released(self):
        self.assertFalse(self.h.last_phone.lock.exists())
        self.assertEqual(self.h.last_phone.document["actions"], [])

    def test_complete_fake_sdk_flow_preserves_evidence_app_and_owner_receipts(self):
        self.h.driver.snapshot_nodes["adapter-state"].text = "poweredOff"
        self.h.driver.snapshot_nodes["scan-authorization"].text = "denied"
        result, attempt = self.run_attempt()
        self.assertEqual(result["startup_snapshot"]["state"], "poweredOff")
        self.assertEqual(result["startup_snapshot"]["scan_authorization"], "denied")
        self.assertEqual(result["status"], "passed")
        args = self.h.sdk.spawn.call_args.args[0]
        self.assertEqual(args[args.index("--log-level") + 1], "info")
        self.assertNotIn("--log-timestamp", args)
        self.assertFalse(any(call[:1] == ("logcat",) for call in self.h.calls))
        self.assertEqual(self.h.adb_calls, [])
        self.assertEqual(result["scenario_status"], "passed")
        self.assertEqual(result["session_status"], "passed")
        self.assertEqual(result["phone_status"], "passed")
        self.assertEqual(result["host_gate_status"], "pending")
        self.assertEqual(
            result["cleanup_order"],
            [
                "automation.close",
                "candidate.finalize",
                "bundle.prepare",
                "phone.finish",
            ],
        )
        self.assertEqual(self.h.driver.page, "Evidence")
        self.assertTrue(self.h.app_running)
        self.h.driver.scan_start.click.assert_not_called()
        self.assertEqual(self.h.calls.count(DELETE), 1)
        self.assertEqual(self.h.calls.count(DENY), 1)
        self.assertEqual(self.h.replies[READ], "absent\n")
        self.assertEqual(self.h.replies[APPOPS], "MOCK_LOCATION: deny\n")
        self.assertEqual(
            (attempt / "candidate.apk").read_bytes(), self.h.source.read_bytes()
        )
        for name in (
            "candidate/finalization.json",
            "platform/finalized.json",
            "helpers/restored.json",
            "automation/close.json",
            "phone.json",
            "prepared-result.json",
            "manifest.json",
        ):
            self.assertTrue((attempt / name).is_file(), name)
        self.assert_released()

    def test_live_capture_precedes_start_and_stops_before_session_delete(self):
        def before_start():
            self.assertIn("live-logcat-start", self.h.sdk.events)
            self.assertNotIn("live-logcat-stop", self.h.sdk.events)

        original = self.h.driver.start.click.side_effect

        def start_once():
            before_start()
            return original()

        self.h.driver.start.click.side_effect = start_once
        result, attempt = self.run_attempt(close_diagnostics=True)
        self.assertEqual(result["status"], "passed")
        self.assertLess(
            self.h.sdk.events.index("live-logcat-stop"),
            self.h.sdk.events.index("session-quit"),
        )
        capture = json.loads(
            (attempt / "platform/close-diagnostics/live-stop.json").read_bytes()
        )
        self.assertEqual(capture["run_id"], result["run_id"])
        self.assertIn("not request arrival", capture["limitation"])
        self.assertEqual(capture["host_pid"], 9876)
        self.assertTrue(
            (attempt / "platform/close-diagnostics/live-logcat.raw").is_file()
        )
        self.assert_released()

    def test_live_start_failure_reaps_locally_and_keeps_primary_before_ui(self):
        self.h.live_start_error = RuntimeError("live stream refused")
        result, _ = self.run_attempt(close_diagnostics=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["primary_error"], "RuntimeError: live stream refused")
        self.assertEqual(result["observations"], [])
        self.h.driver.start.click.assert_not_called()
        self.assertIn("session-quit", self.h.sdk.events)
        self.assertEqual(self.h.diagnostics_value, "INFO")
        self.assertFalse(self.h.app_running)
        self.assert_released()

    def test_idle_control_uses_distinct_scope_without_start_or_close(self):
        result, attempt = self.run_attempt(
            close_diagnostics=True, automation_idle_control=True
        )
        self.assertEqual(result["scope"], "formal-android-managed-idle-control")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["observations"], ["idle"])
        self.assertEqual(result["startup_snapshot"], {})
        self.assertEqual(result["host_gate_status"], "pending")
        self.assertEqual(self.h.driver.page, "Evidence")
        self.h.driver.start.click.assert_not_called()
        self.h.driver.close.click.assert_not_called()
        self.h.driver.scan_start.click.assert_not_called()
        self.h.driver.get_screenshot_as_png.assert_not_called()
        self.assertFalse(
            (attempt / "platform/close-diagnostics/before-close.json").exists()
        )
        self.assertTrue(self.h.app_running)
        self.assertEqual(self.h.replies[READ], "absent\n")
        self.assert_released()

    def test_start_hold_control_reuses_managed_owners_and_preclose_diagnostics(self):
        result, attempt = self.run_attempt(
            close_diagnostics=True, automation_start_hold_control=True, phase_timeout=10
        )
        self.assertEqual(result["scope"], "formal-android-managed-start-hold-control")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["observations"], ["idle", "started", "closed"])
        self.assertEqual(result["host_gate_status"], "pending")
        self.assertEqual(self.h.driver.page, "Current run")
        self.h.driver.start.click.assert_called_once_with()
        self.h.driver.close.click.assert_called_once_with()
        for node in (*self.h.driver.navigation.values(), self.h.driver.scan_start):
            node.click.assert_not_called()
        for identifier in self.h.driver.snapshot_nodes:
            self.assertEqual(self.h.driver.trace.count(f"text:{identifier}"), 1)
        proof = json.loads(
            (attempt / "automation/start-hold-control.json").read_bytes()
        )
        self.assertEqual(proof["scope"], result["scope"])
        self.assertEqual(proof["run_id"], result["run_id"])
        self.assertIs(proof["browse"], False)
        window = proof["hold_observation"]
        self.assertGreaterEqual(
            window["finished_monotonic"] - window["started_monotonic"], 5
        )
        self.h.driver.get_screenshot_as_png.assert_called_once_with()
        self.assertTrue(
            (attempt / "platform/close-diagnostics/before-close.json").is_file()
        )
        self.assertFalse((attempt / "platform/server-status.json").exists())
        self.assertTrue(self.h.app_running)
        self.assertEqual(self.h.diagnostics_value, "INFO")
        self.assert_released()

    def test_start_hold_insufficient_budget_keeps_failure_and_normal_cleanup(self):
        result, attempt = self.run_attempt(
            close_diagnostics=True, automation_start_hold_control=True, phase_timeout=4
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("quiet window", result["primary_error"])
        self.assertEqual(result["observations"], ["idle", "started"])
        self.assertEqual(result["scope"], "formal-android-managed-start-hold-control")
        proof = json.loads(
            (attempt / "automation/start-hold-control.json").read_bytes()
        )
        self.assertNotIn("finished_monotonic", proof["hold_observation"])
        self.h.driver.close.click.assert_not_called()
        self.h.driver.get_screenshot_as_png.assert_not_called()
        self.assertEqual(self.h.http_calls.count((self.h.system_port, "/status")), 1)
        self.assertEqual(self.h.diagnostics_value, "INFO")
        self.assertFalse(self.h.app_running)
        self.assert_released()

    def test_start_hold_record_failure_preserves_ui_success_but_quarantines_cleanup(
        self,
    ):
        publish = runner.publish_new_json

        def fail_record(path, value):
            if path.name == "start-hold-control.json":
                raise OSError("scenario record unavailable")
            return publish(path, value)

        with patch.object(runner, "publish_new_json", side_effect=fail_record):
            result, _ = self.run_attempt(
                close_diagnostics=True,
                automation_start_hold_control=True,
                phase_timeout=10,
            )
        self.assertEqual(result["scenario_status"], "passed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phone_status"], "quarantined")
        self.assertTrue(
            any(
                "scenario record unavailable" in error
                for error in result["cleanup_errors"]
            )
        )
        self.assertEqual(self.h.diagnostics_value, "INFO")
        self.assertTrue(self.h.app_running)
        self.assertTrue(self.h.last_phone.lock.exists())

    def test_start_hold_failure_and_interrupt_keep_scope_and_fresh_probe_before_delete(
        self,
    ):
        for failure in (TimeoutError("hold primary"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                # Each attempt has independently owned SDK/HTTP/phone boundaries.
                root = Path(self.enterContext(tempfile.TemporaryDirectory()))
                with LifecycleHarness(root / "harness") as harness:
                    arguments = harness.arguments() | {
                        "close_diagnostics": True,
                        "automation_start_hold_control": True,
                        "phase_timeout": 10,
                    }
                    harness.driver.get_screenshot_as_png.side_effect = failure
                    if isinstance(failure, KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            runner.run_android_lifecycle(**arguments)
                        path = (
                            arguments["output_root"]
                            / arguments["run_id"]
                            / "result.json"
                        )
                    else:
                        path = runner.run_android_lifecycle(**arguments)
                    result = verify_lifecycle_bundle(path.parent)
                    self.assertEqual(
                        result["scope"], "formal-android-managed-start-hold-control"
                    )
                    self.assertEqual(result["status"], "failed")
                    self.assertIn(type(failure).__name__, result["primary_error"])
                    self.assertEqual(result["observations"], ["idle", "started"])
                    self.assertEqual(result["session_status"], "passed")
                    harness.driver.close.click.assert_not_called()
                    self.assertEqual(
                        harness.http_calls.count((harness.system_port, "/status")), 1
                    )
                    self.assertLess(
                        harness.sdk.events.index("fresh-server-status"),
                        harness.sdk.events.index("session-quit"),
                    )
                    self.assertEqual(harness.diagnostics_value, "INFO")
                    self.assertFalse(harness.app_running)
                    self.assertFalse(harness.last_phone.lock.exists())

    def test_idle_control_failure_preserves_scope_and_runs_normal_cleanup(self):
        self.h.driver.find_element = Mock(
            side_effect=RuntimeError("idle UI SDK failure")
        )
        result, _ = self.run_attempt(
            close_diagnostics=True, automation_idle_control=True
        )
        self.assertEqual(result["scope"], "formal-android-managed-idle-control")
        self.assertEqual(result["primary_error"], "RuntimeError: idle UI SDK failure")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["session_status"], "passed")
        self.assertEqual(result["startup_snapshot"], {})
        self.h.driver.start.click.assert_not_called()
        self.h.driver.close.click.assert_not_called()
        self.assertFalse(self.h.app_running)
        self.assert_released()

    def test_opt_in_diagnostics_capture_and_restore_before_phone_release(self):
        result, attempt = self.run_attempt(close_diagnostics=True)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(self.h.diagnostics_value, "INFO")
        appium_calls = [
            call
            for call in self.h.sdk.spawn.call_args_list
            if "--log-level" in call.args[0]
        ]
        self.assertEqual(len(appium_calls), 1)
        args = appium_calls[0].args[0]
        self.assertEqual(args[args.index("--log-level") + 1], "debug")
        self.assertIn("--log-timestamp", args)
        self.assertIn("--log-no-colors", args)
        server_logs = [
            json.loads(path.read_bytes())
            for path in (attempt / "platform").glob("*-read.json")
            if "logcat" in json.loads(path.read_bytes()).get("argv", [])
        ]
        self.assertEqual(len(server_logs), 1)
        self.assertEqual(
            server_logs[0]["argv"][3:],
            [
                "logcat",
                "-d",
                "-v",
                "threadtime",
                "--pid",
                "3210",
                "-t",
                "1000",
                "appium:I",
                "*:S",
            ],
        )
        self.assertEqual(server_logs[0]["returncode"], 0)
        self.assertEqual(
            result["cleanup_order"],
            [
                "close-diagnostics.stop-live",
                "platform.capture-server-logs",
                "close-diagnostics.capture",
                "automation.close",
                "candidate.finalize",
                "close-diagnostics.restore",
                "bundle.prepare",
                "phone.finish",
            ],
        )
        self.assertTrue(
            (attempt / "platform/close-diagnostics/restored.json").is_file()
        )
        commands = [
            json.loads(path.read_bytes())
            for path in (attempt / "platform/close-diagnostics").glob("*-command.json")
        ]
        captured = next(item for item in commands if "logcat" in item["argv"])
        self.assertIn("phase=rust.closeResult.return", captured["stdout"])
        before_close = json.loads(
            (attempt / "platform/close-diagnostics/before-close.json").read_bytes()
        )
        self.assertEqual(before_close["elementId"], "fixture-close-element")
        self.assertEqual(before_close["before_rect"], before_close["after_rect"])
        self.assertEqual(before_close["pid"], 321)
        self.assertTrue(
            (attempt / "platform/close-diagnostics/before-close.png").is_file()
        )
        self.assertTrue(
            (attempt / "platform/close-diagnostics/before-close.xml").is_file()
        )
        self.h.driver.get_screenshot_as_png.assert_called_once_with()
        self.assertFalse((attempt / "platform/server-status.json").exists())
        self.assertNotIn((self.h.system_port, "/status"), self.h.http_calls)
        self.assert_released()

    def test_failed_ui_probes_fresh_server_before_delete_without_promoting_verdict(
        self,
    ):
        self.h.driver.get_screenshot_as_png.side_effect = TimeoutError(
            "UI read primary"
        )
        result, attempt = self.run_attempt(close_diagnostics=True)
        self.assertEqual(result["primary_error"], "TimeoutError: UI read primary")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["observations"], ["idle", "started"])
        self.assertEqual(result["cleanup_errors"], [])
        self.assertEqual(result["session_status"], "passed")
        status = json.loads((attempt / "platform/server-status.json").read_bytes())
        self.assertEqual(status["response"], self.h.server_status_response)
        self.assertIsNone(status["error"])
        self.assertEqual(self.h.http_calls.count((self.h.system_port, "/status")), 1)
        self.assertLess(
            self.h.sdk.events.index("fresh-server-status"),
            self.h.sdk.events.index("session-quit"),
        )
        self.assertEqual(result["cleanup_order"][0], "platform.probe-server-status")
        transport = json.loads((attempt / "platform/adb-transport.json").read_bytes())
        self.assertEqual(
            [stage["name"] for stage in transport["stages"]],
            [
                "server_version",
                "cached_state",
                "transport_select",
                "service_accept",
                "boot_id",
            ],
        )
        self.assertIs(transport["boot_id_matches_expected"], True)
        self.assertEqual(
            self.h.adb_calls,
            [
                b"host:version",
                f"host-serial:{self.h.serial}:get-state".encode(),
                f"host:transport:{self.h.serial}".encode(),
                b"shell:cat /proc/sys/kernel/random/boot_id",
            ],
        )
        events = self.h.sdk.events
        self.assertLess(
            events.index("fresh-server-status"), events.index("adb-transport-connect")
        )
        self.assertLess(
            events.index("adb-transport-connect"), events.index("live-logcat-stop")
        )
        self.assertLess(events.index("live-logcat-stop"), events.index("session-quit"))
        self.assertFalse(self.h.app_running)
        self.assert_released()

    def test_adb_probe_failure_preserves_ui_primary_and_independent_cleanup(self):
        self.h.driver.get_screenshot_as_png.side_effect = TimeoutError("UI primary")
        self.h.adb_responses = [b"FAIL0004nope"]
        result, attempt = self.run_attempt(close_diagnostics=True)
        self.assertEqual(result["primary_error"], "TimeoutError: UI primary")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["session_status"], "passed")
        self.assertEqual(result["phone_status"], "quarantined")
        self.assertTrue(
            any(
                error.startswith("platform.probe-adb-transport:")
                for error in result["cleanup_errors"]
            )
        )
        self.assertEqual(self.h.adb_calls, [b"host:version"])
        self.assertEqual(self.h.diagnostics_value, "INFO")
        self.assertFalse(self.h.app_running)
        self.assertIn("session-quit", self.h.sdk.events)
        probe = json.loads((attempt / "platform/adb-transport.json").read_bytes())
        self.assertEqual(probe["stages"][0]["error"]["type"], "RuntimeError")

    def test_fresh_server_timeout_preserves_ui_primary_and_cleanup_time(self):
        self.h.driver.get_screenshot_as_png.side_effect = TimeoutError(
            "UI read primary"
        )
        self.h.server_status_timeout = True
        result, attempt = self.run_attempt(close_diagnostics=True, cleanup_seconds=1)
        self.assertEqual(result["primary_error"], "TimeoutError: UI read primary")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["session_status"], "passed")
        self.assertEqual(result["phone_status"], "quarantined")
        status = json.loads((attempt / "platform/server-status.json").read_bytes())
        self.assertEqual(status["error"]["type"], "TimeoutError")
        self.assertIsNone(status["response"])
        self.assertLessEqual(
            status["finished_monotonic"] - status["started_monotonic"], 0.5
        )
        self.assertEqual(self.h.http_calls.count((self.h.system_port, "/status")), 1)
        self.assertLess(
            self.h.sdk.events.index("fresh-server-status"),
            self.h.sdk.events.index("session-quit"),
        )
        self.assertTrue(
            any(
                "platform.probe-server-status" in error
                for error in result["cleanup_errors"]
            )
        )
        self.assertEqual(self.h.diagnostics_value, "INFO")
        self.assertEqual(self.h.replies[READ], "absent\n")
        self.assertFalse(self.h.app_running)

    def test_server_log_timeout_is_not_retried_or_allowed_to_skip_other_cleanup(self):
        self.h.server_diagnostics_capture_error = True
        result, attempt = self.run_attempt(close_diagnostics=True, cleanup_seconds=1)
        self.assertEqual(result["scenario_status"], "passed")
        self.assertEqual(result["session_status"], "passed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phone_status"], "quarantined")
        self.assertTrue(
            any("platform.capture-server-logs" in e for e in result["cleanup_errors"])
        )
        self.assertEqual(
            sum(
                call.args[0][3:4] == ["logcat"] and "appium:I" in call.args[0]
                for call in self.h.command.call_args_list
            ),
            1,
        )
        self.assertTrue(
            (attempt / "platform/close-diagnostics/restored.json").is_file()
        )
        self.assertEqual(self.h.diagnostics_value, "INFO")
        self.assertEqual(self.h.replies[READ], "absent\n")
        self.assertTrue(self.h.app_running)
        self.assertTrue(self.h.last_phone.lock.exists())

    def test_diagnostic_capture_failure_preserves_session_cleanup_and_restoration(self):
        self.h.diagnostics_capture_error = True
        result, _ = self.run_attempt(close_diagnostics=True)
        self.assertEqual(result["scenario_status"], "passed")
        self.assertEqual(result["session_status"], "passed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phone_status"], "quarantined")
        self.assertTrue(
            any(
                "close-diagnostics.capture" in item for item in result["cleanup_errors"]
            )
        )
        self.assertEqual(self.h.diagnostics_value, "INFO")
        self.assertTrue(self.h.app_running)

    def test_diagnostics_restore_failure_blocks_phone_release_without_hiding_ui_pass(
        self,
    ):
        self.h.diagnostics_restore_error = True
        result, _ = self.run_attempt(close_diagnostics=True)
        self.assertEqual(result["scenario_status"], "passed")
        self.assertEqual(result["session_status"], "passed")
        self.assertEqual(result["phone_status"], "quarantined")
        self.assertEqual(self.h.diagnostics_value, "DEBUG")
        self.assertTrue(
            any(
                "close-diagnostics.restore" in item for item in result["cleanup_errors"]
            )
        )
        self.assertTrue(self.h.last_phone.lock.exists())

    def test_invalid_inputs_and_authorizations_rejected_before_any_device_command(self):
        changes: dict[str, Any]
        for changes in (
            {"authorize_install": False},
            {"authorize_helper_settings": False},
            {"authorize_install": 1},
            {"use_root": 1},
            {"close_diagnostics": 1},
            {"automation_idle_control": 1},
            {"automation_start_hold_control": 1},
            {"automation_start_hold_control": True},
            {
                "automation_start_hold_control": True,
                "automation_idle_control": True,
                "close_diagnostics": True,
            },
            {"automation_idle_control": True, "close_diagnostics": False},
            {"device": "emulator-5554"},
            {"device": "127.0.0.1:5555"},
            {"device": "unknown"},
            {"app_id": "bad app"},
            {"activity": "."},
            {"apk_sha256": "bad"},
            {"appium_port": self.h.system_port},
            {"system_port": True},
            {"appium_port": 65536},
            {"timeout": float("nan")},
            {"phase_timeout": 0},
            {"cleanup_seconds": float("inf")},
            {"timeout": True},
            {"run_id": "../../bad"},
            {"cwd": "not-a-Path"},
            {"appium_entry": self.h.root / "missing.js"},
            {"expected_helper_sha256": {}},
            {
                "expected_helper_sha256": self.h.expected
                | {"io.appium.settings": "0" * 64}
            },
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises((ValueError, TypeError, OSError, RuntimeError)),
            ):
                runner.run_android_lifecycle(**(self.h.arguments() | changes))
        self.assertEqual(self.h.calls, [])
        self.h.sdk.spawn.assert_not_called()
        self.assertIsNone(self.h.last_phone)

    def test_helper_apk_above_receipt_budget_is_hashed_before_device_admission(self):
        # Real UiAutomator2 server APKs exceed the JSON receipt reader's 8 MiB cap.
        from tools.react_native.bleng_android_session import MAX_OUTPUT_BYTES

        package = "io.appium.uiautomator2.server"
        path = self.h.apks[package]
        path.write_bytes(b"x" * (8 * MAX_OUTPUT_BYTES + 1))
        self.h.expected[package] = hashlib.sha256(path.read_bytes()).hexdigest()
        arguments = self.h.arguments()
        attempt = arguments["output_root"] / arguments["run_id"]
        attempt.mkdir(parents=True)
        # Reaching exclusive attempt creation proves local helper hashes were
        # admitted; no phone/candidate/automation owner may be acquired here.
        with self.assertRaises(FileExistsError):
            runner.run_android_lifecycle(**arguments)
        self.assertEqual(self.h.calls, [])
        self.assertIsNone(self.h.last_phone)
        self.h.sdk.spawn.assert_not_called()

    def test_missing_local_helper_rejected_before_phone_acquisition(self):
        self.h.apks["io.appium.settings"].unlink()
        with self.assertRaises(AppiumError):
            runner.run_android_lifecycle(**self.h.arguments())
        self.assertEqual(self.h.calls, [])
        self.assertIsNone(self.h.last_phone)

    def test_old_phone_lock_is_never_adopted_or_removed(self):
        self.h.phone = type(self.h.phone)(
            self.h.serial, self.h.run_id, self.h.root / "old.json"
        )
        self.h.phone.acquire()
        owner = (self.h.phone.lock / "owner.json").read_bytes()
        result, _ = self.run_attempt()
        self.assertEqual(result["phone_status"], "not_acquired")
        self.assertEqual(result["cleanup_order"], ["bundle.prepare"])
        self.assertEqual(result["phone_cleanup_errors"], [])
        self.assertEqual((self.h.phone.lock / "owner.json").read_bytes(), owner)
        self.assertEqual(self.h.calls, [])

    def test_reused_attempt_never_overwrites_previous_output(self):
        result, attempt = self.run_attempt()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["scenario_status"], "passed")
        self.assertEqual(result["session_status"], "passed")
        self.assertIsNone(result["primary_error"])
        self.assertEqual(result["cleanup_errors"], [])
        self.assertEqual(result["observations"], ["idle", "started", "closed"])
        self.assertEqual(self.h.driver.page, "Evidence")
        self.assert_released()
        original = (attempt / "result.json").read_bytes()
        before = len(self.h.calls)
        with self.assertRaises(FileExistsError):
            runner.run_android_lifecycle(**self.h.arguments())
        self.assertEqual((attempt / "result.json").read_bytes(), original)
        self.assertEqual(len(self.h.calls), before)

    def test_missing_ui_node_expires_and_cleans_up_original_app(self):
        self.h.driver.overrides["formal-host-title"] = Mock(
            side_effect=NoSuchElementException("formal-host-title")
        )
        result, _ = self.run_attempt(phase_timeout=0.3)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["scenario_status"], "failed")
        self.assertEqual(result["session_status"], "passed")
        self.assertEqual(
            result["primary_error"],
            "tools.react_native.run_bleng_ui._UiDeadline: Timeout waiting for formal-host-title",
        )
        self.assertEqual(result["cleanup_errors"], [])
        self.assertGreater(self.h.driver.find_calls.count("formal-host-title"), 1)
        self.h.sleep.assert_called()
        self.h.driver.start.click.assert_not_called()
        self.h.driver.close.click.assert_not_called()
        self.h.driver.scan_start.click.assert_not_called()
        self.assertFalse(self.h.app_running)
        self.assert_released()

    def test_pending_start_expires_without_retry_and_cleans_up_original_app(self):
        self.h.driver.start.click.side_effect = lambda: None
        result, _ = self.run_attempt(phase_timeout=0.3)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["scenario_status"], "failed")
        self.assertEqual(result["session_status"], "passed")
        self.assertEqual(
            result["primary_error"],
            "tools.react_native.run_bleng_ui._UiDeadline: "
            "Timeout waiting for session-state 'started'; observed 'idle'",
        )
        self.assertEqual(result["cleanup_errors"], [])
        self.assertEqual(result["observations"], ["idle"])
        self.h.sleep.assert_called()
        self.h.driver.start.click.assert_called_once()
        self.h.driver.close.click.assert_not_called()
        self.h.driver.scan_start.click.assert_not_called()
        self.assertFalse(self.h.app_running)
        self.assert_released()

    def test_prelaunch_validation_rejection_restores_helper_and_releases_without_adoption(
        self,
    ):
        self.h.reject_badging = True
        result, attempt = self.run_attempt()
        self.assertEqual(result["scenario_status"], "not_run")
        self.assertEqual(result["cleanup_errors"], [])
        self.assertEqual(result["phone_status"], "failed")
        final = json.loads((attempt / "candidate/finalization.json").read_bytes())
        self.assertEqual(final["action"], "no-launch")
        self.assertFalse(self.h.app_running)
        self.h.sdk.remote.assert_not_called()
        self.assert_released()

    def test_uncertain_installation_does_not_claim_no_launch_cleanup_success(self):
        self.h.install_error = True
        result, _ = self.run_attempt()
        self.assertEqual(result["scenario_status"], "not_run")
        self.assertEqual(result["phone_status"], "quarantined")
        self.assertTrue(self.h.last_phone.lock.exists())
        self.assertTrue(
            any(
                "installation outcome uncertain" in error
                for error in result["cleanup_errors"]
            )
        )
        self.assertFalse(self.h.app_running)
        self.h.sdk.remote.assert_not_called()

    def test_preparation_failure_before_handoff_rolls_back_once(self):
        failed = False

        def fail(command):
            nonlocal failed
            if (
                command == APPOPS
                and self.h.replies[APPOPS] == "MOCK_LOCATION: allow\n"
                and not failed
            ):
                failed = True
                raise RuntimeError("prepare after known AppOp")

        self.h.after_command = fail
        result, _ = self.run_attempt()
        self.assertIn("prepare after known AppOp", result["primary_error"])
        self.assertEqual(result["cleanup_errors"], [])
        self.assertEqual(self.h.calls.count(DENY), 1)
        self.assert_released()

    def test_unknown_launch_or_missing_original_identity_never_adopts_process(self):
        for kind in ("unknown_launch", "bad_identity"):
            with self.subTest(kind=kind):
                # A separate harness is needed: quarantine is intentionally permanent.
                root = Path(self.enterContext(tempfile.TemporaryDirectory()))
                with LifecycleHarness(root / "harness") as h:
                    setattr(h, kind, True)
                    path = runner.run_android_lifecycle(**h.arguments())
                    result = verify_lifecycle_bundle(path.parent)
                    self.assertEqual(result["phone_status"], "quarantined")
                    self.assertTrue(result["cleanup_errors"])
                    self.assertTrue(h.app_running)
                    self.assertTrue(h.last_phone.lock.exists())
                    self.assertNotIn(
                        ("shell", "am", "force-stop", "--user", "0", h.app_id), h.calls
                    )

    def test_unknown_remote_outcome_is_not_cleaned_into_success(self):
        self.h.remote_error = RuntimeError("Remote outcome unknown")
        result, _ = self.run_attempt()
        self.assertIn("Remote outcome unknown", result["primary_error"])
        self.assertEqual(result["session_status"], "failed")
        self.assertEqual(result["phone_status"], "quarantined")
        self.assertTrue(self.h.last_phone.lock.exists())
        self.assertFalse(self.h.app_running)  # Known original App only.
        self.assertNotIn(DELETE, self.h.calls)
        self.assertTrue(any("unknown" in error for error in result["cleanup_errors"]))
        self.h.driver.quit.assert_not_called()

    def test_ui_primary_and_independent_cleanup_errors_are_both_retained(self):
        self.h.driver.start.click.side_effect = RuntimeError("UI primary")
        self.h.quit_error = RuntimeError("DELETE cleanup")
        result, _ = self.run_attempt()
        self.assertIn("UI primary", result["primary_error"])
        self.assertTrue(
            any("DELETE cleanup" in error for error in result["cleanup_errors"])
        )
        self.assertEqual(result["phone_status"], "quarantined")
        self.assertFalse(self.h.app_running)
        self.assertIn("server-reaped", self.h.sdk.events)

    def test_ui_failure_stops_only_original_app_and_allows_verified_release(self):
        self.h.driver.start.click.side_effect = RuntimeError("UI primary")
        result, _ = self.run_attempt()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["scenario_status"], "failed")
        self.assertEqual(result["session_status"], "passed")
        self.assertFalse(self.h.app_running)
        self.assert_released()

    def test_preexisting_process_refused_before_install_and_never_stopped(self):
        self.h.replies[PS] += f"321 10123 {self.h.app_id}\n"
        result, _ = self.run_attempt()
        self.assertIn("already running", result["primary_error"])
        self.assertIsNone(self.h.upload)
        self.assertIn(f"321 10123 {self.h.app_id}\n", self.h.replies[PS])
        self.assert_released()

    def test_publication_failure_after_release_never_reacquires_or_rolls_back_phone(
        self,
    ):
        failure = OSError("bundle publication")
        with (
            patch.object(runner, "publish_lifecycle_bundle", side_effect=failure),
            self.assertRaises(OSError) as caught,
        ):
            runner.run_android_lifecycle(**self.h.arguments())
        self.assertIs(caught.exception, failure)
        self.assert_released()
        self.assertTrue(self.h.app_running)
        prepared = self.h.root / "runs" / self.h.run_id / "prepared-result.json"
        self.assertEqual(
            json.loads(prepared.read_bytes())["host_gate_status"], "pending"
        )
        self.assertFalse(prepared.with_name("result.json").exists())
        self.assertTrue(
            any("phone_status=passed" in note for note in failure.__notes__)
        )

    def test_keyboard_interrupt_is_rethrown_after_all_owned_cleanup_and_publication(
        self,
    ):
        interruption = KeyboardInterrupt("stop lifecycle")
        self.h.driver.start.click.side_effect = interruption
        with self.assertRaises(KeyboardInterrupt) as caught:
            runner.run_android_lifecycle(**self.h.arguments())
        self.assertIs(caught.exception, interruption)
        self.assert_released()
        result = verify_lifecycle_bundle(self.h.root / "runs" / self.h.run_id)
        self.assertIn("KeyboardInterrupt", result["primary_error"])
        self.assertFalse(self.h.app_running)

    def test_explicit_root_install_is_forwarded_without_helper_install_or_grants(self):
        result, attempt = self.run_attempt(use_root=True)
        self.assertEqual(result["status"], "passed")
        installs = list((attempt / "candidate").glob("*-install.json"))
        self.assertEqual(len(installs), 1)
        receipt = json.loads(installs[0].read_bytes())
        self.assertIs(receipt["use_root"], True)
        self.assertIs(receipt["grant"], False)
        self.assertIs(receipt["test_only"], False)
        self.assert_released()

    def test_spawn_failure_restores_through_parent_only_and_stops_original_app(self):
        self.h.sdk.spawn.side_effect = OSError("spawn failed")
        result, attempt = self.run_attempt()
        self.assertIn("spawn failed", result["primary_error"])
        self.assertEqual(result["session_status"], "failed")
        self.assertEqual(result["cleanup_errors"], [])
        self.assertEqual(result["cleanup_order"].count("automation.close"), 1)
        self.assertNotIn("helper.restore", result["cleanup_order"])
        self.assertEqual(self.h.calls.count(DELETE), 1)
        self.assertFalse(self.h.app_running)
        self.assertTrue((attempt / "automation/close.json").exists())
        self.assert_released()

    def test_local_apk_hash_failure_never_acquires_phone_or_dispatches(self):
        result, attempt = self.run_attempt(apk_sha256="0" * 64)
        self.assertEqual(result["phone_status"], "not_acquired")
        self.assertEqual(result["cleanup_order"], ["bundle.prepare"])
        self.assertIsNone(self.h.last_phone)
        self.assertEqual(self.h.calls, [])
        self.assertFalse((attempt / "candidate.apk").exists())

    def test_prepared_publication_failure_quarantines_without_hiding_scenario_pass(
        self,
    ):
        with patch.object(
            runner, "publish_new_json", side_effect=OSError("prepare publication")
        ):
            result, _ = self.run_attempt()
        self.assertEqual(result["scenario_status"], "passed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phone_status"], "quarantined")
        self.assertTrue(self.h.last_phone.lock.exists())
        self.assertTrue(self.h.app_running)
        self.assertTrue(
            any("prepare publication" in error for error in result["cleanup_errors"])
        )

    def test_cleanup_interrupt_survives_publication_error_after_local_reap(self):
        interruption = KeyboardInterrupt("DELETE interrupted")
        self.h.quit_error = interruption
        with (
            patch.object(
                runner,
                "publish_lifecycle_bundle",
                side_effect=OSError("publication failed"),
            ),
            self.assertRaises(KeyboardInterrupt) as caught,
        ):
            runner.run_android_lifecycle(**self.h.arguments())
        self.assertIs(caught.exception, interruption)
        self.assertIn("server-reaped", self.h.sdk.events)
        self.assertTrue(self.h.last_phone.lock.exists())
        self.assertTrue(self.h.app_running)
        self.assertTrue(
            any("publication failed" in note for note in interruption.__notes__)
        )

    def test_local_freeze_time_is_part_of_common_absolute_deadline(self):
        freeze = runner.freeze_lifecycle_apk

        def late(*args, **kwargs):
            freeze(*args, **kwargs)
            self.h.clock += 61

        with patch.object(runner, "freeze_lifecycle_apk", side_effect=late):
            result, _ = self.run_attempt()
        self.assertEqual(result["phone_status"], "not_acquired")
        self.assertIn("deadline", result["primary_error"])
        self.assertEqual(self.h.calls, [])
