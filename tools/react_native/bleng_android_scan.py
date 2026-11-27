"""Managed formal Android scan producer; no grant, radio, flash or reset fallback."""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
import uuid
from builtins import BaseExceptionGroup
from dataclasses import asdict
from pathlib import Path
from traceback import format_exception_only

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.direct_fixture import (
    AdvertisingCloseReceipt,
    DirectFixture,
    FixtureConstructionError,
)
from tests.peripherals.nrf52840.resource_lease import canonical_identity, port_lease
from tools.android.state_control import AndroidRadioController

from .android_bluetooth_permissions import AndroidBluetoothPermissionScope
from .android_native_host import AndroidNativeHost, validate_android_inputs
from .bleng_scan_evidence import (
    ANDROID_NATIVE_DRIVER_KIND,
    SCAN_SCENE,
    SceneEvidence,
    publish_scan_bundle,
    require_release_apk,
)
from .bleng_scan_runner import (
    MAX_SCENE_SECONDS,
    PreparedScanHandoff,
    run_prepared_scene,
)
from .capture_io import publish_new_json
from .evidence_io import freeze_file
from .rf_fixture import preflight_fixture
from .rf_runner import ProgressReporter, phase_scope
from .scene_android_native import NativeAndroidSceneDriver
from .scene_native import GATT_TARGET_KEYS, SUBSCRIPTION_KINDS
from .scene_targets import io_target, read_write_target, subscription_target


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("managed Android scan deadline exceeded")
    return remaining


# The UI scene is the only phase that produces an observation, so a run never starts it
# with less than this much of its single finite budget left.
MINIMUM_SCENARIO_SECONDS = 120
# One finite budget covers uninstall/install, the automation session, the fixture and the
# UI scene. Evidence validation reads this same ceiling instead of repeating a literal.
# The scene itself is round-trip-bound (see `bleng_scan_runner.MAX_SCENE_SECONDS`), so the
# total must leave room for its whole allowance plus installation, automation startup and
# cleanup. It is one ceiling for both lanes: a scene that declares the budget it needs must
# be runnable here too, otherwise its cycle count is a claim this lane can never satisfy.
MAX_TOTAL_SECONDS = 2400


def _error(error: BaseException) -> str:
    return "".join(format_exception_only(error, show_group=True)).strip()


def run_android_scan(
    *,
    device: str,
    app_id: str,
    activity: str,
    apk: Path,
    apk_sha256: str,
    test_apk: Path,
    test_apk_sha256: str,
    fixture_contract: Path,
    fixture_profile: Path,
    fixture_port: str,
    fixture_resource_identity: str,
    output_root: Path,
    timeout: float,
    cleanup_seconds: float,
    authorize_install: bool,
    authorize_fixture_start: bool,
    use_root: bool,
    cwd: Path,
    authorize_bluetooth_permissions: bool = False,
    run_id: str | None = None,
    scene: SceneEvidence = SCAN_SCENE,
    progress: ProgressReporter | None = None,
) -> Path:
    """Prepare and consume one same-process fixture/phone/automation tuple.

    A passing result is a managed UI observation with verified host cleanup,
    not a native RF/resource-zero/ABI exit. Broker acquire/release use their own
    finite budgets; stage checks reject late completion without extending UI or
    fixture wire deadlines. Cleanup uses a separate shared budget.

    `scene` decides which workbench scene is driven and which evidence scope and RF
    claim the bundle may carry. Preparation, lease, cleanup and the host gate are
    identical for every scene.

    Every managed scene has an in-process plan (`scene_android_native.PLANNED_SCENES`)
    and is driven by the App's own instrumentation, so the test APK is the only
    automation artifact: no Appium server, helper APKs or UiAutomator2 server exist for
    this lane to prepare.

    `progress` narrates preparation and cleanup stages for the human driving one
    blocking run. It never changes what is prepared, published or claimed.
    """
    if not isinstance(scene, SceneEvidence):
        raise TypeError("an actual managed scene descriptor is required")
    required = (authorize_install, authorize_fixture_start)
    if any(value is not True for value in required):
        raise ValueError(
            "explicit App install and fixture startup authorization required"
        )
    if authorize_bluetooth_permissions is not True:
        raise ValueError(
            "explicit BLUETOOTH_SCAN/CONNECT permission authorization required: "
            "root grant, readback and exact restore; no radio change"
        )
    if type(use_root) is not bool:
        raise ValueError("root must be an explicit bool")
    for name, value, maximum in (
        # One finite budget for the whole run: uninstall/install of the candidate, the
        # automation session and the fixture all happen inside it before the UI scene.
        ("timeout", timeout, MAX_TOTAL_SECONDS),
        ("cleanup_seconds", cleanup_seconds, 120),
    ):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 < value <= maximum
        ):
            raise ValueError(
                f"{name} must be finite, positive and at most {maximum} seconds"
            )
    deadline = time.monotonic() + timeout
    validate_android_inputs(
        device=device,
        app_id=app_id,
        activity=activity,
        apk=apk,
        apk_sha256=apk_sha256,
        output_root=output_root,
        cwd=cwd,
    )
    if not isinstance(test_apk, Path) or not test_apk.is_absolute():
        raise ValueError("the in-process lane needs an absolute test APK path")
    if test_apk.is_symlink() or not test_apk.is_file():
        raise ValueError(f"missing test APK: {test_apk}")
    if test_apk.suffix != ".apk":
        raise ValueError("the test artifact must be an .apk")
    if (
        type(test_apk_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", test_apk_sha256) is None
    ):
        raise ValueError("reviewed lowercase test APK SHA256 required")
    for path in (fixture_contract, fixture_profile):
        if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
            raise ValueError(
                "explicit non-symlink absolute fixture input paths required"
            )
    if (
        type(fixture_port) is not str
        or not fixture_port.strip()
        or any(ord(c) < 32 for c in fixture_port)
    ):
        raise ValueError("explicit fixture serial port required")
    if (
        type(fixture_resource_identity) is not str
        or ":" not in fixture_resource_identity
    ):
        raise ValueError("explicit stable fixture identity required")
    kind, serial = fixture_resource_identity.split(":", 1)
    if canonical_identity(kind, serial) != fixture_resource_identity:
        raise ValueError("canonical stable fixture identity required")
    service_uuid = preflight_fixture(
        fixture_contract=fixture_contract, fixture_profile=fixture_profile
    )
    run_id = str(uuid.uuid4()) if run_id is None else run_id
    if type(run_id) is not str or str(uuid.UUID(run_id)) != run_id:
        raise ValueError("run_id must be a canonical UUID")
    _remaining(deadline)
    attempt = output_root / run_id
    attempt.mkdir(parents=True, mode=0o700, exist_ok=False)
    request = {
        "schema_version": 1,
        "scope": scene.scope("android"),
        "run_id": run_id,
        "device": device,
        "app_id": app_id,
        "activity": activity,
        "apk_sha256": apk_sha256,
        "fixture_resource_identity": fixture_resource_identity,
        "fixture_port": fixture_port,
        "service_uuid": service_uuid,
        "authorize_install": True,
        "authorize_fixture_start": True,
        "authorize_bluetooth_permissions": authorize_bluetooth_permissions,
        "use_root": use_root,
        "timeout": timeout,
        "cleanup_seconds": cleanup_seconds,
        "fixture_pid": os.getpid(),
        "fixture_pgid": os.getpgid(0),
    }
    request["driver_kind"] = ANDROID_NATIVE_DRIVER_KIND
    request["test_apk_sha256"] = test_apk_sha256
    publish_new_json(attempt / "request.json", request)
    result = {
        "schema_version": 1,
        "scope": scene.scope("android"),
        "run_id": run_id,
        "platform": "android",
        "device": device,
        "app_id": app_id,
        "apk_sha256": apk_sha256,
        "status": "failed",
        "host_gate_status": "pending",
        "rf": scene.rf,
        "scenario_status": "not_run",
        "session_status": "not_run",
        "phone_status": "not_acquired",
        "fixture_status": "not_acquired",
        "fixture_close": None,
        "primary_error": None,
        "cleanup_errors": [],
        "phone_cleanup_errors": [],
        "cleanup_order": [],
    }
    phone = None
    phone_acquired = False
    host = None
    fixture = None
    fixture_started = fixture_start_verified = start_attempted = False
    handoff = PreparedScanHandoff()
    lease_context = None
    lease_attempted = lease_entered = False
    primary = None
    interruptions = []
    lease_result = {
        "schema_version": 1,
        "run_id": run_id,
        "resource_identity": fixture_resource_identity,
        "status": "not_acquired",
        "acquired": False,
        "observed_resource_identity": None,
        "fixture_pid": os.getpid(),
        "fixture_pgid": os.getpgid(0),
        "error": None,
    }
    plan = None
    permission_scope = None
    try:
        with phase_scope(progress, "candidate.freeze"):
            freeze_file(
                apk,
                attempt / "candidate.apk",
                expected_sha256=apk_sha256,
                deadline=deadline,
                expected_name="candidate.apk",
            )
            require_release_apk(attempt / "candidate.apk", deadline)
        _remaining(deadline)
        phone = AndroidRadioController(device, run_id, attempt / "phone.json")
        with phase_scope(progress, "phone.acquire"):
            phone.acquire()
        phone_acquired = True
        _remaining(deadline)
        lease_context = port_lease(
            fixture_port, purpose="BleHub formal Android managed scan"
        )
        lease_attempted = True
        with phase_scope(progress, "fixture.lease"):
            lease = lease_context.__enter__()
            lease_entered = True
            lease_result.update(
                acquired=True,
                status="failed",
                observed_resource_identity=lease.identity,
            )
            result["fixture_status"] = "failed"
            _remaining(deadline)
            lease.require(kind, serial)
            if lease.identity != fixture_resource_identity:
                raise RuntimeError(
                    "selected fixture differs from authorized physical identity"
                )
        with phase_scope(progress, "fixture.prepare"):
            plan = fixture_session.prepare_session(
                fixture_contract,
                fixture_profile,
                attempt / "fixture",
                port=fixture_port,
                service_uuid=service_uuid,
                session_id=run_id,
            )
            if plan.resource_identity != fixture_resource_identity:
                raise RuntimeError("fixture session USB identity drift")
            _remaining(deadline)
            fixture = DirectFixture(
                plan.port, plan.profile_path, lease=lease, fixture_session=plan
            )
        start_attempted = True
        result["fixture_status"] = "failed"
        start_record = {
            "run_id": run_id,
            "resource_identity": fixture_resource_identity,
            "status": "failed",
            "error": None,
        }
        start_error = None
        with phase_scope(progress, "fixture.start"):
            try:
                fixture.start(deadline=deadline)
                fixture.require_active_session(plan)
                fixture_session.verify_receipt(
                    plan, fixture_identity=(os.getpid(), os.getpgid(0))
                )
                _remaining(deadline)
                fixture_started = True
                start_record["status"] = "passed"
            except BaseException as error:  # noqa: BLE001 -- preserve startup interruption through independent evidence publication.
                start_error = error
                start_record["error"] = _error(error)
            try:
                publish_new_json(attempt / "fixture/start.json", start_record)
                fixture_start_verified = start_record["status"] == "passed"
            except BaseException as publication:  # noqa: BLE001 -- never replace the startup primary with a recording failure.
                result["cleanup_errors"].append(
                    "fixture.start-record: " + _error(publication)
                )
                if start_error is not None:
                    start_error.add_note(
                        "fixture start evidence: " + _error(publication)
                    )
                else:
                    start_error = publication
            if start_error is not None:
                raise start_error
        # No App installation/launch occurs before live installed-image proof.
        host = AndroidNativeHost(
            phone=phone,
            run_id=run_id,
            app_id=app_id,
            activity=activity,
            apk=attempt / "candidate.apk",
            apk_sha256=apk_sha256,
            test_apk=test_apk,
            test_apk_sha256=test_apk_sha256,
            attempt=attempt,
            cwd=cwd,
            deadline=deadline,
            use_root=use_root,
        )
        permission_scope = AndroidBluetoothPermissionScope(
            device=device,
            run_id=run_id,
            evidence_path=attempt / "platform/permission-scope.json",
            package=app_id,
            phone_guard=phone.require_owner,
            use_root=use_root,
        )
        with phase_scope(progress, "platform.prepare"):
            permission_scope.prep()
            host.prepare()
        with phase_scope(progress, "app.install-launch"):
            host.launch()
        # A scene that starts with a sliver of the run budget cannot finish and would
        # be misread as an RF or UI failure; require a usable scenario window instead.
        # The window scales down with the declared budget so short control runs stay short.
        minimum = max(5.0, min(MINIMUM_SCENARIO_SECONDS, timeout * 0.25))
        if _remaining(deadline) < minimum:
            raise RuntimeError(
                "preparation consumed the run budget: "
                f"{minimum:.0f}s must remain for the UI scene; "
                "pass a larger --timeout"
            )
        # The prepared envelope owns one scene-sized window (its own bound is
        # `MAX_SCENE_SECONDS`), taken from the run's single finite budget rather than
        # extending it.
        scene_deadline = time.monotonic() + min(_remaining(deadline), MAX_SCENE_SECONDS)
        common = {
            "candidate": host.candidate,
            "phone": phone,
            "fixture": fixture,
            "plan": plan,
            "app_id": app_id,
            "output": attempt / "scan",
            "deadline": scene_deadline,
            "cleanup_seconds": cleanup_seconds,
            "scene": scene,
            "handoff": handoff,
            "before_phone_release": (
                permission_scope.finish if permission_scope is not None else None
            ),
            # The in-process lane's App process is the instrumentation host's: it cannot
            # outlive the run, so its package is always stopped on the way out.
            "release_retained_app": True,
            "progress": progress,
        }
        # A subscription or read/write scene names its target from the profile alone; the
        # driver neither knows nor chooses a characteristic or a payload.
        subscription = None
        if scene.kind in SUBSCRIPTION_KINDS:
            role = scene.characteristic_role
            if role is None:
                raise ValueError("a subscription scene needs a profile role")
            target_uuid, target_mode, target_value = subscription_target(plan, role)
            subscription = {
                "uuid": target_uuid,
                "mode": target_mode,
                "value": target_value,
            }
        gatt = None
        required_gatt = GATT_TARGET_KEYS.get(scene.kind)
        if required_gatt is not None:
            role = scene.characteristic_role
            if role is None:
                raise ValueError(f"a {scene.kind} scene needs a profile role")
            if scene.kind in ("disconnect", "descriptor-io"):
                # The read/descriptor and the subscription share one profile-declared
                # characteristic; the driver refuses a target that is not exactly the one it
                # declares, and `read_write_target` cannot derive it because the role is not
                # writable.
                if subscription is None:
                    raise ValueError(
                        f"a {scene.kind} scene needs its subscription target"
                    )
                gatt = {"uuid": subscription["uuid"]}
            elif "write" in required_gatt:
                target_uuid, write_payload = io_target(plan, role)
                gatt = {"uuid": target_uuid, "write": write_payload.decode("ascii")}
            else:
                target_uuid, _ = read_write_target(plan, role)
                gatt = {"uuid": target_uuid}
        run_prepared_scene(
            scene_driver=NativeAndroidSceneDriver(
                scene=scene,
                device=device,
                app_id=app_id,
                activity=activity,
                test_apk=host.test_apk,
                output=attempt / "driver",
                cwd=cwd,
                fresh_install=True,
                expected_local_name=plan.contract.local_name,
                service_filter=plan.contract.service_uuid,
                subscription=subscription,
                gatt=gatt,
            ),
            **common,
        )
    except BaseException as error:  # noqa: BLE001 -- retire owned scopes before propagating interruptions.
        primary = error
        result["primary_error"] = _error(error)
        if lease_attempted and not lease_entered:
            lease_result.update(status="failed", error=_error(error))
            result["fixture_status"] = "failed"
        if isinstance(error, FixtureConstructionError):
            result["cleanup_errors"].append(
                "fixture.construct: resource unwind unverified: " + _error(error)
            )
        if (
            lease_attempted
            and not lease_entered
            and isinstance(error, BaseExceptionGroup)
        ):
            result["cleanup_errors"].append(
                "fixture.lease-acquire: broker unwind unverified: " + _error(error)
            )

    cleanup_deadline = (
        handoff.cleanup_deadline
        if handoff.cleanup_deadline is not None
        else time.monotonic() + cleanup_seconds
    )

    def cleanup(label, action):
        result["cleanup_order"].append(label)
        try:
            action()
            return True
        except BaseException as error:  # noqa: BLE001 -- continue independent resource cleanup.
            result["cleanup_errors"].append(label + ": " + _error(error))
            if not isinstance(error, Exception):
                interruptions.append(error)
            return False

    def close_fixture():
        if fixture_started:
            # The scene's own central may have connected and disconnected while this
            # process was not reading its queue, so the peer fact is settled here rather
            # than assumed from an observation list.
            fixture.settle_no_peer(deadline=cleanup_deadline)
            receipt = fixture.stop_advertising_and_close(deadline=cleanup_deadline)
            if (
                type(receipt) is not AdvertisingCloseReceipt
                or receipt.session_id != run_id
                or receipt.resource_identity != fixture_resource_identity
            ):
                raise RuntimeError("foreign passive fixture close receipt")
            result["fixture_close"] = asdict(receipt)
            result["fixture_status"] = "passed" if fixture_start_verified else "failed"
        else:
            fixture.close_resources()
            if start_attempted:
                raise RuntimeError(
                    "failed fixture startup has no verified advertising-inactive receipt"
                )

    if handoff.admitted:
        result["cleanup_order"].append("scan.run-prepared")

        def recover_inner_result():
            inner = fixture_session._json(
                fixture_session._read_bytes(attempt / "scan/result.json")
            )
            if (
                inner.get("scope"),
                inner.get("run_id"),
                inner.get("device"),
                inner.get("app_id"),
            ) != (scene.inner_scope, run_id, device, app_id):
                raise RuntimeError("foreign prepared scan result")
            result.update(
                scenario_status=inner["scenario_status"],
                session_status=inner["session_status"],
                phone_cleanup_errors=list(inner["phone_cleanup_errors"]),
                phone_status=inner["phone_journal"]["status"],
                fixture_close=inner["fixture_close"],
            )
            result["cleanup_errors"].extend(inner["cleanup_errors"])
            if primary is None:
                result["primary_error"] = inner["primary_error"]
            elif inner["primary_error"] is not None:
                primary.add_note("prepared scan primary: " + inner["primary_error"])
                result["primary_error"] = _error(primary)
            result["fixture_status"] = (
                "passed"
                if fixture_start_verified and inner["fixture_close"] is not None
                else "failed"
            )

        cleanup("scan.result", recover_inner_result)

    if not handoff.admitted:
        if permission_scope is not None and not permission_scope.finished:
            cleanup("permissions.restore", lambda: permission_scope.finish(primary))
        if host is not None:
            cleanup(
                "automation.close",
                lambda: host.close_automation(deadline=cleanup_deadline),
            )
            if host.candidate is not None:
                cleanup(
                    "candidate.finalize",
                    lambda: host.candidate.finalize(
                        primary,
                        deadline=cleanup_deadline,
                        # The in-process lane's App process belongs to its test host.
                        release_retained=True,
                    ),
                )
        if fixture is not None:
            cleanup("fixture.close", close_fixture)
    elif primary is not None:
        # Publication/interruption after transfer cannot authorize a second DELETE,
        # App stop, passive stop or phone release by the outer producer.
        if result["phone_status"] == "not_acquired":
            observed = phone.document["status"]
            result["phone_status"] = (
                observed
                if observed in ("passed", "failed", "quarantined")
                else "failed"
            )

    if plan is not None:
        publish_close = {
            "run_id": run_id,
            "resource_identity": fixture_resource_identity,
            "status": "passed" if result["fixture_close"] is not None else "failed",
            "receipt": result["fixture_close"],
            "error": None
            if result["fixture_close"] is not None
            else "passive closure not verified",
        }
        cleanup(
            "fixture.close-record",
            lambda: publish_new_json(attempt / "fixture/close.json", publish_close),
        )
    if lease_entered:

        def release_lease():
            try:
                # Pass no primary to the context manager: broker release errors
                # must be caught independently, not hidden as notes on that cause.
                lease_context.__exit__(None, None, None)
                lease_result["status"] = "released"
            except BaseException as error:
                lease_result.update(status="failed", error=_error(error))
                raise

        if not cleanup("fixture.lease-release", release_lease):
            result["fixture_status"] = "failed"
    if lease_attempted:

        def record_lease():
            # Snapshot construction itself may have failed before producing a plan.
            # Preserve the independent acquisition/release outcome in that case.
            (attempt / "fixture").mkdir(mode=0o700, exist_ok=True)
            publish_new_json(attempt / "fixture/lease.json", lease_result)

        cleanup("fixture.lease-record", record_lease)
    prepared_sha = None

    def prepare_result():
        nonlocal prepared_sha
        _remaining(cleanup_deadline)
        prepared_sha = publish_new_json(attempt / "prepared-result.json", result)
        _remaining(cleanup_deadline)

    cleanup("bundle.prepare", prepare_result)

    if not handoff.admitted and phone_acquired:

        def authorize_release():
            _remaining(cleanup_deadline)
            if result["cleanup_errors"]:
                raise RuntimeError("; ".join(result["cleanup_errors"]))
            if host is not None:
                host.require_automation_closed()
            raw = fixture_session._read_bytes(attempt / "prepared-result.json")
            if prepared_sha is None or hashlib.sha256(raw).hexdigest() != prepared_sha:
                raise RuntimeError(
                    "prepared managed scan result changed before phone release"
                )
            _remaining(cleanup_deadline)

        result["cleanup_order"].append("phone.finish")
        try:
            result["phone_cleanup_errors"] = phone.finish_app_observation(
                primary, finalize_owned=authorize_release
            )
            result["phone_status"] = phone.document["status"]
        except BaseException as error:  # noqa: BLE001 -- preserve phone failure independently.
            result["phone_status"] = "failed"
            result["phone_cleanup_errors"].append(_error(error))
            if not isinstance(error, Exception):
                interruptions.append(error)
    cleanup("cleanup.complete", lambda: _remaining(cleanup_deadline))
    if (
        result["primary_error"] is None
        and not result["cleanup_errors"]
        and not result["phone_cleanup_errors"]
        and all(
            result[key] == "passed"
            for key in (
                "scenario_status",
                "session_status",
                "phone_status",
                "fixture_status",
            )
        )
    ):
        result["status"] = "passed"
    else:
        result["host_gate_status"] = "failed"
    interruption = (
        primary
        if primary is not None and not isinstance(primary, Exception)
        else next(iter(interruptions), None)
    )
    try:
        path = publish_scan_bundle(
            attempt,
            result,
            deadline=cleanup_deadline if result["status"] == "passed" else None,
            scene=scene,
        )
    except BaseException as publication:
        if interruption is not None:
            interruption.add_note("managed scan publication: " + _error(publication))
            raise interruption from publication
        publication.add_note(
            f"managed scan primary={result['primary_error']}; cleanup={result['cleanup_errors']}; phone={result['phone_status']}"
        )
        raise
    if interruption is not None:
        interruption.add_note(f"managed scan evidence: {path}")
        raise interruption
    return path
