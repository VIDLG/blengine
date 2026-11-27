"""Managed Android instrumentation notification scene; the host owns every device action.

This is the lane where the *App process itself* opens a real subscription to a real
nRF52840 and the host proves what crossed the air. It deliberately does not use a UI
automation server: the observable here is delivery, and an accessibility-tree driver is
not part of it. The instrumentation test calls the same Nitro surface the JS API calls.

Bounds and ownership that stay with the host:

- the phone lease, the fixture session and its live receipt are host-owned and checked
  around every fixture/device action;
- the fixture's value emission is driven by *stage markers* from the test process, so a
  value can only be sent after the App itself reported `subscribed`;
- the scene is one finite budget; no retry, no implicit install of a different APK and no
  radio, firmware or permission mutation.
"""

from __future__ import annotations

import math
import os
import time
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
from tests.peripherals.nrf52840.profile import PROPERTY_BITS
from tests.peripherals.nrf52840.resource_lease import canonical_identity, port_lease
from tools.android import adb
from tools.android.state_control import AndroidRadioController
from tools.file_digest import sha256_file

from . import android_sdk
from .android_bluetooth_permissions import AndroidBluetoothPermissionScope
from .bleng_android_instrumentation import (
    HOST_MARKER_TAG,
    TEST_COMPONENT,
    MarkerCapture,
    run_instrumentation,
)
from .capture_io import publish_new_json
from .rf_fixture import preflight_fixture

SCOPE = "formal-android-native-notification"
TEST_CLASS = "com.blehubblenghost.BlengNotificationInstrumentedTest"
TEST_PACKAGE = "com.blehubblenghost.test"
APP_PACKAGE = "com.blehubblenghost"
MARKER_TAG = "BLEHUB_RN_NOTIFY"
# More than the supervisor's value budget, so a slow consumer must fail closed.
MAX_SCENE_VALUES = 96
# Ordered stage markers each scene must emit. The host advances the fixture on
# `subscribed` (emit the value) and `disabled` (the peer's disable was observed).
PREFIX_STAGES = (
    "started",
    "scan-submitted",
    "scan-started",
    "advertisement-matched",
    "scan-stopped",
    "connected",
    "gatt-ready",
)
SCENES: dict[str, tuple[str, tuple[str, ...]]] = {
    "single": (
        "realPeripheralValueReachesTheSubscriptionAndTheStreamEndsOnDisable",
        (
            *PREFIX_STAGES,
            "subscribed",
            "value-matched",
            "disabled",
            "terminal",
            "disconnected",
            "done",
        ),
    ),
    "sequence": (
        "repeatedValuesAreNeitherLostNorDuplicated",
        (
            *PREFIX_STAGES,
            "subscribed",
            "values-requested",
            "values-matched",
            "disabled",
            "terminal",
            "disconnected",
            "done",
        ),
    ),
    "overflow": (
        "aSlowConsumerFailsClosedAtTheValueBudget",
        (
            *PREFIX_STAGES,
            "subscribed",
            "values-requested",
            "overflow-observed",
            "disconnected",
            "done",
        ),
    ),
}
# Kept for the single-value scene's own regression vocabulary.
STAGES = PREFIX_STAGES
TAIL_STAGES = SCENES["single"][1][len(PREFIX_STAGES) :]


def _remaining(deadline: float, label: str) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError(
            f"native notification scene deadline exceeded during {label}"
        )
    return remaining


def _error(error: BaseException) -> str:
    return "".join(format_exception_only(error, show_group=True)).strip()


def _subscription_target(
    plan: fixture_session.FixtureSession, role: str
) -> tuple[str, str, str]:
    """Derive the subscription facts from the fixture profile alone."""
    from tests.peripherals.nrf52840 import fixture_session as session_module

    profile = session_module.load_session_profile(plan.contract, plan.profile_path)
    characteristic = next(
        (item for item in profile.characteristics if item.role == role), None
    )
    if characteristic is None:
        raise ValueError(f"fixture profile has no {role!r} role")
    if characteristic.properties & PROPERTY_BITS["notify"]:
        mode = "notification"
    elif characteristic.properties & PROPERTY_BITS["indicate"]:
        mode = "indication"
    else:
        raise ValueError(
            f"fixture {role!r} characteristic is neither notify nor indicate"
        )
    if not profile.notification_value:
        raise ValueError("fixture profile declares no notification value")
    return characteristic.uuid, mode, profile.notification_value.hex()


def run_native_notification(
    *,
    device: str,
    app_apk: Path,
    app_apk_sha256: str,
    test_apk: Path,
    test_apk_sha256: str,
    fixture_contract: Path,
    fixture_profile: Path,
    fixture_port: str,
    fixture_resource_identity: str,
    characteristic_role: str,
    scene: str = "single",
    value_count: int = 8,
    drain_delay_seconds: float = 8.0,
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
    """Run one bounded native notification scene; return the published result path."""
    for name, value, maximum in (
        ("timeout", timeout, 600),
        ("cleanup_seconds", cleanup_seconds, 120),
    ):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 < value <= maximum
        ):
            raise ValueError(f"{name} must be finite, positive and at most {maximum}")
    if not (authorize_install is True and authorize_fixture_start is True):
        raise ValueError(
            "explicit App/test install and fixture startup authorization required"
        )
    if authorize_bluetooth_permissions is not True:
        raise ValueError(
            "explicit BLUETOOTH_SCAN/CONNECT authorization required: root grant, "
            "readback and exact restore; the App process cannot observe a peripheral "
            "without them"
        )
    if type(use_root) is not bool:
        raise ValueError("use_root must be an explicit bool")
    if scene not in SCENES:
        raise ValueError("scene must be one of " + ", ".join(SCENES))
    if type(value_count) is not int or not 1 <= value_count <= 512:
        raise ValueError("value_count must be an integer in [1, 512]")
    if (
        type(drain_delay_seconds) not in (int, float)
        or not math.isfinite(drain_delay_seconds)
        or not 1 <= drain_delay_seconds <= 30
    ):
        raise ValueError("drain delay must be finite and in [1, 30] seconds")
    if (
        type(device) is not str
        or not device.strip()
        or device.lower().startswith("emulator-")
    ):
        raise ValueError("explicit physical Android serial required")
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
    kind, serial = fixture_resource_identity.split(":", 1)
    if canonical_identity(kind, serial) != fixture_resource_identity:
        raise ValueError("canonical stable fixture identity required")
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
        if not path.is_file():
            raise ValueError(f"missing {label} APK: {path}")
        frozen = attempt / f"{label}.apk"
        with frozen.open("xb") as stream:
            stream.write(path.read_bytes())
        if sha256_file(frozen) != expected:
            raise ValueError(f"{label} APK does not match the authorized SHA-256")

    result: dict[str, Any] = {
        "schema_version": 1,
        "scope": SCOPE,
        "run_id": run_id,
        "platform": "android",
        "device": device,
        "app_id": APP_PACKAGE,
        "test_component": TEST_COMPONENT,
        "test_class": TEST_CLASS,
        "app_apk_sha256": app_apk_sha256,
        "test_apk_sha256": test_apk_sha256,
        "status": "failed",
        "rf": "native-notification-observation",
        "host_gate_status": "failed",
        "scenario_status": "not_run",
        "phone_status": "not_acquired",
        "fixture_status": "not_acquired",
        "scene": scene,
        "subscription": None,
        "stages": [],
        "fixture_value_applied": None,
        "fixture_value_count": None,
        "fixture_post_disable": False,
        "instrumentation_output": None,
        "fixture_close": None,
        "primary_error": None,
        "cleanup_errors": [],
        "cleanup_order": [],
    }
    request = {
        "schema_version": 1,
        "scope": SCOPE,
        "run_id": run_id,
        "device": device,
        "app_id": APP_PACKAGE,
        "app_apk_sha256": app_apk_sha256,
        "test_apk_sha256": test_apk_sha256,
        "fixture_resource_identity": fixture_resource_identity,
        "fixture_port": fixture_port,
        "characteristic_role": characteristic_role,
        "scene": scene,
        "value_count": value_count,
        "drain_delay_seconds": drain_delay_seconds,
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
    fixture: DirectFixture | None = None
    plan = None
    lease_context = None
    permission_scope: AndroidBluetoothPermissionScope | None = None
    capture: MarkerCapture | None = None
    primary: BaseException | None = None
    fixture_started = False
    try:
        phone = AndroidRadioController(device, run_id, attempt / "phone.json")
        phone.acquire()
        result["phone_status"] = "running"
        _remaining(deadline, "phone lease")

        lease_context = port_lease(
            fixture_port, purpose="BleHub native notification observation"
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
        fixture = DirectFixture(
            plan.port, plan.profile_path, lease=lease, fixture_session=plan
        )
        fixture.start(deadline=deadline)
        fixture.require_active_session(plan)
        fixture_started = True
        result["fixture_status"] = "passed"
        _remaining(deadline, "fixture start")

        characteristic_uuid, mode, value_hex = _subscription_target(
            plan, characteristic_role
        )
        result["subscription"] = {
            "service_uuid": plan.contract.service_uuid,
            "characteristic_uuid": characteristic_uuid,
            "mode": mode,
            "value_hex": value_hex,
        }

        coordination = attempt / "coordination"
        coordination.mkdir(mode=0o700)

        gap_frames: list[tuple[int, bytes]] = []

        # One thread owns this fixture session: the host pumps the tester queue itself and
        # acts at the scene's own stage boundaries. An in-process host therefore needs no
        # file protocol and cannot race a second reader of the same BTP session.
        #   single/sequence: emit after the App reports `subscribed`;
        #   overflow:        emit a whole budget-exceeding run at the test's request.
        def emit_values(count: int) -> None:
            sent = fixture.publish_notification_sequence(
                characteristic_role, count, gap_seconds=0.04
            )
            result["fixture_value_applied"] = value_hex
            result["fixture_value_count"] = sent
            (coordination / "value-applied").touch()

        def on_stage(name: str) -> None:
            fixture.drain_events()
            if name == "subscribed" and scene != "overflow":
                emit_values(1 if scene == "single" else value_count)
            elif name == "values-requested" and scene == "overflow":
                # More than the observation budget, so the peer's lane must fail closed.
                emit_values(MAX_SCENE_VALUES)
            elif name == "disabled":
                result["fixture_post_disable"] = True
                (coordination / "post-disable-triggered").touch()

        def install(label: str, path: Path, *, test_only: bool) -> bool:
            with (attempt / f"install-{label}.log").open(
                "x", encoding="utf-8"
            ) as stream:
                return android_sdk.install_apk(
                    device,
                    path,
                    log=stream,
                    test_only=test_only,
                    use_root=use_root,
                    deadline=deadline,
                    require_owner=phone.require_owner,
                    cwd=cwd,
                )

        installed_with_root = install("app", attempt / "app.apk", test_only=False)
        install("test", attempt / "test.apk", test_only=True)
        result["install_mode"] = {"installedWithRoot": installed_with_root}
        _remaining(deadline, "install")

        # The App process cannot observe a peripheral without its runtime permissions; the
        # baseline (usually denied) is captured here and restored exactly under this lease.
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

        # Scoped by its own sentinel: the device's log is replayed first, and it is not ours
        # to clear.
        capture = MarkerCapture(
            device, deadline, tag=MARKER_TAG, host_tag=HOST_MARKER_TAG
        )
        result["scenario_status"] = "running"
        output = run_instrumentation(
            device,
            {
                "blehubServiceUuid": plan.contract.service_uuid,
                "blehubCharacteristicUuid": characteristic_uuid,
                "blehubValueHex": value_hex,
                "blehubMode": mode,
                "blehubValueCount": str(value_count),
                "blehubDrainDelaySeconds": str(int(drain_delay_seconds)),
            },
            test_class=TEST_CLASS,
            method=SCENES[scene][0],
            expected_stages=SCENES[scene][1],
            capture=capture,
            on_stage=on_stage,
            advance=lambda: fixture.drain_events(record=gap_frames),
            deadline=deadline,
            require_owner=phone.require_owner,
            log=attempt / "instrumentation.log",
        )
        result["gap_frames"] = [
            f"{opcode}:{payload.hex()}" for opcode, payload in gap_frames
        ]
        result["instrumentation_output"] = output["summary"]
        result["stages"] = output["stages"]
        if not result["fixture_value_applied"]:
            raise RuntimeError(
                "the fixture never applied a value for this subscription"
            )
        if scene == "overflow":
            # This scene never disables: the lane already failed closed, and the peer's
            # teardown is the disconnect. Requiring a disable receipt here would demand a
            # command the contract does not allow on a failed stream.
            if "overflow-observed" not in result["stages"]:
                raise RuntimeError(
                    "the slow consumer never observed the budget failure"
                )
        elif not result["fixture_post_disable"]:
            raise RuntimeError("the fixture never observed the subscription disable")
        result["scenario_status"] = "passed"
        result["status"] = "passed"
        result["host_gate_status"] = "pending"
    except BaseException as error:  # noqa: BLE001 -- finish owned cleanup before propagating
        primary = error
        result["primary_error"] = _error(error)
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

    def stop_owned_app() -> None:
        """Bound a stuck instrumentation run to our own installed candidate.

        `am instrument -w` cannot be interrupted by a Python deadline, and a hung
        native call would otherwise hold the lease past its budget. The package and
        its installed bytes are re-checked before this one force-stop.
        """
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

    if primary is not None:
        cleanup("app.stop-after-failure", stop_owned_app)
    if permission_scope is not None:
        scope = permission_scope
        cleanup("permissions.restore", lambda: scope.finish(primary))

    def close_fixture() -> None:
        assert fixture is not None and plan is not None
        if not fixture_started:
            fixture.close_resources()
            return

        # A passive close refuses to stop advertising while a central is still connected,
        # and the central's own connect/disconnect can still be queued here. Settling that
        # observed fact is the fixture's own job; the close itself is one-shot and must
        # not be retried.
        fixture.settle_no_peer(deadline=cleanup_deadline)
        receipt = fixture.stop_advertising_and_close(deadline=cleanup_deadline)
        if (
            type(receipt) is not AdvertisingCloseReceipt
            or receipt.session_id != run_id
            or receipt.resource_identity != fixture_resource_identity
        ):
            raise RuntimeError("foreign passive fixture close receipt")
        result["fixture_close"] = asdict(receipt)

    if fixture is not None:
        cleanup("fixture.close", close_fixture)
        if result["fixture_close"] is None:
            result["fixture_status"] = "failed"
    if lease_context is not None:

        def release_lease() -> None:
            assert lease_context is not None
            lease_context.__exit__(None, None, None)

        cleanup("fixture.lease-release", release_lease)
    if phone is not None:
        result["phone_cleanup_errors"] = []

        def finish_phone() -> None:
            assert phone is not None

            def authorize_release() -> None:
                if result["cleanup_errors"]:
                    raise RuntimeError("; ".join(result["cleanup_errors"]))

            result["phone_cleanup_errors"] = phone.finish_app_observation(
                primary, finalize_owned=authorize_release
            )

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
