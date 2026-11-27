"""Fresh-install Bluetooth consent on the formal App's own UI test target.

The host owns the state change that makes each phase meaningful: it removes the App, so the new
installation's first `CBCentralManager` raises the real system consent alert. It then runs one
phase (`deny`, then `allow`), and requires each xcresult to be one exact pass.

No radio, fixture, Probe or RF is involved, and nothing here builds or signs: the lane consumes the
signed App + manifest that `formal-host-ios-device-test-build` produced. The App is left visible on
success so a human can inspect the final state.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from traceback import format_exception_only
from typing import Any

from tools.apple import ios_preflight
from tools.file_digest import sha256_file

from . import ios_sdk
from .bleng_ios_native_common import (
    app_bundle_id,
    resolve_xctestrun,
    staged_xctestrun,
)
from .capture_io import publish_new_json

SCOPE = "formal-ios-permission-authorization"
TEST_BUNDLE = "BleHubBlengHostUITests.xctest"
TEST_CLASS = "BlengPermissionAuthorizationUITests"
METHOD = "testBluetoothConsentPhase"
PHASE_VARIABLE = "BLEHUB_IOS_PERMISSION_PHASE"
# `deny` first, then `allow`: the device ends in the authorized state it started from.
PHASES = ("deny", "allow")
TIMEOUT_MAX = 1800.0
XCRUN = "/usr/bin/xcrun"


def _error(error: BaseException) -> str:
    return "".join(format_exception_only(error, show_group=True)).strip()


def _remaining(deadline: float, label: str) -> float:
    budget = deadline - time.monotonic()
    if not math.isfinite(budget) or budget <= 0:
        raise TimeoutError(f"{label} exceeded the run budget")
    return budget


def _installed(device: str, bundle_id: str, *, cwd: Path, timeout: float) -> bool:
    # `devicectl` writes the JSON document to a file (stdout carries a human table), so a
    # temporary directory is the only reliable way to read the inventory from this command.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "apps.json"
        ios_sdk.run_command(
            [
                XCRUN,
                "devicectl",
                "device",
                "info",
                "apps",
                "--device",
                device,
                "--json-output",
                str(path),
            ],
            cwd=cwd,
            timeout=timeout,
            check=True,
        )
        value = json.loads(path.read_bytes() or b"{}")
    apps = value.get("result", {}).get("apps", [])
    return any(
        isinstance(app, dict) and app.get("bundleIdentifier") == bundle_id
        for app in apps
    )


def _uninstall_app(
    device: str, bundle_id: str, *, cwd: Path, deadline: float
) -> dict[str, Any]:
    """Remove the App so the next installation has no recorded Bluetooth decision."""
    if not _installed(
        device, bundle_id, cwd=cwd, timeout=_remaining(deadline, "app list")
    ):
        return {"bundle_id": bundle_id, "removed": False, "reason": "not installed"}
    completed = ios_sdk.run_command(
        [
            XCRUN,
            "devicectl",
            "device",
            "uninstall",
            "app",
            "--device",
            device,
            bundle_id,
        ],
        cwd=cwd,
        timeout=_remaining(deadline, "app uninstall"),
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"could not remove the App for a fresh consent state (exit={completed.returncode}): "
            f"{(completed.stdout or '')[-800:]}"
        )
    if _installed(device, bundle_id, cwd=cwd, timeout=_remaining(deadline, "app list")):
        raise RuntimeError("the App is still installed after the uninstall request")
    return {"bundle_id": bundle_id, "removed": True}


def _test_summary(bundle: Path, *, cwd: Path) -> dict[str, Any]:
    completed = ios_sdk.run_command(
        [
            XCRUN,
            "xcresulttool",
            "get",
            "test-results",
            "summary",
            "--path",
            str(bundle),
            "--compact",
        ],
        cwd=cwd,
        timeout=120,
        check=True,
    )
    return json.loads(completed.stdout)


def run_permission_ui(
    *,
    device: str,
    xctestrun: Path,
    output_root: Path,
    timeout: float,
    restore_only: bool,
    authorize_permission_reset: bool,
    cwd: Path,
    run_id: str | None = None,
) -> Path:
    """Run the fresh-install consent phases; return the published result path."""
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= TIMEOUT_MAX
    ):
        raise ValueError(f"timeout must be finite, positive and at most {TIMEOUT_MAX}")
    if authorize_permission_reset is not True:
        raise ValueError(
            "explicit --authorize-permission-reset is required: this lane removes and reinstalls "
            "the App to produce a fresh OS consent decision"
        )
    if type(restore_only) is not bool:
        raise ValueError("restore_only must be an explicit bool")
    if type(device) is not str or not device.strip():
        raise ValueError("an explicit physical device UDID is required")
    if not xctestrun.is_file() or xctestrun.is_symlink():
        raise ValueError(f"missing xctestrun: {xctestrun}")

    target = resolve_xctestrun(xctestrun, test_bundle=TEST_BUNDLE)
    app_path = target.document[target.name].get("UITargetAppPath")
    if not isinstance(app_path, str) or not app_path:
        raise ValueError("xctestrun target declares no UITargetAppPath")
    bundle_id = app_bundle_id(Path(app_path))

    run_id = str(uuid.uuid4()) if run_id is None else run_id
    if type(run_id) is not str or str(uuid.UUID(run_id)) != run_id:
        raise ValueError("run_id must be a canonical UUID")
    deadline = time.monotonic() + timeout
    attempt = output_root / run_id
    attempt.mkdir(parents=True, mode=0o700, exist_ok=False)
    phases = ("allow",) if restore_only else PHASES
    result: dict[str, Any] = {
        "schema_version": 1,
        "scope": SCOPE,
        "run_id": run_id,
        "platform": "ios",
        "device": device,
        "status": "failed",
        "rf": "none",
        "restore_only": restore_only,
        "xctestrun_sha256": sha256_file(xctestrun),
        "test_target": target.name,
        "app_bundle_id": bundle_id,
        "only_testing": f"{target.name}/{TEST_CLASS}/{METHOD}",
        "preflight": None,
        "phases": {},
        "primary_error": None,
        "primary_traceback": None,
    }
    publish_new_json(
        attempt / "request.json",
        {
            "schema_version": 1,
            "scope": SCOPE,
            "run_id": run_id,
            "device": device,
            "bundle_id": bundle_id,
            "restore_only": restore_only,
            "phases": list(phases),
            "authorize_permission_reset": True,
            "timeout": timeout,
            "host_pid": os.getpid(),
            "host_pgid": os.getpgid(0),
        },
    )

    primary: BaseException | None = None
    try:
        # Read-only admission: the runner artifact, its Team/profile and the installed runner
        # inventory must match this manifest before anything is installed.
        preflight = ios_preflight.preflight(
            mode="build-signing",
            device=device,
            xctestrun=str(xctestrun),
        )
        result["preflight"] = {
            "status": preflight.get("status"),
            "runner_bundle_id": preflight["observations"]
            .get("ui-test-runner-inventory", {})
            .get("expected_runner_bundle_id"),
        }
        if preflight.get("status") != "verified":
            blocked = [
                name
                for name, entry in preflight["observations"].items()
                if entry.get("status") == "blocked"
            ]
            raise RuntimeError(f"iOS preflight blocked: {', '.join(blocked)}")

        for phase in phases:
            _remaining(deadline, f"{phase} phase")
            result["phases"][phase] = {
                "uninstall": _uninstall_app(
                    device, bundle_id, cwd=cwd, deadline=deadline
                )
            }
            staged = staged_xctestrun(
                xctestrun,
                attempt / f"{phase}.xctestrun",
                test_bundle=TEST_BUNDLE,
                environment={PHASE_VARIABLE: phase},
            )
            bundle = attempt / f"{phase}.xcresult"
            command = ios_sdk.xcodebuild_test_without_building(
                xctestrun=staged,
                device=device,
                result=bundle,
                only_testing=result["only_testing"],
                quiet=True,
            )
            completed = ios_sdk.run_command(
                command,
                cwd=cwd,
                timeout=_remaining(deadline, f"{phase} run"),
                check=False,
            )
            (attempt / f"xcodebuild-{phase}.log").write_text(
                completed.stdout or "", encoding="utf-8"
            )
            result["phases"][phase]["xcodebuild_returncode"] = completed.returncode
            if not bundle.is_dir():
                raise RuntimeError(
                    f"{phase}: xcodebuild produced no result bundle "
                    f"(exit={completed.returncode}): {(completed.stdout or '')[-1200:]}"
                )
            summary = _test_summary(bundle, cwd=cwd)
            result["phases"][phase]["result_bundle"] = str(bundle)
            result["phases"][phase]["test_summary"] = summary
            if (
                summary.get("passedTests") != 1
                or summary.get("totalTestCount") != 1
                or summary.get("failedTests") != 0
                or summary.get("skippedTests") != 0
            ):
                raise RuntimeError(f"{phase}: result is not one exact pass: {summary}")
        result["status"] = "passed"
    except BaseException as error:  # noqa: BLE001 -- publish the attempt before propagating
        primary = error
        result["primary_error"] = _error(error)
        result["primary_traceback"] = traceback.format_exception(error)

    path = attempt / "result.json"
    publish_new_json(path, result)
    if primary is not None:
        raise primary
    return path


def read_result(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if type(value) is not dict or value.get("scope") != SCOPE:
        raise ValueError("published result is not this lane's scope")
    return value


__all__ = [
    "METHOD",
    "PHASES",
    "SCOPE",
    "TEST_BUNDLE",
    "TEST_CLASS",
    "read_result",
    "run_permission_ui",
]
