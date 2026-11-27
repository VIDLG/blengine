"""Tamper and relocation tests for formal managed Android scan evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tests.peripherals.nrf52840 import fixture_session
from tools.react_native import bleng_scan_evidence as evidence

RUN = "12345678-1234-4234-8234-123456789abc"
DEVICE = "TESTPHYSICAL42"
APP = "dev.blehub.formal"
SERVICE = "12345678-1234-4234-8234-123456789abc"
RESOURCE = "nrf-usb:SCANFIXTURE"
PORT = "/dev/scan-fixture"
TEST_APK = "5" * 64
TRACE = [
    "idle",
    "started",
    "scan:running",
    "scan:advertisement-matched",
    "scan:stopped",
    "closed",
]


class ScanEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.attempt = self.root / "attempt"
        self.attempt.mkdir()
        self._make_success()

    def write(self, relative: str, value: dict) -> None:
        path = self.attempt / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    def _make_apk(self) -> str:
        path = self.attempt / "candidate.apk"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"manifest")
            archive.writestr("assets/index.android.bundle", b"bundle")
            archive.writestr("classes.dex", b"dex")
            archive.writestr("lib/arm64-v8a/libbleng_uniffi.so", b"elf")
            archive.writestr("lib/arm64-v8a/libBleHubReactNativeBleng.so", b"elf")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _make_fixture(self) -> dict:
        source = self.root / "contract.json"
        source.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "fixture_role": "multifunction-rf",
                    "image_id": "nrf-multifunction-rf-v2-a",
                    "image_version": "2",
                    "image_sha256": "a" * 64,
                    "board": "nrf52840dk_nrf52840",
                    "profile_id": "blehub-nrf52840-multifunction-v2-a",
                    "service_uuid": SERVICE,
                    "local_name": "BleHub Scan Fixture",
                    "capabilities": [
                        "normal-rf",
                        "gatt-reset",
                        "notification",
                        "indication",
                        "passive-disconnect",
                        "att-read-fault-v2",
                        "att-write-fault-v2",
                        "att-ccc-fault-v2",
                        "att-precommit-defer-v2",
                        "att-fault-arm-timeout-10000",
                        "att-fault-hold-timeout-20000",
                    ],
                }
            ),
            encoding="utf-8",
        )
        profile = self.root / "profile.json"
        roles = [
            (
                "read-write",
                "fff10000-0000-4000-8000-000000000000",
                ["read", "write", "write_without_response"],
                "00",
            ),
            (
                "updates",
                "fff20000-0000-4000-8000-000000000000",
                ["read", "notify", "indicate"],
                "00",
            ),
            (
                "read-only",
                "fff30000-0000-4000-8000-000000000000",
                ["read"],
                "426c65487562",
            ),
            (
                "write-only",
                "fff40000-0000-4000-8000-000000000000",
                ["write", "write_without_response"],
                "00",
            ),
            (
                "indications",
                "fff50000-0000-4000-8000-000000000000",
                ["read", "indicate"],
                "00",
            ),
        ]
        profile.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profile_id": "blehub-nrf52840-multifunction-v2-a",
                    "notification_value_hex": "ef",
                    "advertising": {
                        "local_name": "BleHub Scan Fixture",
                        "service_uuids": [SERVICE],
                        "require_local_name": True,
                    },
                    "services": [
                        {
                            "uuid": SERVICE,
                            "primary": True,
                            "advertise": True,
                            "characteristics": [
                                {
                                    "role": role,
                                    "uuid": uuid,
                                    "properties": properties,
                                    "initial_value_hex": value,
                                }
                                for role, uuid, properties, value in roles
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        fixture = self.attempt / "fixture"
        fixture.mkdir(parents=True)
        contract_bytes = source.read_bytes()
        profile_bytes = profile.read_bytes()
        (fixture / fixture_session.CONTRACT_NAME).write_bytes(contract_bytes)
        (fixture / fixture_session.PROFILE_NAME).write_bytes(profile_bytes)
        (fixture / fixture_session.REQUEST_NAME).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": RUN,
                    "contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
                    "profile_sha256": hashlib.sha256(profile_bytes).hexdigest(),
                    "service_uuid": SERVICE,
                    "port": PORT,
                    "resource_identity": RESOURCE,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        plan = fixture_session.load_session(fixture / fixture_session.REQUEST_NAME)
        captured = datetime.now(timezone.utc).isoformat()
        installed = {
            "schema_version": 1,
            "image_id": plan.contract.image_id,
            "image_version": plan.contract.image_version,
            "image_sha256": plan.contract.image_sha256,
            "board": plan.contract.board,
            "profile_id": plan.contract.profile_id,
            "service_uuid": plan.contract.service_uuid,
            "local_name": plan.contract.local_name,
            "capabilities": sorted(plan.contract.capabilities),
        }
        receipt = {
            "schema_version": 1,
            "session_id": RUN,
            "proof_method": fixture_session.PROOF_METHOD,
            "captured_at_utc": captured,
            "fixture_pid": os.getpid(),
            "fixture_pgid": os.getpgid(0),
            "port": PORT,
            "resource_identity": RESOURCE,
            "contract_sha256": plan.contract_sha256,
            "profile_sha256": plan.profile_sha256,
            "installed_identity": installed,
        }
        (fixture / fixture_session.RECEIPT_NAME).write_text(
            json.dumps(receipt, sort_keys=True), encoding="utf-8"
        )
        self.write(
            "fixture/lease.json",
            {
                "schema_version": 1,
                "run_id": RUN,
                "resource_identity": RESOURCE,
                "observed_resource_identity": RESOURCE,
                "status": "released",
                "acquired": True,
                "fixture_pid": os.getpid(),
                "fixture_pgid": os.getpgid(0),
                "error": None,
            },
        )
        self.write(
            "fixture/start.json",
            {
                "run_id": RUN,
                "status": "passed",
                "resource_identity": RESOURCE,
                "error": None,
            },
        )
        close = {
            "session_id": RUN,
            "resource_identity": RESOURCE,
            "boot_nonce": "aa" * 16,
            "supported_settings": 1024,
            "current_settings": 0,
            "proof_method": "btp-gap-controller-info-advertising-bit-clear",
            "advertising_inactive": True,
            "resources_closed": True,
        }
        self.write(
            "fixture/close.json",
            {
                "run_id": RUN,
                "resource_identity": RESOURCE,
                "status": "passed",
                "receipt": close,
                "error": None,
            },
        )
        (self.attempt / "scan").mkdir()
        shutil.copy2(
            fixture / fixture_session.REQUEST_NAME,
            self.attempt / "scan" / fixture_session.REQUEST_NAME,
        )
        shutil.copy2(
            fixture / fixture_session.RECEIPT_NAME,
            self.attempt / "scan" / fixture_session.RECEIPT_NAME,
        )
        return {"plan": plan, "close": close, "receipt": receipt}

    def _make_success(self) -> None:
        self.fixture = self._make_fixture()
        digest = self._make_apk()
        self.write(
            "request.json",
            {
                "schema_version": 1,
                "scope": evidence.SCOPE,
                "run_id": RUN,
                "device": DEVICE,
                "app_id": APP,
                "activity": APP + ".MainActivity",
                "apk_sha256": digest,
                "driver_kind": evidence.ANDROID_NATIVE_DRIVER_KIND,
                "test_apk_sha256": TEST_APK,
                "fixture_resource_identity": RESOURCE,
                "fixture_port": PORT,
                "service_uuid": SERVICE,
                "authorize_install": True,
                "authorize_fixture_start": True,
                "authorize_bluetooth_permissions": True,
                "use_root": True,
                "timeout": 60,
                "cleanup_seconds": 30,
                "fixture_pid": os.getpid(),
                "fixture_pgid": os.getpgid(0),
            },
        )
        phone = {
            "run_id": RUN,
            "platform": "android",
            "device": DEVICE,
            "status": "passed",
            "actions": [],
            "primary_error": None,
            "cleanup_errors": [],
            "initial_state": None,
            "final_state": None,
        }
        self.write("phone.json", phone)
        phone_hash = hashlib.sha256(
            (self.attempt / "phone.json").read_bytes()
        ).hexdigest()
        identity = {
            "serial": DEVICE,
            "package": APP,
            "pid": 123,
            "uid": 10123,
            "cmdline": APP,
            "start_ticks": 456,
            "boot_id": RUN,
            "base_apk_path": "/data/app/formal/base.apk",
            "apk_sha256": digest,
        }
        self.write("candidate/bound-identity.json", identity)
        self.write(
            "candidate/finalization.json",
            {
                "run_id": RUN,
                "status": "passed",
                "action": "retain",
                "error": None,
                "identity": identity,
            },
        )
        log = self.attempt / "candidate/001-install.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_bytes(b"installed\n")
        self.write(
            "candidate/001-install.json",
            {
                "run_id": RUN,
                "serial": DEVICE,
                "apk_sha256": digest,
                "status": "passed",
                "error": None,
                "grant": False,
                "use_root": True,
                "test_only": False,
                "stdout_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            },
        )
        self.write(
            "driver/record.json",
            {
                "schema_version": 1,
                "scope": "formal-android-native-scene-driver",
                "run_id": RUN,
                "kind": evidence.ANDROID_NATIVE_DRIVER_KIND,
                "session_id": "host-session-1",
                "device": DEVICE,
                "app_id": APP,
                "test_apk_sha256": TEST_APK,
                "test_component": "dev.blehub.formal.test/androidx.test.runner.AndroidJUnitRunner",
                "test_class": "dev.blehub.formal.BlengSceneDriverInstrumentedTest",
                "plan": "scan",
                "phases": [
                    {
                        "name": "scan",
                        "host": None,
                        "steps": 3,
                        "result": "phase-scan.json",
                        "status": "passed",
                        "step_seconds_total": 1.0,
                        "instrumentation_returncode": 0,
                    }
                ],
                "steps": 3,
                "status": "passed",
                "primary_error": None,
            },
        )
        self.write(
            "driver/phase-scan.json",
            {
                "schema_version": 1,
                "scope": "formal-android-scene-driver",
                "run_id": RUN,
                "platform": "android",
                "device": DEVICE,
                "app_id": APP,
                "test_apk_sha256": TEST_APK,
                "status": "passed",
                "primary_error": None,
            },
        )
        inner = {
            "schema_version": 1,
            "scope": "formal-rn-prepared-scan",
            "run_id": RUN,
            "platform": "android",
            "device": DEVICE,
            "app_id": APP,
            "scenario_status": "passed",
            "session_status": "passed",
            "host_gate_status": "pending",
            "observations": TRACE,
            "cleanup_order": [
                "driver.close",
                "candidate.finalize",
                "fixture.close",
                "fixture.evidence",
                "bundle.prepare",
                "phone.finish",
            ],
            "driver_session_id": "host-session-1",
            "driver": {
                "kind": evidence.ANDROID_NATIVE_DRIVER_KIND,
                "plan": "scan",
                "steps": 3,
                "phases": ["scan"],
                "session_id": "host-session-1",
                "test_apk_sha256": TEST_APK,
            },
            "fixture": {
                "resource_identity": RESOURCE,
                "contract_sha256": self.fixture["plan"].contract_sha256,
                "profile_sha256": self.fixture["plan"].profile_sha256,
            },
            "fixture_input_sha256": {
                fixture_session.REQUEST_NAME: hashlib.sha256(
                    (self.attempt / "scan" / fixture_session.REQUEST_NAME).read_bytes()
                ).hexdigest(),
                fixture_session.RECEIPT_NAME: hashlib.sha256(
                    (self.attempt / "scan" / fixture_session.RECEIPT_NAME).read_bytes()
                ).hexdigest(),
            },
            "fixture_close": self.fixture["close"],
            "phone_journal": {
                "path": str(self.attempt / "phone.json"),
                "status": "passed",
                "sha256": phone_hash,
            },
        }
        self.write("scan/result.json", inner)
        prepared = copy.deepcopy(inner)
        prepared["session_status"] = "pending"
        self.write("scan/prepared-result.json", prepared)
        self.result = {
            "schema_version": 1,
            "scope": evidence.SCOPE,
            "run_id": RUN,
            "platform": "android",
            "device": DEVICE,
            "app_id": APP,
            "apk_sha256": digest,
            "status": "passed",
            "host_gate_status": "pending",
            "rf": "scan-ui-observation",
            "scenario_status": "passed",
            "session_status": "passed",
            "phone_status": "passed",
            "fixture_status": "passed",
            "fixture_close": self.fixture["close"],
            "primary_error": None,
            "cleanup_errors": [],
            "phone_cleanup_errors": [],
            "cleanup_order": [
                "scan.run-prepared",
                "scan.result",
                "fixture.close-record",
                "fixture.lease-release",
                "fixture.lease-record",
                "bundle.prepare",
                "cleanup.complete",
            ],
        }
        # The in-process lane always prepares and restores the Bluetooth grants under the
        # phone lease, so a successful bundle carries that scope by construction.
        self.enable_permission_scope()

    def publish(self) -> None:
        evidence.publish_scan_bundle(self.attempt, self.result)

    def enable_permission_scope(self) -> None:
        request = json.loads((self.attempt / "request.json").read_bytes())
        request["authorize_bluetooth_permissions"] = True
        (self.attempt / "request.json").write_text(
            json.dumps(request, sort_keys=True), encoding="utf-8"
        )
        denied = {
            "android.permission.BLUETOOTH_SCAN": False,
            "android.permission.BLUETOOTH_CONNECT": False,
        }
        self.write(
            "platform/permission-scope.json",
            {
                "schema_version": 1,
                "scope": "android-bluetooth-permissions",
                "platform": "android",
                "device": DEVICE,
                "run_id": RUN,
                "package": APP,
                "user_id": 0,
                "permission_command_mode": "root",
                "status": "passed",
                "baseline_grants": denied,
                "prepared_grants": {permission: True for permission in denied},
                "restored_grants": denied,
            },
        )

    def test_formal_permission_scope_replays_and_requires_exact_restore(self) -> None:
        self.enable_permission_scope()
        restored = self.attempt / "platform/permission-scope.json"
        document = json.loads(restored.read_bytes())
        document["restored_grants"]["android.permission.BLUETOOTH_SCAN"] = True
        restored.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.publish()

        document["restored_grants"]["android.permission.BLUETOOTH_SCAN"] = False
        restored.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        self.publish()
        self.assertEqual(evidence.verify_scan_bundle(self.attempt), self.result)

    def test_success_replays_offline_and_survives_relocation(self) -> None:
        self.publish()
        self.assertEqual(evidence.verify_scan_bundle(self.attempt), self.result)
        relocated = self.root / "relocated"
        shutil.copytree(self.attempt, relocated)
        request = relocated / "fixture" / fixture_session.REQUEST_NAME
        now_ns = time.time_ns()
        os.utime(request, ns=(now_ns, now_ns))
        self.assertEqual(evidence.verify_scan_bundle(relocated), self.result)

    def _rewrite_as_scene(self, scene: evidence.SceneEvidence) -> None:
        """Re-label the same host facts for another managed scene."""
        request = json.loads((self.attempt / "request.json").read_bytes())
        request["scope"] = scene.scope("android")
        self.write("request.json", request)
        record = json.loads((self.attempt / "driver/record.json").read_bytes())
        record["plan"] = scene.name
        record["phases"][0]["name"] = scene.name
        self.write("driver/record.json", record)
        for name in ("scan/result.json", "scan/prepared-result.json"):
            inner = json.loads((self.attempt / name).read_bytes())
            inner["scope"] = scene.inner_scope
            inner["observations"] = list(scene.trace)
            inner["driver"]["plan"] = scene.name
            inner["driver"]["phases"] = [scene.name]
            self.write(name, inner)
        self.result["scope"] = scene.scope("android")
        self.result["rf"] = scene.rf

    def test_an_authorized_release_replays_and_an_unauthorized_one_does_not(
        self,
    ) -> None:
        """A scene sequence may stop the App it launched, but never silently.

        The success contract otherwise requires the App to still be running, because that
        is the evidence a human inspects and the proof that no foreign process was
        adopted. A receipt that says `force-stop` therefore has to say the caller asked
        for it; otherwise the bundle is describing an unexplained stop.
        """
        final = json.loads((self.attempt / "candidate/finalization.json").read_bytes())
        final["action"] = "force-stop"
        final["released_retained_app"] = True
        self.write("candidate/finalization.json", final)
        self.publish()
        self.assertEqual(evidence.verify_scan_bundle(self.attempt), self.result)

    def test_a_stop_without_the_release_flag_cannot_support_success(self) -> None:
        final = json.loads((self.attempt / "candidate/finalization.json").read_bytes())
        final["action"] = "force-stop"
        self.write("candidate/finalization.json", final)
        with self.assertRaises(evidence.ScanEvidenceError):
            evidence.publish_scan_bundle(self.attempt, self.result)

    def test_a_release_flag_cannot_describe_a_retained_app(self) -> None:
        final = json.loads((self.attempt / "candidate/finalization.json").read_bytes())
        final["released_retained_app"] = True
        self.write("candidate/finalization.json", final)
        with self.assertRaises(evidence.ScanEvidenceError):
            evidence.publish_scan_bundle(self.attempt, self.result)

    def test_notification_scene_publishes_and_replays_under_its_own_scope(self) -> None:
        self._rewrite_as_scene(evidence.NOTIFICATION_SCENE)
        # The prepared result is the producer's pre-finalization view: a successful
        # scene still publishes with `session_status=pending`, and that must verify.
        prepared = copy.deepcopy(self.result)
        prepared["session_status"] = "pending"
        self.write("prepared-result.json", prepared)
        path = evidence.publish_scan_bundle(
            self.attempt, self.result, scene=evidence.NOTIFICATION_SCENE
        )
        replayed = evidence.verify_scan_bundle(path.parent)
        self.assertEqual(replayed, self.result)
        self.assertEqual(replayed["scope"], "formal-android-managed-notification")
        self.assertEqual(replayed["rf"], "notification-ui-observation")

    def test_the_three_fault_scenes_publish_and_replay_under_their_own_scopes(
        self,
    ) -> None:
        """Each fault scene is a scene: its scope, rf name and ordered trace are its own.

        The evidence layer must not accept one scene's trace under another scene's scope,
        and the three rejection scenes are otherwise ordinary managed bundles.
        """
        cases = (
            evidence.WRITE_REJECT_SCENE,
            evidence.CCC_ENABLE_REJECT_SCENE,
            evidence.CCC_DISABLE_REJECT_SCENE,
        )
        pristine = Path(self.temporary.name) / "pristine"
        shutil.copytree(self.attempt, pristine)
        for scene in cases:
            with self.subTest(scene=scene.name):
                attempt = Path(self.temporary.name) / scene.name
                shutil.copytree(pristine, attempt)
                self.attempt = attempt
                self._rewrite_as_scene(scene)
                path = evidence.publish_scan_bundle(attempt, self.result, scene=scene)
                replayed = evidence.verify_scan_bundle(path.parent)
                self.assertEqual(replayed, self.result)
                self.assertEqual(replayed["scope"], scene.scope("android"))
                self.assertEqual(replayed["rf"], scene.rf)

    def test_a_rejection_scene_cannot_borrow_another_scene_s_trace(self) -> None:
        self._rewrite_as_scene(evidence.WRITE_REJECT_SCENE)
        inner = json.loads((self.attempt / "scan/result.json").read_bytes())
        inner["observations"] = list(evidence.CANCELLATION_SCENE.trace)
        self.write("scan/result.json", inner)
        with self.assertRaises(evidence.ScanEvidenceError):
            evidence.publish_scan_bundle(
                self.attempt, self.result, scene=evidence.WRITE_REJECT_SCENE
            )

    def test_a_final_result_may_not_keep_a_pending_verdict(self) -> None:
        pending = copy.deepcopy(self.result)
        pending["session_status"] = "pending"
        self.result = pending
        with self.assertRaises(evidence.ScanEvidenceError):
            self.publish()

    def test_a_scan_typed_prepared_result_cannot_ride_a_subscription_bundle(
        self,
    ) -> None:
        self._rewrite_as_scene(evidence.NOTIFICATION_SCENE)
        prepared = copy.deepcopy(self.result)
        prepared["session_status"] = "pending"
        prepared["scope"] = evidence.SCAN_SCENE.scope("android")
        prepared["rf"] = evidence.SCAN_SCENE.rf
        self.write("prepared-result.json", prepared)
        with self.assertRaises(evidence.ScanEvidenceError):
            evidence.publish_scan_bundle(
                self.attempt, self.result, scene=evidence.NOTIFICATION_SCENE
            )

    def test_a_scan_scope_cannot_carry_a_subscription_trace(self) -> None:
        self._rewrite_as_scene(evidence.NOTIFICATION_SCENE)
        with self.assertRaises(evidence.ScanEvidenceError):
            evidence.publish_scan_bundle(self.attempt, self.result)

    def test_a_subscription_scope_cannot_keep_the_scan_trace(self) -> None:
        self._rewrite_as_scene(evidence.NOTIFICATION_SCENE)
        inner = json.loads((self.attempt / "scan/result.json").read_bytes())
        inner["observations"] = list(evidence.SCAN_SCENE.trace)
        self.write("scan/result.json", inner)
        with self.assertRaises(evidence.ScanEvidenceError):
            evidence.publish_scan_bundle(
                self.attempt, self.result, scene=evidence.NOTIFICATION_SCENE
            )

    def test_prepared_primary_error_cannot_be_dropped_by_final_result(self) -> None:
        prepared = copy.deepcopy(self.result)
        prepared.update(
            status="failed",
            primary_error="OSError: inner publication failed",
            cleanup_order=self.result["cleanup_order"],
        )
        self.write("prepared-result.json", prepared)
        with self.assertRaises(ValueError):
            self.publish()

    def test_failed_released_lease_preserves_observed_identity_mismatch(self) -> None:
        self.publish()
        lease = self.attempt / "fixture/lease.json"
        document = json.loads(lease.read_bytes())
        document["observed_resource_identity"] = "nrf-usb:OTHER"
        lease.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        result = json.loads((self.attempt / "result.json").read_bytes())
        result.update(
            status="failed",
            host_gate_status="failed",
            fixture_status="failed",
            primary_error=None,
            cleanup_errors=["fixture selected identity mismatch"],
        )
        (self.attempt / "result.json").write_text(
            json.dumps(result, sort_keys=True), encoding="utf-8"
        )
        self.reseal()
        self.assertEqual(evidence.verify_scan_bundle(self.attempt), result)
        self.assertEqual(
            json.loads(lease.read_bytes())["observed_resource_identity"],
            "nrf-usb:OTHER",
        )

    def test_success_rejects_observed_identity_mismatch(self) -> None:
        self.publish()
        lease = self.attempt / "fixture/lease.json"
        document = json.loads(lease.read_bytes())
        document["observed_resource_identity"] = "nrf-usb:OTHER"
        lease.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        self.reseal()
        with self.assertRaises(ValueError):
            evidence.verify_scan_bundle(self.attempt)

    def test_resealed_lease_pid_and_close_receipt_tampering_fails(self) -> None:
        self.publish()
        lease = self.attempt / "fixture/lease.json"
        document = json.loads(lease.read_bytes())
        document.update(fixture_pid=None, fixture_pgid=None)
        lease.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        self.reseal()
        with self.assertRaises(ValueError):
            evidence.verify_scan_bundle(self.attempt)

    def test_resealed_noncanonical_close_receipt_cannot_pass(self) -> None:
        self.publish()
        bad = {
            **self.fixture["close"],
            "boot_nonce": "aa",
            "supported_settings": 0,
            "current_settings": 1024,
        }
        for relative in (
            "fixture/close.json",
            "scan/result.json",
            "scan/prepared-result.json",
        ):
            path = self.attempt / relative
            document = json.loads(path.read_bytes())
            if relative == "fixture/close.json":
                document["receipt"] = bad
            else:
                document["fixture_close"] = bad
            path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        result = json.loads((self.attempt / "result.json").read_bytes())
        result["fixture_close"] = bad
        (self.attempt / "result.json").write_text(
            json.dumps(result, sort_keys=True), encoding="utf-8"
        )
        self.reseal()
        with self.assertRaises(ValueError):
            evidence.verify_scan_bundle(self.attempt)

    def test_inner_handoff_requires_driver_and_fixture_plan_identity(self) -> None:
        self.publish()
        path = self.attempt / "scan/result.json"
        inner = json.loads(path.read_bytes())
        inner["driver_session_id"] = "foreign-session"
        inner["fixture"]["profile_sha256"] = "0" * 64
        path.write_text(json.dumps(inner, sort_keys=True), encoding="utf-8")
        self.reseal()
        with self.assertRaises(ValueError):
            evidence.verify_scan_bundle(self.attempt)

    def test_inner_prepared_primary_error_must_survive_final_inner_result(self) -> None:
        self.publish()
        path = self.attempt / "scan/prepared-result.json"
        prepared = json.loads(path.read_bytes())
        prepared["primary_error"] = "OSError: inner cleanup failed"
        path.write_text(json.dumps(prepared, sort_keys=True), encoding="utf-8")
        self.reseal()
        with self.assertRaises(ValueError):
            evidence.verify_scan_bundle(self.attempt)

    def test_resealed_identity_lease_fixture_cleanup_and_inner_trace_tampering_fails(
        self,
    ) -> None:
        self.publish()
        mutations = (
            (
                "result identity",
                "result.json",
                {"run_id": "12345678-1234-4234-8234-000000000000"},
            ),
            ("lease", "fixture/lease.json", {"resource_identity": "nrf-usb:OTHER"}),
            (
                "fixture proof",
                "fixture/close.json",
                {"receipt": {**self.fixture["close"], "resources_closed": False}},
            ),
            ("cleanup order", "result.json", {"cleanup_order": ["phone.finish"]}),
            ("inner trace", "scan/result.json", {"observations": TRACE[:-1]}),
        )
        for label, relative, changes in mutations:
            with self.subTest(label=label):
                path = self.attempt / relative
                document = json.loads(path.read_bytes())
                document.update(changes)
                path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
                self.reseal()
                with self.assertRaises(ValueError):
                    evidence.verify_scan_bundle(self.attempt)
                # Restore the original sealed bytes for the next independent attack.
                self.tearDownAttempt()

    def reseal(self) -> None:
        manifest_path = self.attempt / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        files, directories, _ = evidence._snapshot(
            self.attempt, evidence.time.monotonic() + evidence.MAX_SECONDS
        )
        result = json.loads((self.attempt / "result.json").read_bytes())
        manifest.update(evidence._summary(result), files=files, directories=directories)
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    def tearDownAttempt(self) -> None:
        # Rebuild the complete fixture instead of resealing attacker-controlled
        # bytes. The test deliberately proves that a forged inventory is not a pass.
        shutil.rmtree(self.attempt)
        self.attempt.mkdir()
        self._make_success()
        self.publish()

    def test_late_success_publication_retracts_manifest_on_deadline(self) -> None:
        original = evidence.publish_new_json

        def delayed(path, value, **kwargs):
            digest = original(path, value, **kwargs)
            if path.name == "manifest.json":
                time.sleep(0.7)
            return digest

        with (
            patch.object(evidence, "publish_new_json", side_effect=delayed),
            self.assertRaises(TimeoutError),
        ):
            evidence.publish_scan_bundle(
                self.attempt, self.result, deadline=time.monotonic() + 0.5
            )
        self.assertTrue((self.attempt / "result.json").exists())
        self.assertFalse((self.attempt / "manifest.json").exists())

    def test_failed_start_record_preserves_verified_close_without_fake_primary(
        self,
    ) -> None:
        (self.attempt / "fixture/start.json").unlink()
        self.result.update(
            status="failed",
            host_gate_status="failed",
            fixture_status="failed",
            primary_error=None,
            cleanup_errors=["fixture.start-record: publication failed"],
        )
        published = evidence.publish_scan_bundle(self.attempt, self.result)
        verified = evidence.verify_scan_bundle(published.parent)
        self.assertEqual(verified["status"], "failed")
        self.assertIsNone(verified["primary_error"])
        self.assertEqual(verified["fixture_status"], "failed")
        self.assertEqual(verified["fixture_close"], self.fixture["close"])
        self.assertFalse((self.attempt / "fixture/start.json").exists())

    def test_failed_attestation_preserves_inputs_without_fabricating_receipt(
        self,
    ) -> None:
        (self.attempt / "fixture" / fixture_session.RECEIPT_NAME).unlink()
        shutil.rmtree(self.attempt / "scan")
        (self.attempt / "prepared-result.json").unlink(missing_ok=True)
        self.write(
            "fixture/start.json",
            {
                "run_id": RUN,
                "status": "failed",
                "resource_identity": RESOURCE,
                "error": "installed attestation unavailable",
            },
        )
        self.write(
            "fixture/close.json",
            {
                "run_id": RUN,
                "resource_identity": RESOURCE,
                "status": "failed",
                "receipt": None,
                "error": "passive closure not verified",
            },
        )
        self.write(
            "phone.json",
            {
                "run_id": RUN,
                "platform": "android",
                "device": DEVICE,
                "status": "not_acquired",
                "actions": [],
                "primary_error": None,
                "cleanup_errors": [],
                "initial_state": None,
                "final_state": None,
            },
        )
        self.result.update(
            status="failed",
            host_gate_status="failed",
            scenario_status="not_run",
            session_status="not_run",
            phone_status="not_acquired",
            fixture_status="failed",
            fixture_close=None,
            primary_error="installed attestation unavailable",
            cleanup_errors=["fixture.close: passive closure not verified"],
            cleanup_order=[
                "fixture.close",
                "fixture.close-record",
                "fixture.lease-release",
                "fixture.lease-record",
                "bundle.prepare",
            ],
        )
        published = evidence.publish_scan_bundle(self.attempt, self.result)
        self.assertFalse(
            (self.attempt / "fixture" / fixture_session.RECEIPT_NAME).exists()
        )
        self.assertEqual(evidence.verify_scan_bundle(published.parent), self.result)

    def test_incomplete_fixture_snapshot_cannot_claim_passed_start(self) -> None:
        for name in (
            fixture_session.CONTRACT_NAME,
            fixture_session.PROFILE_NAME,
            fixture_session.REQUEST_NAME,
            fixture_session.RECEIPT_NAME,
        ):
            (self.attempt / "fixture" / name).unlink()
        self.result.update(
            status="failed",
            host_gate_status="failed",
            fixture_status="failed",
            primary_error=None,
            cleanup_errors=["fixture startup snapshot incomplete"],
        )
        with self.assertRaises(ValueError):
            self.publish()

    def test_incomplete_fixture_snapshot_rejects_lease_resource_tamper(self) -> None:
        for name in (
            fixture_session.CONTRACT_NAME,
            fixture_session.PROFILE_NAME,
            fixture_session.REQUEST_NAME,
            fixture_session.RECEIPT_NAME,
        ):
            (self.attempt / "fixture" / name).unlink()
        start = self.attempt / "fixture/start.json"
        start_doc = json.loads(start.read_bytes())
        start_doc.update(status="failed", error="fixture admission incomplete")
        start.write_text(json.dumps(start_doc, sort_keys=True), encoding="utf-8")
        lease = self.attempt / "fixture/lease.json"
        lease_doc = json.loads(lease.read_bytes())
        lease_doc["resource_identity"] = "nrf-usb:OTHER"
        lease.write_text(json.dumps(lease_doc, sort_keys=True), encoding="utf-8")
        self.result.update(
            status="failed",
            host_gate_status="failed",
            fixture_status="failed",
            primary_error=None,
            cleanup_errors=["fixture lease identity drift"],
        )
        with self.assertRaises(ValueError):
            self.publish()

    def test_failed_partial_bundle_is_verifiable_without_success_receipts(self) -> None:
        partial = self.root / "partial"
        partial.mkdir()
        result = {
            "schema_version": 1,
            "scope": evidence.SCOPE,
            "run_id": RUN,
            "platform": "android",
            "device": DEVICE,
            "app_id": APP,
            "apk_sha256": "a" * 64,
            "status": "failed",
            "host_gate_status": "failed",
            "rf": "scan-ui-observation",
            "scenario_status": "not_run",
            "session_status": "not_run",
            "phone_status": "not_acquired",
            "fixture_status": "not_acquired",
            "fixture_close": None,
            "primary_error": "pre-lease validation failed",
            "cleanup_errors": [],
            "phone_cleanup_errors": [],
            "cleanup_order": [],
        }
        evidence.publish_scan_bundle(partial, result)
        self.assertEqual(evidence.verify_scan_bundle(partial), result)


if __name__ == "__main__":
    unittest.main()
