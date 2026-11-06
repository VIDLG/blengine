"""Managed non-RF lifecycle or explicit diagnostic controls for one formal Android App.

Owners, not this composition, establish process/session identity and restoration.
A scenario pass is never a host-gate pass. No helper installation or BLE mutation
is authorized here; temporary helper settings belong to the handed-off owner.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from traceback import format_exception_only
from typing import Any

from tools.android.android_appium import uiautomator2_support_apks
from tools.android.state_control import AndroidRadioController

from .bleng_android_automation import HELPER_PACKAGES, hash_helper_apk
from .bleng_android_candidate import AndroidCandidateOwner
from .bleng_android_close_diagnostics import AndroidCloseDiagnostics
from .bleng_android_preparation import AndroidHelperPreparation
from .bleng_android_session import AndroidAutomationOwner
from .bleng_automation import ScanAutomationOwner
from .bleng_lifecycle_evidence import (
    IDLE_CONTROL_SCOPE,
    SCOPE,
    START_HOLD_CONTROL_SCOPE,
    freeze_lifecycle_apk,
    publish_lifecycle_bundle,
)
from .capture_io import publish_new_json
from .run_bleng_ui import (
    START_HOLD_QUIET_SECONDS,
    exercise_idle_control,
    exercise_lifecycle,
)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("Android lifecycle deadline exceeded")
    return remaining


def _error(error: BaseException) -> str:
    return "".join(format_exception_only(error, show_group=True)).strip()


def _regular(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size == 0:
        raise ValueError(f"nonempty unlinked regular local file required: {path}")


def run_android_lifecycle(
    *,
    device: str,
    app_id: str,
    activity: str,
    apk: Path,
    apk_sha256: str,
    appium_home: Path,
    appium_entry: Path,
    node: str,
    expected_helper_sha256: dict[str, str],
    appium_port: int,
    system_port: int,
    output_root: Path,
    timeout: float,
    phase_timeout: float,
    cleanup_seconds: float,
    authorize_install: bool,
    authorize_helper_settings: bool,
    use_root: bool,
    cwd: Path,
    close_diagnostics: bool = False,
    automation_idle_control: bool = False,
    automation_start_hold_control: bool = False,
    run_id: str | None = None,
) -> Path:
    """Return the final bundle result; propagate publication errors/interruptions.

    Validation is local-only and precedes phone acquisition. All preparation and
    UI work share one absolute deadline, including local APK freezing. Cleanup
    has a separate bounded deadline. Failed cleanup quarantines the actual lease;
    failed acquisition never grants authority over an old lock. Ordinary failures
    are represented in the result, which callers must inspect for CLI exit status.
    """
    started = time.monotonic()
    if authorize_install is not True or authorize_helper_settings is not True:
        raise ValueError(
            "explicit App install and temporary helper settings authorization required"
        )
    if any(
        type(value) is not bool
        for value in (
            use_root,
            close_diagnostics,
            automation_idle_control,
            automation_start_hold_control,
        )
    ):
        raise ValueError("root and diagnostic options must be explicit bools")
    if automation_idle_control and automation_start_hold_control:
        raise ValueError(
            "automation idle and start-hold controls are mutually exclusive"
        )
    if (
        automation_idle_control or automation_start_hold_control
    ) and not close_diagnostics:
        raise ValueError("automation controls require explicit close_diagnostics")
    if (
        type(device) is not str
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", device) is None
        or device.lower().startswith(("emulator-", "synthetic", "placeholder"))
        or device.lower() in {"unknown", "device", "serial", "none", "localhost"}
    ):
        raise ValueError("explicit physical Android USB serial required")
    package_pattern = r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+"
    if type(app_id) is not str or re.fullmatch(package_pattern, app_id) is None:
        raise ValueError("explicit canonical Android App package required")
    if (
        type(activity) is not str
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_$]*)+", activity)
        is None
    ):
        raise ValueError("explicit fully qualified Android activity required")
    if type(apk_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", apk_sha256) is None:
        raise ValueError("reviewed lowercase APK SHA256 required")
    if (
        type(expected_helper_sha256) is not dict
        or set(expected_helper_sha256) != set(HELPER_PACKAGES)
        or any(
            type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in expected_helper_sha256.values()
        )
    ):
        raise ValueError("exactly three reviewed helper SHA256 values required")
    for port in (appium_port, system_port):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("explicit valid Appium/system ports required")
    if appium_port == system_port:
        raise ValueError("Appium/system ports must differ")
    for label, value, maximum in (
        ("timeout", timeout, 300),
        ("phase_timeout", phase_timeout, 300),
        ("cleanup_seconds", cleanup_seconds, 120),
    ):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 < value <= maximum
        ):
            raise ValueError(
                f"{label} must be positive, finite and at most {maximum} seconds"
            )
    if phase_timeout > timeout:
        raise ValueError("phase timeout cannot exceed total scenario budget")
    deadline = started + timeout
    for path in (apk, appium_home, appium_entry, output_root, cwd):
        if not isinstance(path, Path) or not path.is_absolute():
            raise TypeError("explicit absolute local Path arguments required")
        if path.is_symlink():
            raise ValueError("symlink local paths are not admitted")
    if not cwd.is_dir() or not appium_home.is_dir():
        raise ValueError("existing local cwd and Appium home required")
    if output_root.exists() and not output_root.is_dir():
        raise ValueError("output root must be a directory")
    _regular(apk)
    _regular(appium_entry)
    if apk.suffix != ".apk":
        raise ValueError("source must be an .apk")
    if type(node) is not str or not node or "\0" in node:
        raise ValueError("explicit local Node executable required")
    executable = shutil.which(node)
    if executable is None:
        raise ValueError("Node executable is unavailable locally")
    _regular(Path(executable).resolve())
    if not os.access(executable, os.X_OK):
        raise ValueError("Node executable is not executable")
    run_id = str(uuid.uuid4()) if run_id is None else run_id
    if type(run_id) is not str or str(uuid.UUID(run_id)) != run_id:
        raise ValueError("run_id must be a canonical UUID")
    # Reuse the inventory's APK reader, not the smaller JSON receipt reader.
    # This local-only guard enforces the same scenario deadline before leasing.
    helpers = dict(expected_helper_sha256)
    for package, path in zip(
        HELPER_PACKAGES, uiautomator2_support_apks(appium_home), strict=True
    ):
        _remaining(deadline)
        if (
            hash_helper_apk(path, require_current=lambda: _remaining(deadline))
            != helpers[package]
        ):
            raise ValueError("local helper candidate differs from reviewed SHA256")
        _remaining(deadline)

    attempt = output_root / run_id
    attempt.mkdir(parents=True, mode=0o700, exist_ok=False)
    result: dict[str, Any] = {
        "schema_version": 1,
        "scope": (
            START_HOLD_CONTROL_SCOPE
            if automation_start_hold_control
            else IDLE_CONTROL_SCOPE
            if automation_idle_control
            else SCOPE
        ),
        "run_id": run_id,
        "platform": "android",
        "device": device,
        "app_id": app_id,
        "apk_sha256": apk_sha256,
        "scenario_status": "not_run",
        "session_status": "not_run",
        "host_gate_status": "pending",
        "status": "failed",
        "observations": [],
        "startup_snapshot": {},
        "primary_error": None,
        "cleanup_errors": [],
        "phone_cleanup_errors": [],
        "phone_status": "not_acquired",
        "cleanup_order": [],
        "rf": "not_run",
    }
    hold_observation: dict[str, float] = {}
    primary: BaseException | None = None
    interruptions: list[BaseException] = []
    phone: AndroidRadioController | None = None
    acquired = False
    helper: AndroidHelperPreparation | None = None
    platform: AndroidAutomationOwner | None = None
    candidate: AndroidCandidateOwner | None = None
    automation: ScanAutomationOwner | None = None
    diagnostics: AndroidCloseDiagnostics | None = None
    helper_attempted = platform_prepared = session_started = False
    try:
        freeze_lifecycle_apk(
            apk,
            attempt / "candidate.apk",
            expected_sha256=apk_sha256,
            deadline=deadline,
        )
        _remaining(deadline)
        phone = AndroidRadioController(device, run_id, attempt / "phone.json")
        phone.acquire()
        acquired = True
        _remaining(deadline)
        owner_arguments: dict[str, Any] = {
            "phone": phone,
            "run_id": run_id,
            "appium_home": appium_home,
            "expected_apk_sha256": helpers,
            "system_port": system_port,
            "cwd": cwd,
            "deadline": deadline,
        }
        # Construct the platform scope before helper mutation so even an aborted
        # handoff has a real no-session authority for restoration.
        platform = AndroidAutomationOwner(
            **owner_arguments, app_id=app_id, output=attempt / "platform"
        )
        if close_diagnostics:
            diagnostics = AndroidCloseDiagnostics(
                phone, run_id, attempt / "platform/close-diagnostics", cwd, deadline
            )
            diagnostics.prepare()
        helper = AndroidHelperPreparation(
            **owner_arguments,
            authorization=authorize_helper_settings,
            output=attempt / "helpers",
        )
        helper_attempted = True
        helper.prepare()
        platform.prepare()
        platform_prepared = True
        candidate = AndroidCandidateOwner(
            apk=attempt / "candidate.apk",
            expected_sha256=apk_sha256,
            package=app_id,
            activity=activity,
            phone=phone,
            serial=device,
            run_id=run_id,
            attempt=attempt / "candidate",
            cwd=cwd,
            deadline=deadline,
            test_only=False,
            use_root=use_root,
        )
        candidate.install_and_launch()
        automation = ScanAutomationOwner(
            phone=phone,
            run_id=run_id,
            app_id=app_id,
            activity=activity,
            node=node,
            appium_entry=appium_entry,
            port=appium_port,
            output=attempt / "automation",
            cwd=cwd,
            deadline=deadline,
            android_owner=platform,
            android_preparation=helper,
            debug_logging=close_diagnostics,
        )
        result["session_status"] = "failed"
        driver = automation.start()
        session_started = True
        executor = driver.command_executor
        config = executor.client_config
        original_timeout = config.timeout

        def check_authority() -> None:
            _remaining(deadline)
            phone.require_owner()
            automation.require_running(deadline=deadline)
            candidate.require_scan_authority()
            if (
                driver.command_executor is not executor
                or executor.client_config is not config
            ):
                raise RuntimeError("lifecycle driver transport changed")

        def authority() -> None:
            check_authority()
            config.timeout = min(original_timeout, phase_timeout, _remaining(deadline))

        identity = candidate.bind(driver, require_owner=authority, deadline=deadline)
        if diagnostics is not None:
            diagnostics.start_live(attempt / "candidate/bound-identity.json")

        def require_identity() -> None:
            authority()
            identity()
            authority()

        result["scenario_status"] = "failed"
        if automation_idle_control:
            exercise_idle_control(
                driver,
                platform="android",
                app_id=app_id,
                timeout=phase_timeout,
                observations=result["observations"],
                require_authority=authority,
                require_app_identity=require_identity,
                deadline=deadline,
            )
        else:
            exercise_lifecycle(
                driver,
                platform="android",
                app_id=app_id,
                timeout=phase_timeout,
                observations=result["observations"],
                startup_snapshot=result["startup_snapshot"],
                require_authority=authority,
                require_app_identity=require_identity,
                deadline=deadline,
                browse=not automation_start_hold_control,
                hold_observation=hold_observation
                if automation_start_hold_control
                else None,
                before_close=(
                    lambda element, click_deadline: diagnostics.capture_before_close(
                        driver,
                        element,
                        deadline=click_deadline,
                        require_authority=check_authority,
                    )
                )
                if diagnostics is not None
                else None,
            )
        require_identity()
        result["scenario_status"] = "passed"
    except BaseException as error:  # noqa: BLE001 -- finalize owned resources before propagating interruption.
        primary = error
        result["primary_error"] = _error(error)

    cleanup_deadline = time.monotonic() + cleanup_seconds
    # Optional log reads share at most half the remaining cleanup window; a
    # stalled diagnostic must leave budget for DELETE/helper/property restoration.
    diagnostic_deadline = min(deadline, cleanup_deadline - cleanup_seconds / 2)

    def cleanup(label: str, action: Callable[[], None]) -> None:
        result["cleanup_order"].append(label)
        try:
            action()
        except BaseException as error:  # noqa: BLE001 -- independent cleanup must continue.
            result["cleanup_errors"].append(label + ": " + _error(error))
            if not isinstance(error, Exception):
                interruptions.append(error)

    if automation_start_hold_control:

        def record_start_hold() -> None:
            _remaining(cleanup_deadline)
            path = attempt / "automation/start-hold-control.json"
            path.parent.mkdir(exist_ok=True)
            publish_new_json(
                path,
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "scope": START_HOLD_CONTROL_SCOPE,
                    "browse": False,
                    "quiet_seconds": START_HOLD_QUIET_SECONDS,
                    "hold_observation": hold_observation,
                },
            )
            _remaining(cleanup_deadline)

        cleanup("start-hold-control.record", record_start_hold)
    if diagnostics is not None and session_started and primary is not None:
        # A fresh connection before DELETE distinguishes pooled-path failure
        # from a server/forwarding endpoint that cannot serve new requests.
        cleanup(
            "platform.probe-server-status",
            lambda: platform.probe_server_status(deadline=diagnostic_deadline),
        )
        cleanup(
            "platform.probe-adb-transport",
            lambda: platform.probe_adb_transport(deadline=diagnostic_deadline),
        )
    if diagnostics is not None:
        # Reap only our pre-opened host logcat child, even if binding/start failed.
        # Keep it running through the fresh-status/ADB probes, not through DELETE.
        cleanup(
            "close-diagnostics.stop-live",
            lambda: diagnostics.stop_live(cleanup_deadline),
        )
    if diagnostics is not None and session_started:
        # Capture the original server PID without refreshing its identity or
        # changing the session. Both observations precede destructive cleanup.
        cleanup(
            "platform.capture-server-logs",
            lambda: platform.capture_server_logs(deadline=diagnostic_deadline),
        )
    if diagnostics is not None and (attempt / "candidate/bound-identity.json").exists():
        cleanup(
            "close-diagnostics.capture",
            lambda: diagnostics.capture(
                attempt / "candidate/bound-identity.json",
                deadline=diagnostic_deadline,
            ),
        )
    if automation is not None:
        prior_errors = len(result["cleanup_errors"])
        cleanup("automation.close", lambda: automation.close(deadline=cleanup_deadline))
        if session_started and len(result["cleanup_errors"]) == prior_errors:
            result["session_status"] = "passed"
    else:
        if platform_prepared and platform is not None:
            cleanup("platform.finalize", lambda: platform.finalize(cleanup_deadline))
        if helper_attempted and helper is not None:
            cleanup(
                "helper.restore",
                lambda: helper.restore(automation=platform, deadline=cleanup_deadline),
            )
    if candidate is not None:
        # An automation cleanup failure after a successful UI scenario does not
        # authorize stopping that successful App/Evidence page.
        cleanup(
            "candidate.finalize",
            lambda: candidate.finalize(primary, deadline=cleanup_deadline),
        )

    if diagnostics is not None:
        cleanup(
            "close-diagnostics.restore", lambda: diagnostics.restore(cleanup_deadline)
        )

    prepared_digest = None

    def prepare_result() -> None:
        nonlocal prepared_digest
        _remaining(cleanup_deadline)
        prepared_digest = publish_new_json(attempt / "prepared-result.json", result)
        _remaining(cleanup_deadline)

    cleanup("bundle.prepare", prepare_result)

    def authorize_release() -> None:
        _remaining(cleanup_deadline)
        if result["cleanup_errors"]:
            raise RuntimeError("; ".join(result["cleanup_errors"]))
        if automation is not None:
            automation.require_closed()
        elif helper_attempted and helper is not None:
            helper.require_restored()
        if diagnostics is not None:
            diagnostics.require_restored()
        if (
            prepared_digest is None
            or AndroidAutomationOwner._record_digest(attempt / "prepared-result.json")
            != prepared_digest
        ):
            raise RuntimeError("prepared lifecycle result changed before release")
        _remaining(cleanup_deadline)

    if acquired and phone is not None:
        result["cleanup_order"].append("phone.finish")
        try:
            result["phone_cleanup_errors"] = phone.finish_app_observation(
                primary, finalize_owned=authorize_release
            )
            result["phone_status"] = phone.document["status"]
        except BaseException as error:  # noqa: BLE001 -- keep phone failure separate from primary and owner cleanup.
            result["phone_status"] = "failed"
            result["phone_cleanup_errors"].append(_error(error))
            if not isinstance(error, Exception):
                interruptions.append(error)
    if (
        primary is None
        and result["scenario_status"] == "passed"
        and result["session_status"] == "passed"
        and result["phone_status"] == "passed"
        and not result["cleanup_errors"]
        and not result["phone_cleanup_errors"]
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
        path = publish_lifecycle_bundle(attempt, result)
    except BaseException as publication:
        publication.add_note(
            f"lifecycle publication after cleanup; phone_status={result['phone_status']}; primary_error={result['primary_error']}; cleanup_errors={result['cleanup_errors']}; phone_cleanup_errors={result['phone_cleanup_errors']}"
        )
        if interruption is not None:
            interruption.add_note(
                "lifecycle bundle publication: " + _error(publication)
            )
            raise interruption from publication
        raise
    if interruption is not None:
        interruption.add_note(f"lifecycle result: {path}")
        raise interruption
    return path
