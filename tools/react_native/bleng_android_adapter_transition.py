"""Adapter OFF -> ON recovery proved on the formal App's own native surface.

The host owns the phone's radio: it requires ON, turns it off, starts the fixture, runs the
formal App's instrumented scene, and turns the radio back on when that scene reports its refused
scan. The App owns only the product facts: a start attempted while the adapter is off must still
report the real adapter state, a scan in that state must not start, and the App's own retry must
produce a working owner, link and read once the radio returns.

This replaces the retired consumer's deep-link adapter scene. Ownership is unchanged: the host
still holds the phone lease, the fixture lease, the receipts and the final verdict.
"""

from __future__ import annotations

import math
import os
import shutil
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from traceback import format_exception_only
from typing import Any

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.direct_fixture import (
    AdvertisingCloseReceipt,
    DirectFixture,
)
from tests.peripherals.nrf52840.resource_lease import canonical_identity, port_lease
from tools.android import adb
from tools.android.state_control import AndroidRadioController
from tools.file_digest import sha256_file

from . import android_sdk
from .android_bluetooth_permissions import AndroidBluetoothPermissionScope
from .bleng_android_instrumentation import (
    HOST_MARKER_TAG,
    MarkerCapture,
    run_instrumentation,
)
from .capture_io import publish_new_json
from .rf_fixture import preflight_fixture
from .scene_targets import read_write_target

SCOPE = "formal-android-native-adapter-transition"
MARKER_TAG = "BLEHUB_RN_ADAPTER"
TEST_CLASS = "com.blehubblenghost.BlengAdapterTransitionInstrumentedTest"
TEST_PACKAGE = "com.blehubblenghost.test"
APP_PACKAGE = "com.blehubblenghost"
METHOD = "refusedScanRecoversThroughRetryOnceTheAdapterIsBack"
READ_WRITE_ROLE = "read-write"
# The App reports the refused scan; only then does the host turn the radio back on, so the
# App's own retry is a response to a real change and not to a timer.
REFUSAL_STAGE = "adapter-off"
# The adapter lane reporting the restored radio: the product fact that the change happened.
STREAM_STAGE = "adapter-stream-on"
RECOVERY_STAGE = "adapter-restored"
# One scene, in order: the refusal, the App's own recovery, then a normal scan/link/read.
EXPECTED_STAGES = (
    REFUSAL_STAGE,
    STREAM_STAGE,
    RECOVERY_STAGE,
    "scan-submitted",
    "scan-started",
    "advertisement",
    "scan-stopped",
    "connected",
    "gatt",
    "io",
    "done",
)
SYSTEM_TIMEOUT_MAX = 900.0
CLEANUP_TIMEOUT_MAX = 180.0


def _error(error: BaseException) -> str:
    return "".join(format_exception_only(error, show_group=True)).strip()


def _remaining(deadline: float, label: str) -> float:
    budget = deadline - time.monotonic()
    if not math.isfinite(budget) or budget <= 0:
        raise TimeoutError(f"{label} exceeded the run budget")
    return budget


def run_adapter_transition(
    *,
    device: str,
    app_apk: Path,
    app_apk_sha256: str,
    test_apk: Path,
    test_apk_sha256: str,
    fixture_port: str,
    fixture_contract: Path,
    fixture_profile: Path,
    fixture_resource_identity: str,
    output_root: Path,
    timeout: float,
    cleanup_seconds: float,
    authorize_install: bool,
    authorize_fixture_start: bool,
    authorize_bluetooth_permissions: bool,
    use_root: bool,
    cwd: Path,
    run_id: str | None = None,
) -> Path:
    """Run one bounded adapter OFF -> ON recovery scene; return the published result path."""
    for name, value, maximum in (
        ("timeout", timeout, SYSTEM_TIMEOUT_MAX),
        ("cleanup_seconds", cleanup_seconds, CLEANUP_TIMEOUT_MAX),
    ):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 < value <= maximum
        ):
            raise ValueError(f"{name} must be finite, positive and at most {maximum}")
    if authorize_install is not True or authorize_fixture_start is not True:
        raise ValueError(
            "explicit App/test install and fixture startup authorization required"
        )
    if authorize_bluetooth_permissions is not True:
        raise ValueError(
            "explicit BLUETOOTH_SCAN/CONNECT authorization required: root grant, readback "
            "and exact restore"
        )
    if type(use_root) is not bool:
        raise ValueError("use_root must be an explicit bool")
    if (
        type(device) is not str
        or not device.strip()
        or device.lower().startswith("emulator-")
    ):
        raise ValueError("an explicit physical Android device serial is required")
    kind, _, serial = fixture_resource_identity.partition(":")
    if not serial or canonical_identity(kind, serial) != fixture_resource_identity:
        raise ValueError("canonical stable fixture identity required")
    for label, path in (("contract", fixture_contract), ("profile", fixture_profile)):
        if not path.is_file():
            raise ValueError(f"missing fixture {label}: {path}")
    for label, value in (
        ("app_apk_sha256", app_apk_sha256),
        ("test_apk_sha256", test_apk_sha256),
    ):
        if (
            type(value) is not str
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError(f"{label} must be a lowercase SHA-256")

    service_uuid = preflight_fixture(
        fixture_contract=fixture_contract, fixture_profile=fixture_profile
    )
    run_id = str(uuid.uuid4()) if run_id is None else run_id
    if type(run_id) is not str or str(uuid.UUID(run_id)) != run_id:
        raise ValueError("run_id must be a canonical UUID")
    deadline = time.monotonic() + timeout
    attempt = output_root / run_id
    attempt.mkdir(parents=True, mode=0o700, exist_ok=False)

    for label, path, expected in (
        ("app", app_apk, app_apk_sha256),
        ("test", test_apk, test_apk_sha256),
    ):
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"missing {label} APK: {path}")
        measured = sha256_file(path)
        if measured != expected:
            raise ValueError(f"{label} APK does not match the authorized SHA-256")
        shutil.copyfile(path, attempt / f"{label}.apk")

    result: dict[str, Any] = {
        "schema_version": 1,
        "scope": SCOPE,
        "run_id": run_id,
        "platform": "android",
        "device": device,
        "app_id": APP_PACKAGE,
        "app_apk_sha256": app_apk_sha256,
        "test_apk_sha256": test_apk_sha256,
        "status": "failed",
        "rf": "adapter-transition",
        "host_gate_status": "failed",
        "scenario_status": "not_run",
        "phone_status": "not_acquired",
        "fixture_status": "not_acquired",
        "stages": [],
        "primary_error": None,
        "primary_traceback": None,
        "cleanup_errors": [],
        "phone_cleanup_errors": [],
        "cleanup_order": [],
    }
    request = {
        "schema_version": 1,
        "scope": SCOPE,
        "run_id": run_id,
        "device": device,
        "app_apk_sha256": app_apk_sha256,
        "test_apk_sha256": test_apk_sha256,
        "fixture": {
            "port": fixture_port,
            "contract": fixture_contract.name,
            "profile": fixture_profile.name,
            "resource_identity": fixture_resource_identity,
        },
        "authorize_install": True,
        "authorize_fixture_start": True,
        "authorize_bluetooth_permissions": True,
        "use_root": use_root,
        "timeout": timeout,
        "cleanup_seconds": cleanup_seconds,
        "host_pid": os.getpid(),
        "host_pgid": os.getpgid(0),
    }
    publish_new_json(attempt / "request.json", request)

    phone: AndroidRadioController | None = None
    lease_context: Any = None
    plan: fixture_session.FixtureSession | None = None
    fixture: DirectFixture | None = None
    fixture_started = False
    permission_scope: AndroidBluetoothPermissionScope | None = None
    capture: MarkerCapture | None = None
    primary: BaseException | None = None
    try:
        phone = AndroidRadioController(device, run_id, attempt / "phone.json")
        phone.acquire()
        result["phone_status"] = "running"
        _remaining(deadline, "phone lease")
        # The scene starts from a known ON phone: the host proves it before it turns it off.
        phone.initial()
        _remaining(deadline, "initial radio observation")

        with (attempt / "install-app.log").open("x", encoding="utf-8") as stream:
            installed_with_root = android_sdk.install_apk(
                device,
                attempt / "app.apk",
                log=stream,
                test_only=False,
                use_root=use_root,
                deadline=deadline,
                require_owner=phone.require_owner,
                cwd=cwd,
            )
        with (attempt / "install-test.log").open("x", encoding="utf-8") as stream:
            android_sdk.install_apk(
                device,
                attempt / "test.apk",
                log=stream,
                test_only=True,
                use_root=use_root,
                deadline=deadline,
                require_owner=phone.require_owner,
                cwd=cwd,
            )
        result["install_mode"] = {"installedWithRoot": installed_with_root}
        _remaining(deadline, "install")

        (attempt / "platform").mkdir(mode=0o700, exist_ok=True)
        permission_scope = AndroidBluetoothPermissionScope(
            device=device,
            run_id=run_id,
            evidence_path=attempt / "platform/permission-scope.json",
            package=APP_PACKAGE,
            phone_guard=phone.require_owner,
            use_root=use_root,
        )
        permission_scope.prep()
        result["permission_scope"] = "prepared"
        _remaining(deadline, "permissions")

        lease_context = port_lease(
            fixture_port, purpose="BleHub adapter transition fixture"
        )
        lease = lease_context.__enter__()
        lease.require(kind, serial)
        if lease.identity != fixture_resource_identity:
            raise RuntimeError("selected fixture differs from the authorized identity")
        plan = fixture_session.prepare_session(
            fixture_contract,
            fixture_profile,
            attempt / "fixture",
            port=fixture_port,
            service_uuid=service_uuid,
            session_id=run_id,
        )
        read_write_uuid, provisioned = read_write_target(plan, READ_WRITE_ROLE)
        fixture = DirectFixture(
            plan.port, plan.profile_path, lease=lease, fixture_session=plan
        )
        fixture.start(deadline=deadline)
        fixture.require_active_session(plan)
        fixture_started = True
        result["fixture_status"] = "passed"
        result["fixture"] = {
            "port": fixture_port,
            "resource_identity": fixture_resource_identity,
            "session_id": plan.session_id,
            "service_uuid": service_uuid,
            "local_name": plan.contract.local_name,
            "read_uuid": read_write_uuid,
            "read_hex": provisioned.hex(),
        }
        _remaining(deadline, "fixture start")

        # The App must start its scene with the adapter already off.
        phone.off()
        result["radio"] = "off"
        _remaining(deadline, "radio off")

        arguments = {
            "blehubAdapterTransition": "adapter-transition",
            "serviceUuid": service_uuid,
            "localName": plan.contract.local_name,
            "readUuid": read_write_uuid,
            "readHex": provisioned.hex(),
        }

        def on_stage(stage: str) -> None:
            if stage == REFUSAL_STAGE:
                # The App reported the refusal the host caused; this is the only moment the
                # radio is turned back on, so the App's recovery is a response to a real fact.
                assert phone is not None
                phone.on()
                result["radio"] = "restored"

        # Scoped by its own sentinel: the device's log is replayed first, and it is not ours
        # to clear.
        capture = MarkerCapture(
            device, deadline, tag=MARKER_TAG, host_tag=HOST_MARKER_TAG
        )
        result["scenario_status"] = "running"
        output = run_instrumentation(
            device,
            arguments,
            test_class=TEST_CLASS,
            method=METHOD,
            expected_stages=EXPECTED_STAGES,
            capture=capture,
            on_stage=on_stage,
            advance=fixture.drain_events,
            deadline=deadline,
            require_owner=phone.require_owner,
            log=attempt / "instrumentation.log",
        )
        result["stages"] = output["stages"]
        result["instrumentation_output"] = output["summary"]
        result["scenario_status"] = "passed"
        result["status"] = "passed"
        result["host_gate_status"] = "pending"
    except BaseException as error:  # noqa: BLE001 -- finish owned cleanup before propagating
        primary = error
        result["primary_error"] = _error(error)
        result["primary_traceback"] = traceback.format_exception(error)
        if result["scenario_status"] == "running":
            result["scenario_status"] = "failed"

    cleanup_deadline = time.monotonic() + cleanup_seconds

    def cleanup(label: str, action: Callable[[], None]) -> bool:
        result["cleanup_order"].append(label)
        try:
            action()
            return True
        except BaseException as error:  # noqa: BLE001 -- continue independent cleanup
            result["cleanup_errors"].append(f"{label}: {_error(error)}")
            return False

    if capture is not None:
        cleanup("markers.close", capture.close)

    if primary is not None:

        def dump_device_log() -> None:
            command = adb.adb_command(device, "logcat", "-d", "-v", "brief")
            output = adb.run_command(command, timeout=60, check=True).stdout
            (attempt / "device-logcat.txt").write_text(output, encoding="utf-8")

        cleanup("device.log", dump_device_log)

    if primary is not None and phone is not None:

        def stop_owned_app() -> None:
            """Bound a stuck scene to our own installed candidate."""
            path = adb.run_command(
                adb.adb_command(device, "shell", "pm", "path", APP_PACKAGE),
                timeout=10,
                check=True,
            ).stdout.strip()
            installed = path.removeprefix("package:").strip()
            if not installed:
                return
            digest = adb.run_command(
                adb.adb_command(device, "shell", "sha256sum", installed),
                timeout=15,
                check=True,
            ).stdout.split()[0]
            if digest != app_apk_sha256:
                raise RuntimeError("refusing to stop a foreign App installation")
            adb.run_command(
                adb.adb_command(device, "shell", "am", "force-stop", APP_PACKAGE),
                timeout=15,
                check=True,
            )
            result["owned_app_stopped"] = True

        cleanup("app.stop-after-failure", stop_owned_app)

    if permission_scope is not None:
        scope = permission_scope
        cleanup("permissions.restore", lambda: scope.finish(primary))

    if fixture is not None and plan is not None:

        def close_fixture() -> None:
            assert fixture is not None and plan is not None
            if not fixture_started:
                fixture.close_resources()
                return
            fixture.settle_no_peer(deadline=cleanup_deadline)
            receipt = fixture.stop_advertising_and_close(deadline=cleanup_deadline)
            if (
                type(receipt) is not AdvertisingCloseReceipt
                or receipt.session_id != plan.session_id
                or receipt.resource_identity != fixture_resource_identity
            ):
                raise RuntimeError("fixture passive close receipt is foreign")
            result["fixture_close"] = asdict(receipt)

        cleanup("fixture.close", close_fixture)

    if lease_context is not None:

        def release_fixture_lease() -> None:
            lease_context.__exit__(None, None, None)

        cleanup("fixture.lease-release", release_fixture_lease)

    if phone is not None:

        def finish_phone() -> None:
            assert phone is not None
            result["phone_cleanup_errors"] = phone.finish(primary)

        cleanup("phone.finish", finish_phone)
        result["phone_status"] = phone.document["status"]

    if result["cleanup_errors"] or result["phone_cleanup_errors"]:
        result["status"] = "failed"
        result["host_gate_status"] = "failed"
    path = attempt / "result.json"
    publish_new_json(path, result)
    if primary is not None:
        raise primary
    return path
