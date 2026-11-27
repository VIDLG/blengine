"""Managed formal iOS scene producer; no signing, grant, radio or reset fallback.

The shape mirrors the Android producer on purpose: one finite budget covers freezing
the candidate, the phone lease, the passive fixture, the installed/launched App and the
scene itself, and every owned scope is retired in a fixed order even after an
interruption.

Three facts are specific to iOS and are made explicit here rather than inherited:

**The candidate is a directory.** An `.app` is frozen into the attempt as a tree and
identified by its tree fingerprint, so the evidence bundle carries the exact bytes
that were installed. Android freezes one APK; iOS freezes one bundle.

**The driver is a test bundle, not a server.** The scene runs inside the formal App's
own XCTest target, so this lane hands `scene_ios_native` one signed `.xctestrun` and
owns no WebDriverAgent, tunnel registry or automation port.

**There is no helper and no permission scope.** iOS needed neither installation
authorization nor a UiAutomator2/App-only split, so the request states only what it
prepared and the evidence records `helper_restoration` as `not_owned` rather than
inventing a pass.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
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
from tools.apple.app_artifact import fingerprint

from .bleng_ios_candidate import IosCandidateOwner
from .bleng_scan_evidence import IOS_NATIVE_DRIVER_KIND as NATIVE_DRIVER_KIND
from .bleng_scan_evidence import SCAN_SCENE, SceneEvidence, publish_scan_bundle
from .bleng_scan_runner import (
    MAX_SCENE_SECONDS,
    PreparedScanHandoff,
    run_prepared_scene,
)
from .capture_io import publish_new_json
from .ios_device import canonical_physical_udid
from .ios_display import DisplayAsleepError, require_awake_display
from .ios_radio_lease import SCAN_OBSERVATION_SCOPE, IosRadioLease
from .rf_fixture import preflight_fixture
from .rf_runner import ProgressReporter, phase_scope
from .scene_ios_native import NativeIosSceneDriver
from .scene_native import GATT_TARGET_KEYS, SUBSCRIPTION_KINDS
from .scene_targets import io_target, read_write_target, subscription_target

# The UI scene is the only phase that produces an observation, so a run never starts it
# with less than this much of its single finite budget left.
MINIMUM_SCENARIO_SECONDS = 120
# One finite budget covers the frozen App tree, install/launch, the test bundle, the
# fixture and the UI scene. A first test-bundle build compiles an Xcode target on this
# Mac, so the ceiling is a device-build allowance rather than the Android run's shorter
# one.
MAX_TOTAL_SECONDS = 2700
MAX_CLEANUP_SECONDS = 120


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("managed iOS scan deadline exceeded")
    return remaining


def _error(error: BaseException) -> str:
    return "".join(format_exception_only(error, show_group=True)).strip()


def validate_ios_scan_inputs(
    *,
    device: str,
    app: Path,
    app_tree_sha256: str,
    app_id: str,
    executable: str,
    team: str,
    identity: str,
    xctestrun: Path | None,
    output_root: Path,
    cwd: Path,
    deadline: float,
) -> None:
    """Reject anything that is not an explicit physical device + frozen App + driver.

    No SDK lookup or device access occurs here. Signing material is passed in already
    resolved by the caller instead of being re-derived, so a scene cannot silently
    build with a different Team than the App it installs.

    The lane drives the App from the formal App's own XCTest target, so it consumes one
    test bundle and owns no WebDriverAgent, tunnel registry or automation server.
    """
    canonical_physical_udid(device)
    if (
        not isinstance(app, Path)
        or not app.is_absolute()
        or app.is_symlink()
        or not app.is_dir()
        or app.suffix != ".app"
        or not (app / "Info.plist").is_file()
    ):
        raise ValueError("an explicit absolute frozen device .app is required")
    if (
        type(app_tree_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", app_tree_sha256) is None
    ):
        raise ValueError("reviewed lowercase App tree SHA256 required")
    if (
        type(app_id) is not str
        or re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", app_id) is None
    ):
        raise ValueError("explicit canonical App bundle identifier required")
    if (
        type(executable) is not str
        or re.fullmatch(r"[A-Za-z0-9_-]+", executable) is None
    ):
        raise ValueError("explicit App executable name required")
    if type(team) is not str or re.fullmatch(r"[A-Z0-9]{10}", team) is None:
        raise ValueError("explicit 10-character signing team required")
    if type(identity) is not str or not identity.startswith("Apple Development:"):
        raise ValueError("an explicit Apple Development identity name is required")
    if (
        not isinstance(xctestrun, Path)
        or not xctestrun.is_absolute()
        or xctestrun.is_symlink()
        or not xctestrun.is_file()
    ):
        raise ValueError("the in-process driver needs an explicit absolute .xctestrun")
    for name, path in (("output_root", output_root), ("cwd", cwd)):
        if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
            raise ValueError(f"{name} must be an explicit absolute non-symlink path")
    _remaining(deadline)


def _freeze_app(
    source: Path, destination: Path, expected: str, deadline: float
) -> None:
    """Copy the signed App tree into this attempt and prove the copy is the reviewed one.

    A copy rather than a reference, because the evidence bundle must stay verifiable
    after a rebuild replaces the derived-data product. Symbolic links are preserved
    rather than dereferenced: the fingerprint seam rejects links, so an App that has
    them fails loudly here instead of silently hashing a different tree.
    """
    _remaining(deadline)
    try:
        shutil.copytree(source, destination, symlinks=True)
    except (OSError, shutil.Error) as error:
        raise RuntimeError(
            f"candidate App tree could not be frozen: {error}"
        ) from error
    if fingerprint(destination) != expected:
        raise RuntimeError("frozen App tree differs from the reviewed fingerprint")
    _remaining(deadline)


def run_ios_scan(
    *,
    device: str,
    app: Path,
    app_tree_sha256: str,
    app_id: str,
    executable: str,
    team: str,
    identity: str,
    xctestrun: Path | None = None,
    fixture_contract: Path,
    fixture_profile: Path,
    fixture_port: str,
    fixture_resource_identity: str,
    output_root: Path,
    timeout: float,
    cleanup_seconds: float,
    authorize_install: bool,
    authorize_fixture_start: bool,
    cwd: Path,
    release_retained_app: bool = False,
    run_id: str | None = None,
    scene: SceneEvidence = SCAN_SCENE,
    progress: ProgressReporter | None = None,
    release_before_install: bool = False,
) -> Path:
    """Prepare and consume one same-process fixture/phone/App/test-bundle tuple.

    A passing result is a managed UI observation with verified host cleanup, not a
    native RF/resource-zero/ABI exit. Broker acquire/release use their own finite
    budgets; stage checks reject late completion without extending UI or fixture wire
    deadlines. Cleanup uses a separate shared budget.

    `scene` decides which workbench scene is driven and which evidence scope and RF
    claim the bundle may carry. Preparation, lease, cleanup and the host gate are
    identical for every scene.

    `progress` narrates preparation and cleanup stages for the human driving one
    blocking run. It never changes what is prepared, published or claimed.

    `release_before_install` lets this run terminate its own bundle's already-running
    copy (attributed by bundle/installation) instead of refusing takeover. It exists so a
    copy left behind by an earlier attempt cannot block every later run; the default
    stays fail-closed.
    """
    if not isinstance(scene, SceneEvidence):
        raise TypeError("an actual managed scene descriptor is required")
    required = (authorize_install, authorize_fixture_start)
    if any(value is not True for value in required):
        raise ValueError(
            "explicit App install and fixture startup authorization required"
        )
    for name, value, maximum in (
        ("timeout", timeout, MAX_TOTAL_SECONDS),
        ("cleanup_seconds", cleanup_seconds, MAX_CLEANUP_SECONDS),
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
    validate_ios_scan_inputs(
        device=device,
        app=app,
        app_tree_sha256=app_tree_sha256,
        app_id=app_id,
        executable=executable,
        team=team,
        identity=identity,
        xctestrun=xctestrun,
        output_root=output_root,
        cwd=cwd,
        deadline=deadline,
    )
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
        "scope": scene.scope("ios"),
        "run_id": run_id,
        "device": device,
        "app_id": app_id,
        "executable": executable,
        "app_tree_sha256": app_tree_sha256,
        "team": team,
        "identity": identity,
        "fixture_resource_identity": fixture_resource_identity,
        "fixture_port": fixture_port,
        "service_uuid": service_uuid,
        "authorize_install": True,
        "authorize_fixture_start": True,
        "timeout": timeout,
        "cleanup_seconds": cleanup_seconds,
        "fixture_pid": os.getpid(),
        "fixture_pgid": os.getpgid(0),
    }
    assert xctestrun is not None
    request["driver_kind"] = NATIVE_DRIVER_KIND
    request["xctestrun_sha256"] = hashlib.sha256(xctestrun.read_bytes()).hexdigest()
    publish_new_json(attempt / "request.json", request)
    result = {
        "schema_version": 1,
        "scope": scene.scope("ios"),
        "run_id": run_id,
        "platform": "ios",
        "device": device,
        "app_id": app_id,
        "app_tree_sha256": app_tree_sha256,
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
    candidate = None
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
    try:
        frozen = attempt / "candidate.app"
        with phase_scope(progress, "candidate.freeze"):
            _freeze_app(app, frozen, app_tree_sha256, deadline)
        # A sleeping display accepts every command and answers none of them: `activate` never
        # becomes hittable, taps land on black and text entry has no keyboard. Read it before
        # anything is leased so the receipt names the phone instead of timing out later.
        with phase_scope(progress, "phone.display"):
            try:
                result["phone_display"] = require_awake_display(
                    device, cwd=cwd, timeout=min(90.0, _remaining(deadline))
                )
            except DisplayAsleepError as asleep:
                # The receipt keeps the measurement, so a failed run says the phone was dark
                # instead of leaving "some UI step timed out" as the only trace.
                result["phone_display"] = asleep.state
                raise
        phone = IosRadioLease(
            device,
            run_id,
            attempt / "phone.json",
            scope=SCAN_OBSERVATION_SCOPE,
        )
        with phase_scope(progress, "phone.acquire"):
            phone.acquire()
        phone_acquired = True
        _remaining(deadline)
        lease_context = port_lease(
            fixture_port, purpose="BleHub formal iOS managed scan"
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
        candidate = IosCandidateOwner(
            app=frozen,
            expected_sha256=app_tree_sha256,
            bundle_id=app_id,
            executable=executable,
            phone=phone,
            device=device,
            run_id=run_id,
            attempt=attempt / "candidate",
            cwd=cwd,
            deadline=deadline,
            release_running=release_before_install,
        )
        with phase_scope(progress, "app.install-launch"):
            candidate.install_and_launch()
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
                # Both write, so both take the one derivation: the profile names the
                # characteristic and the fixture's provisioned length decides the payload.
                target_uuid, write_payload = io_target(plan, role)
                gatt = {"uuid": target_uuid, "write": write_payload.decode("ascii")}
            else:
                # A scene that only reads (or is handed a peer event) needs the identity,
                # not a payload: the profile still names the characteristic.
                target_uuid, _ = read_write_target(plan, role)
                gatt = {"uuid": target_uuid}
        run_prepared_scene(
            scene_driver=NativeIosSceneDriver(
                scene=scene,
                device=device,
                app_id=app_id,
                xctestrun=xctestrun,
                output=attempt / "driver",
                cwd=cwd,
                fresh_install=True,
                expected_local_name=plan.contract.local_name,
                service_filter=plan.contract.service_uuid,
                subscription=subscription,
                gatt=gatt,
            ),
            candidate=candidate,
            phone=phone,
            fixture=fixture,
            plan=plan,
            app_id=app_id,
            output=attempt / "scan",
            deadline=time.monotonic() + min(_remaining(deadline), MAX_SCENE_SECONDS),
            cleanup_seconds=cleanup_seconds,
            scene=scene,
            handoff=handoff,
            release_retained_app=release_retained_app,
            progress=progress,
        )
    except BaseException as error:  # noqa: BLE001 -- retire owned scopes before propagating interruptions.
        primary = error
        result["primary_error"] = _error(error)
        if isinstance(error, FixtureConstructionError):
            result["cleanup_errors"].append(
                "platform.prepare: resource unwind unverified: " + _error(error)
            )
        if lease_attempted and not lease_entered:
            lease_result.update(status="failed", error=_error(error))
            result["fixture_status"] = "failed"
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
                phone_status={
                    "released": "passed",
                    "quarantined": "quarantined",
                }.get(inner["phone_journal"]["status"], "failed"),
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
        if candidate is not None:
            cleanup(
                "candidate.finalize",
                lambda: candidate.finalize(primary, deadline=cleanup_deadline),
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
                lease_context.__exit__(None, None, None)
                lease_result["status"] = "released"
            except BaseException as error:
                lease_result.update(status="failed", error=_error(error))
                raise

        if not cleanup("fixture.lease-release", release_lease):
            result["fixture_status"] = "failed"
    if lease_attempted:

        def record_lease():
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
            raw = fixture_session._read_bytes(attempt / "prepared-result.json")
            if prepared_sha is None or hashlib.sha256(raw).hexdigest() != prepared_sha:
                raise RuntimeError(
                    "prepared managed scan result changed before phone release"
                )
            _remaining(cleanup_deadline)

        result["cleanup_order"].append("phone.finish")
        try:
            result["phone_cleanup_errors"] = phone.finish_observation(
                primary, finalize_owned=authorize_release
            )
            result["phone_status"] = {
                "released": "passed",
                "quarantined": "quarantined",
            }.get(phone.document["status"], "failed")
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
