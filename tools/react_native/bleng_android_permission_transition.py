"""Permission DENIED -> ACTIVE(read) -> OS death -> RESTORED on the formal App's surface.

The host owns every OS action: it requires both grants, revokes them, grants them back,
revokes BLUETOOTH_CONNECT while a read is parked inside the fixture, proves the OS ended that
exact process, and only then runs a fresh process. The App owns the product facts: it reports
the real authorization, a scan under a denied permission ends with a permission failure, and
a fresh process delivers the provisioned value.

Three phases, three processes. "Recovered" is therefore a process-level fact, not a new owner
inside the same runtime, and the death phase cannot be confused with a clean close.
"""

from __future__ import annotations

import json
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
from tools.android.permission_control import (
    AndroidPermissionController,
    AndroidProcessIdentity,
)
from tools.android.state_control import AndroidRadioController
from tools.file_digest import sha256_file

from . import android_sdk
from .bleng_android_instrumentation import (
    HOST_MARKER_TAG,
    MarkerCapture,
    run_instrumentation,
    run_instrumentation_until_process_exit,
)
from .capture_io import publish_new_json
from .rf_fixture import preflight_fixture
from .scene_targets import read_write_target

SCOPE = "formal-android-native-permission-transition"
MARKER_TAG = "BLEHUB_RN_PERMISSION"
TEST_CLASS = "com.blehubblenghost.BlengPermissionTransitionInstrumentedTest"
TEST_PACKAGE = "com.blehubblenghost.test"
APP_PACKAGE = "com.blehubblenghost"
READ_WRITE_ROLE = "read-write"
PHASES = ("denied", "active", "restored")
SCENES: dict[str, tuple[str, tuple[str, ...]]] = {
    "denied": (
        "deniedStartupReportsPermissionAndScanDoesNotStart",
        ("bootstrap", "denied-start", "denied-scan"),
    ),
    "active": (
        "activeReadDiesWithTheProcess",
        (
            "bootstrap",
            "active-start",
            "active-scan",
            "active-connected",
            "active-gatt",
            "armed",
        ),
    ),
    "restored": (
        "restoredProcessDeliversTheValue",
        (
            "bootstrap",
            "restored-start",
            "restored-scan",
            "restored-connected",
            "restored-gatt",
            "restored-io",
            "done",
        ),
    ),
}
SYSTEM_TIMEOUT_MAX = 900.0
CLEANUP_TIMEOUT_MAX = 180.0
_IDENTITY_INTEGERS = ("pid", "uid", "user_id", "start_ticks", "native_started_unix_ms")


def _error(error: BaseException) -> str:
    return "".join(format_exception_only(error, show_group=True)).strip()


def _remaining(deadline: float, label: str) -> float:
    budget = deadline - time.monotonic()
    if not math.isfinite(budget) or budget <= 0:
        raise TimeoutError(f"{label} exceeded the run budget")
    return budget


def _phase_identity(
    raw_stages: list[str], *, phase: str, uid: int
) -> AndroidProcessIdentity:
    """The one bootstrap marker this phase emitted, validated as a process identity."""
    payloads = [
        stage.split(":", 1)[1] for stage in raw_stages if stage.startswith("bootstrap:")
    ]
    if len(payloads) != 1:
        raise RuntimeError(
            f"{phase}: expected exactly one bootstrap marker, got {len(payloads)}"
        )
    try:
        value = json.loads(payloads[0])
    except ValueError as error:
        raise RuntimeError(f"{phase}: malformed bootstrap marker: {error}") from error
    if type(value) is not dict or set(value) != {
        *_IDENTITY_INTEGERS,
        "process_name",
    }:
        raise RuntimeError(f"{phase}: bootstrap identity has unexpected fields")
    if any(type(value[key]) is not int for key in _IDENTITY_INTEGERS):
        raise RuntimeError(f"{phase}: bootstrap identity fields must be integers")
    if (
        value["pid"] <= 0
        or value["start_ticks"] <= 0
        or value["native_started_unix_ms"] <= 0
        or value["user_id"] != 0
        or value["uid"] != uid
        or value["process_name"] != APP_PACKAGE
    ):
        raise RuntimeError(
            f"{phase}: bootstrap identity is not this package's user0 process"
        )
    return value  # type: ignore[return-value]


def _require_fresh_identity(
    known: dict[str, AndroidProcessIdentity],
    identity: AndroidProcessIdentity,
    *,
    phase: str,
) -> None:
    """Each phase must be its own process instance (PID reuse by start_ticks is not one)."""
    for other, seen in known.items():
        if (seen["pid"], seen["start_ticks"]) == (
            identity["pid"],
            identity["start_ticks"],
        ):
            raise RuntimeError(f"{phase} reused the {other} process instance")


def run_permission_transition(
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
    """Run the three-phase permission scene; return the published result path."""
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
            "explicit BLUETOOTH_SCAN/CONNECT revoke/grant authorization required: readback, "
            "OS death proof and exact restore"
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
        "rf": "permission-transition",
        "host_gate_status": "failed",
        "scenario_status": "not_run",
        "phone_status": "not_acquired",
        "fixture_status": "not_acquired",
        "phases": {},
        "identities": {},
        "hold": "not_armed",
        "permission_death": None,
        "active_link_ended": False,
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
    permission: AndroidPermissionController | None = None
    primary: BaseException | None = None
    captures: list[MarkerCapture] = []
    try:
        phone = AndroidRadioController(device, run_id, attempt / "phone.json")
        phone.acquire()
        result["phone_status"] = "running"
        _remaining(deadline, "phone lease")
        # The permission lane never mutates the radio: it requires a known ON phone and proves
        # that state again at cleanup, so a radio problem can never be mistaken for a permission
        # problem.
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
        permission = AndroidPermissionController(
            device,
            run_id,
            attempt / "platform/permission.json",
            package=APP_PACKAGE,
            phone_guard=phone.require_owner,
            use_root=use_root,
        )
        try:
            permission.initial()
        except RuntimeError as error:
            raise RuntimeError(
                "the permission lane starts from granted BLUETOOTH_SCAN/CONNECT; grant them "
                "first (for example via formal-host-android-native-adapter-transition, which "
                f"restores what it found): {error}"
            ) from error
        result["uid"] = permission.uid
        _remaining(deadline, "permission baseline")

        lease_context = port_lease(
            fixture_port, purpose="BleHub permission transition fixture"
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

        # `start()` must report the real grants in every phase; a `bootstrap` marker carries the
        # process identity the host's death gate pins.
        arguments = {
            "blehubPermissionTransition": "permission-transition",
            "serviceUuid": service_uuid,
            "localName": plan.contract.local_name,
            "readUuid": read_write_uuid,
            "readHex": provisioned.hex(),
        }
        drivers = {
            "denied": run_instrumentation,
            "active": run_instrumentation_until_process_exit,
            "restored": run_instrumentation,
        }

        def run_phase(
            phase: str, on_stage: Callable[[str, MarkerCapture], None]
        ) -> dict[str, Any]:
            assert phone is not None and permission is not None and fixture is not None
            _remaining(deadline, f"{phase} phase")
            method, expected = SCENES[phase]
            # A fresh capture per process, each behind its own sentinel: logcat replays old
            # buffers, and a marker from an earlier phase must never route this phase.
            capture = MarkerCapture(
                device, deadline, tag=MARKER_TAG, host_tag=HOST_MARKER_TAG
            )
            captures.append(capture)
            result["scenario_status"] = f"{phase}:running"
            driver = drivers[phase]
            if driver is run_instrumentation:
                output = run_instrumentation(
                    device,
                    arguments,
                    test_class=TEST_CLASS,
                    method=method,
                    expected_stages=expected,
                    capture=capture,
                    on_stage=lambda stage: on_stage(stage, capture),
                    advance=fixture.drain_events,
                    deadline=deadline,
                    require_owner=phone.require_owner,
                    log=attempt / f"instrumentation-{phase}.log",
                )
            else:
                output = run_instrumentation_until_process_exit(
                    device,
                    arguments,
                    test_class=TEST_CLASS,
                    method=method,
                    required_stages=expected,
                    capture=capture,
                    on_stage=lambda stage: on_stage(stage, capture),
                    advance=fixture.drain_events,
                    deadline=deadline,
                    require_owner=phone.require_owner,
                    log=attempt / f"instrumentation-{phase}.log",
                )
            result["phases"][phase] = output["summary"]
            identity = _phase_identity(
                output["raw_stages"], phase=phase, uid=permission.uid
            )
            # Every phase must be its own process; a reused instance would make "fresh process"
            # and "the OS ended it" unprovable.
            _require_fresh_identity(result["identities"], identity, phase=phase)
            result["identities"][phase] = identity
            return output

        # Phase 1: permissions revoked. The App must report the denial and never start a scan.
        result["permission_after_deny"] = permission.deny_startup()
        denied = run_phase("denied", lambda stage, capture: None)
        if tuple(denied["stages"]) != SCENES["denied"][1]:
            raise RuntimeError(
                f"denied phase emitted unexpected stages: {denied['stages']!r}"
            )
        permission.grant(label="after-denied")
        result["permission_after_denied"] = "granted"

        # Phase 2: both granted, a read parked inside the fixture, then the OS ends this process.
        hold: Any = None

        def active_stage(stage: str, capture: MarkerCapture) -> None:
            nonlocal hold
            assert fixture is not None and permission is not None
            if stage == "active-connected":
                hold = fixture.arm_read_hold(READ_WRITE_ROLE, deadline=deadline)
                result["hold"] = "armed"
            elif stage == "armed":
                if hold is None:
                    raise RuntimeError("the read hold was never armed")
                fixture.require_hold_held(hold, deadline=deadline)
                result["hold"] = "held"
                identity = _phase_identity(
                    capture.stages(), phase="active", uid=permission.uid
                )
                # The process is alive at this exact moment; pinning it is what makes the
                # later death record apply to this instance and no other.
                result["permission_after_revoke"] = permission.revoke_connected(
                    identity
                )
                result["permission_death"] = permission.await_permission_death(identity)

        active = run_phase("active", active_stage)
        if tuple(active["stages"]) != SCENES["active"][1]:
            raise RuntimeError(
                f"the death phase did not stop at its prefix: {active['stages']!r}"
            )
        if result["permission_death"] is None:
            raise RuntimeError("the OS death gate produced no exit record")
        fixture.settle_no_peer(deadline=deadline)
        result["active_link_ended"] = True
        fixture.prepare_next_cycle(deadline=deadline)
        permission.grant(label="after-death")
        result["permission_after_death"] = "granted"

        # Phase 3: a genuinely fresh process must deliver the provisioned value.
        restored = run_phase("restored", lambda stage, capture: None)
        if tuple(restored["stages"]) != SCENES["restored"][1]:
            raise RuntimeError(
                f"restored phase emitted unexpected stages: {restored['stages']!r}"
            )
        result["scenario_status"] = "passed"
        result["status"] = "passed"
        result["host_gate_status"] = "pending"
    except BaseException as error:  # noqa: BLE001 -- finish owned cleanup before propagating
        primary = error
        result["primary_error"] = _error(error)
        result["primary_traceback"] = traceback.format_exception(error)
        if str(result["scenario_status"]).endswith(":running"):
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

    for capture in captures:
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

    if permission is not None:
        controller = permission
        cleanup("permissions.restore", lambda: controller.finish(primary))

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
            # This lane never mutates the radio, so the App-observation finalizer is the honest
            # one: final ON readback and release, never a fabricated off/on transition.
            result["phone_cleanup_errors"] = phone.finish_observation(primary)

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
