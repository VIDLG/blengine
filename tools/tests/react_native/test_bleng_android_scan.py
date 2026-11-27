"""Producer-level Android managed-scan contract: wiring, order and cleanup ownership.

The bundle facts have their own exhaustive suites (`test_bleng_scan_evidence`). This file
answers the question those cannot: does the producer hand the prepared envelope the right
scene, the right profile-derived target and the right cleanup ownership — and does it do
that only after the fixture is live and the App installed? A wiring mistake here (a
mis-set `release_retained_app`, a target role, a stage ordered too early) is invisible to a
plan test and reaches the device as a hang, so it is pinned here instead.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from typing import Self
from unittest.mock import patch

from tests.peripherals.nrf52840.direct_fixture import AdvertisingCloseReceipt
from tools.react_native import bleng_android_scan
from tools.react_native.bleng_scan_evidence import (
    IO_SCENE,
    NOTIFICATION_SCENE,
    SCAN_SCENE,
)
from tools.react_native.bleng_scan_runner import MAX_SCENE_SECONDS, PreparedScanHandoff

DEVICE = "3ac5cc09"
APP_ID = "com.blehubblenghost"
ACTIVITY = "com.blehubblenghost.MainActivity"
FIXTURE_PORT = "/dev/cu.usbmodem0010502520281"
FIXTURE_IDENTITY = "jlink:1050252028"
SERVICE_UUID = "fff00000-0000-4000-8000-000000000000"
SUBSCRIPTION_UUID = "fff00000-0000-4000-8000-000000000001"
IO_UUID = "fff00000-0000-4000-8000-000000000002"
IO_PAYLOAD = b"\x01\x02"
NOTIFY_VALUE = "1 bytes [ef]"


def _release_shaped_apk(path: Path) -> str:
    """A minimal APK the real release gate accepts, with its reviewed digest."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in (
            ("assets/index.android.bundle", b"bundle"),
            ("classes.dex", b"dex"),
            ("lib/arm64-v8a/libbleng_uniffi.so", b"so"),
            ("lib/arm64-v8a/libBleHubReactNativeBleng.so", b"so"),
        ):
            archive.writestr(name, payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _FakePhone:
    """A phone lease that records its own lifecycle instead of touching a device."""

    def __init__(self, device: str, run_id: str, path: Path, events: list) -> None:
        self.device, self.run_id, self.path = device, run_id, path
        self.events = events
        self.document = {"status": "passed"}
        self.acquired = False

    def acquire(self) -> None:
        self.events.append("phone.acquire")
        self.acquired = True
        self.path.write_text(json.dumps(self.document), encoding="utf-8")

    def require_owner(self) -> None:
        if not self.acquired:
            raise RuntimeError("phone lease not held")

    def finish_app_observation(self, primary, *, finalize_owned) -> list[str]:
        self.events.append("phone.finish")
        finalize_owned()
        return []


class _FakePermissionScope:
    def __init__(self, events: list, **kwargs) -> None:
        self.events = events
        self.kwargs = kwargs
        self.finished = False

    def prep(self) -> None:
        self.events.append("permissions.prep")

    def finish(self, primary=None) -> list[str]:
        self.events.append("permissions.finish")
        self.finished = True
        return []


class _FakeCandidate:
    def __init__(self, events: list) -> None:
        self.events = events

    def finalize(
        self, primary, *, deadline: float, release_retained: bool
    ) -> list[str]:
        self.events.append(("candidate.finalize", release_retained))
        return []


class _FakeHost:
    def __init__(self, events: list, **kwargs) -> None:
        self.events = events
        self.kwargs = kwargs
        self.events.append(("host", sorted(kwargs)))
        self.test_apk = kwargs["test_apk"]
        self.candidate = _FakeCandidate(events)
        self.closed = False

    def prepare(self) -> None:
        self.events.append("host.prepare")

    def launch(self) -> None:
        self.events.append("host.launch")

    def close_automation(self, *, deadline: float) -> None:
        self.events.append("host.close-automation")
        self.closed = True

    def require_automation_closed(self) -> None:
        if not self.closed:
            raise RuntimeError("automation still open")


class _FakePlan:
    def __init__(self, attempt: Path, session_id: str) -> None:
        self.session_id = session_id
        self.port = FIXTURE_PORT
        self.resource_identity = FIXTURE_IDENTITY
        self.profile_path = attempt / "fixture/profile.json"
        self.receipt_path = attempt / "fixture/receipt.json"
        self.contract = _FakeContract()


class _FakeContract:
    local_name = "BleHubMultiRfV2DKA"
    service_uuid = SERVICE_UUID
    fixture_role = "multifunction-rf"


class _FakeLease:
    def __init__(self, events: list) -> None:
        self.events = events
        self.identity = FIXTURE_IDENTITY

    def require(self, kind: str, serial: str) -> None:
        if f"{kind}:{serial}" != FIXTURE_IDENTITY:
            raise RuntimeError("lease identity mismatch")

    def __enter__(self) -> Self:
        self.events.append("lease.enter")
        return self

    def __exit__(self, *exc: object) -> None:
        self.events.append("lease.exit")


class _FakeFixture:
    def __init__(self, port, profile_path, *, lease, fixture_session, events) -> None:
        self.events = events
        self.port, self.profile_path = port, profile_path
        self.lease, self.session = lease, fixture_session
        self.started = False
        self.resources_closed = False

    def start(self, *, deadline: float) -> None:
        self.events.append("fixture.start")
        self.started = True

    def require_active_session(self, plan) -> None:
        if not self.started or plan is not self.session:
            raise RuntimeError("fixture session not active")

    def settle_no_peer(self, *, deadline: float) -> None:
        if not self.started:
            raise RuntimeError("fixture never started advertising")

    def stop_advertising_and_close(self, *, deadline: float):
        return AdvertisingCloseReceipt(
            session_id=self.session.session_id,
            resource_identity=self.session.resource_identity,
            boot_nonce="00" * 16,
            supported_settings=0,
            current_settings=0,
        )

    def close_resources(self) -> None:
        self.resources_closed = True


class _FakeDriver:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class AndroidManagedScanProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.output = self.root / "runs"
        self.output.mkdir()
        self.events: list = []
        self.apk = self.root / "candidate.apk"
        self.apk_sha256 = _release_shaped_apk(self.apk)
        self.test_apk = self.root / "candidate-androidTest.apk"
        self.test_apk.write_bytes(b"synthetic instrumentation APK")
        self.test_apk_sha256 = hashlib.sha256(self.test_apk.read_bytes()).hexdigest()
        self.contract = self.root / "contract.json"
        self.profile = self.root / "profile.json"
        self.contract.write_text("{}", encoding="utf-8")
        self.profile.write_text("{}", encoding="utf-8")
        self.run_id = str(uuid.uuid4())
        self.fixture: _FakeFixture | None = None
        self.permission_scope: _FakePermissionScope | None = None
        self.scene_failure: BaseException | None = None
        self.scene_calls: list[dict] = []
        self.published: list[dict] = []

        self.enterContext(
            patch.object(
                bleng_android_scan, "preflight_fixture", return_value=SERVICE_UUID
            )
        )
        self.enterContext(
            patch.object(
                bleng_android_scan,
                "AndroidRadioController",
                side_effect=lambda device, run_id, path: _FakePhone(
                    device, run_id, path, self.events
                ),
            )
        )
        self.enterContext(
            patch.object(bleng_android_scan, "port_lease", side_effect=self._lease)
        )
        self.enterContext(
            patch.object(
                bleng_android_scan.fixture_session,
                "prepare_session",
                side_effect=self._prepare_session,
            )
        )
        self.enterContext(
            patch.object(
                bleng_android_scan.fixture_session, "verify_receipt", return_value=None
            )
        )
        self.enterContext(
            patch.object(bleng_android_scan, "DirectFixture", side_effect=self._fixture)
        )
        self.enterContext(
            patch.object(
                bleng_android_scan,
                "AndroidBluetoothPermissionScope",
                side_effect=self._permission_scope_factory,
            )
        )
        self.enterContext(
            patch.object(
                bleng_android_scan,
                "AndroidNativeHost",
                side_effect=lambda **kwargs: _FakeHost(self.events, **kwargs),
            )
        )
        self.enterContext(
            patch.object(bleng_android_scan, "NativeAndroidSceneDriver", _FakeDriver)
        )
        self.enterContext(
            patch.object(
                bleng_android_scan,
                "subscription_target",
                return_value=(SUBSCRIPTION_UUID, "notify", NOTIFY_VALUE),
            )
        )
        self.enterContext(
            patch.object(
                bleng_android_scan, "io_target", return_value=(IO_UUID, IO_PAYLOAD)
            )
        )
        self.enterContext(
            patch.object(
                bleng_android_scan,
                "read_write_target",
                return_value=(IO_UUID, IO_PAYLOAD),
            )
        )
        self.enterContext(
            patch.object(bleng_android_scan, "run_prepared_scene", self._run_scene)
        )
        self.enterContext(
            patch.object(
                bleng_android_scan, "publish_scan_bundle", side_effect=self._publish
            )
        )

    # --- seams ---------------------------------------------------------------------

    def _lease(self, port: str, *, purpose: str) -> _FakeLease:
        self.events.append("lease.enter")
        return _FakeLease(self.events)

    def _prepare_session(self, contract, profile, directory, **kwargs) -> _FakePlan:
        self.events.append("fixture.prepare")
        directory.mkdir(parents=True, exist_ok=True)
        return _FakePlan(directory.parent, kwargs["session_id"])

    def _fixture(self, port, profile_path, *, lease, fixture_session) -> _FakeFixture:
        self.events.append("fixture.construct")
        self.fixture = _FakeFixture(
            port,
            profile_path,
            lease=lease,
            fixture_session=fixture_session,
            events=self.events,
        )
        return self.fixture

    def _permission_scope_factory(self, **kwargs) -> _FakePermissionScope:
        self.permission_scope = _FakePermissionScope(self.events, **kwargs)
        return self.permission_scope

    def _publish(self, attempt: Path, result: dict, *, deadline=None, scene=None):
        self.published.append({"attempt": attempt, "result": result, "scene": scene})
        self.events.append("bundle.publish")
        return attempt

    def _run_scene(self, **kwargs) -> Path:
        """Stand in for the prepared envelope, keeping its admission contract."""
        self.scene_calls.append(kwargs)
        handoff: PreparedScanHandoff = kwargs["handoff"]
        output: Path = kwargs["output"]
        output.mkdir(parents=True, exist_ok=True)
        if self.scene_failure is not None:
            raise self.scene_failure
        handoff._admitted = True
        handoff.cleanup_deadline = kwargs["deadline"]
        inner = {
            "scope": kwargs["scene"].inner_scope,
            "run_id": self.run_id,
            "device": DEVICE,
            "app_id": APP_ID,
            "scenario_status": "passed",
            "session_status": "passed",
            "phone_cleanup_errors": [],
            "phone_journal": {"status": "passed"},
            "fixture_close": {"session_id": self.run_id},
            "cleanup_errors": [],
            "primary_error": None,
            "cleanup_order": ["phone.finish"],
        }
        (output / "result.json").write_text(json.dumps(inner), encoding="utf-8")
        return output

    # --- actions -------------------------------------------------------------------

    def scan(self, *, scene=SCAN_SCENE) -> dict:
        path = bleng_android_scan.run_android_scan(
            device=DEVICE,
            app_id=APP_ID,
            activity=ACTIVITY,
            apk=self.apk,
            apk_sha256=self.apk_sha256,
            test_apk=self.test_apk,
            test_apk_sha256=self.test_apk_sha256,
            fixture_contract=self.contract,
            fixture_profile=self.profile,
            fixture_port=FIXTURE_PORT,
            fixture_resource_identity=FIXTURE_IDENTITY,
            output_root=self.output,
            timeout=900,
            cleanup_seconds=30,
            authorize_install=True,
            authorize_fixture_start=True,
            authorize_bluetooth_permissions=True,
            use_root=True,
            cwd=self.root,
            run_id=self.run_id,
            scene=scene,
        )
        self.assertEqual(path, self.output / self.run_id)
        return self.published[-1]["result"]

    # --- contract ------------------------------------------------------------------

    def test_fixture_is_live_before_the_app_is_installed_or_launched(self) -> None:
        self.scan()
        order = self.events
        self.assertLess(order.index("fixture.prepare"), order.index("fixture.start"))
        self.assertLess(order.index("fixture.start"), order.index("host.prepare"))
        self.assertLess(order.index("host.prepare"), order.index("host.launch"))
        # No device mutation may precede the live installed-image proof.
        self.assertLess(order.index("fixture.start"), order.index("permissions.prep"))
        self.assertLess(
            order.index("fixture.start"),
            order.index(("candidate.finalize", True))
            if ("candidate.finalize", True) in order
            else len(order),
        )

    def test_prepared_envelope_owns_scene_cleanup_for_the_in_process_lane(self) -> None:
        self.scan()
        (call,) = self.scene_calls
        # The App process *is* the instrumentation host, so the envelope must not keep it.
        self.assertIs(call["release_retained_app"], True)
        self.assertEqual(call["app_id"], APP_ID)
        self.assertEqual(call["cleanup_seconds"], 30)
        self.assertIs(call["scene"], SCAN_SCENE)
        self.assertIsInstance(call["handoff"], PreparedScanHandoff)
        self.assertEqual(call["before_phone_release"], self.permission_scope.finish)
        self.assertIs(call["progress"], None)
        # One scene-sized window inside the run budget, never an extension of it.
        self.assertGreater(call["deadline"], time.monotonic())
        self.assertLessEqual(call["deadline"] - time.monotonic(), MAX_SCENE_SECONDS)
        self.assertIsNone(call["scene_driver"].kwargs["subscription"])
        self.assertIsNone(call["scene_driver"].kwargs["gatt"])

    def test_subscription_scene_target_comes_from_the_profile_role(self) -> None:
        self.scan(scene=NOTIFICATION_SCENE)
        (call,) = self.scene_calls
        self.assertEqual(
            call["scene_driver"].kwargs["subscription"],
            {"uuid": SUBSCRIPTION_UUID, "mode": "notify", "value": NOTIFY_VALUE},
        )
        self.assertIsNone(call["scene_driver"].kwargs["gatt"])

    def test_io_scene_target_carries_the_provisioned_write_payload(self) -> None:
        self.scan(scene=IO_SCENE)
        (call,) = self.scene_calls
        self.assertEqual(
            call["scene_driver"].kwargs["gatt"],
            {"uuid": IO_UUID, "write": IO_PAYLOAD.decode("ascii")},
        )
        self.assertIsNone(call["scene_driver"].kwargs["subscription"])

    def test_admitted_scene_reports_the_prepared_result_without_repeating_cleanup(
        self,
    ) -> None:
        result = self.scan()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["host_gate_status"], "pending")
        self.assertEqual(
            (result["scenario_status"], result["session_status"]),
            ("passed", "passed"),
        )
        self.assertEqual(result["fixture_status"], "passed")
        self.assertEqual(
            result["cleanup_order"],
            [
                "scan.run-prepared",
                "scan.result",
                "fixture.close-record",
                "fixture.lease-release",
                "fixture.lease-record",
                "bundle.prepare",
                "cleanup.complete",
            ],
        )
        # The inner envelope already stopped the App, closed the fixture and finished
        # the phone, so the outer producer must not repeat any of those actions.
        self.assertNotIn("candidate.finalize", result["cleanup_order"])
        self.assertNotIn("automation.close", result["cleanup_order"])
        self.assertNotIn("phone.finish", result["cleanup_order"])
        self.assertNotIn("fixture.close", result["cleanup_order"])
        self.assertIs(self.published[-1]["scene"], SCAN_SCENE)

    def test_scene_failure_still_releases_every_owned_stage_in_order(self) -> None:
        self.scene_failure = RuntimeError("scene refused to bind")
        result = self.scan()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["host_gate_status"], "failed")
        self.assertIn("scene refused to bind", result["primary_error"])
        self.assertEqual(
            result["cleanup_order"],
            [
                "permissions.restore",
                "automation.close",
                "candidate.finalize",
                "fixture.close",
                "fixture.close-record",
                "fixture.lease-release",
                "fixture.lease-record",
                "bundle.prepare",
                "phone.finish",
                "cleanup.complete",
            ],
        )
        # Outer ownership: the App the producer installed is always stopped.
        self.assertIn(("candidate.finalize", True), self.events)
        self.assertEqual(result["fixture_close"]["session_id"], self.run_id)
        self.assertEqual(result["phone_status"], "passed")

    def test_fixture_start_failure_never_constructs_or_installs_the_app(self) -> None:
        def refuse(fixture, *, deadline):
            raise RuntimeError("fixture is not advertising")

        with patch.object(_FakeFixture, "start", refuse):
            result = self.scan()
        self.assertEqual(self.scene_calls, [])
        self.assertFalse(any(event == "host.prepare" for event in self.events))
        self.assertNotIn(("candidate.finalize", True), self.events)
        self.assertEqual(result["status"], "failed")
        self.assertIn("not advertising", result["primary_error"])
        self.assertEqual(result["fixture_status"], "failed")
        self.assertTrue(self.fixture.resources_closed)
        self.assertIn("fixture.close", " ".join(result["cleanup_errors"]))


if __name__ == "__main__":
    unittest.main()
