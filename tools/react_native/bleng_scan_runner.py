"""Consume prepared scan owners, run the formal UI scene, and finalize them once.

No CLI, device selection, installation, firmware provisioning, automation-server
startup or WDA/UiAutomator2 ownership is hidden here. The caller prepares those
resources before handoff. A successful session still leaves the outer host gate
pending until those external responsibilities and the full bundle are completed.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from dataclasses import asdict
from pathlib import Path
from traceback import format_exception_only
from typing import Any

from appium import webdriver

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.direct_fixture import (
    AdvertisingCloseReceipt,
    DirectFixture,
)
from tests.peripherals.nrf52840.fixture_session import FixtureSession
from tools.android.state_control import AndroidRadioController
from tools.mobile_appium import require_attached_driver

from .bleng_android_candidate import AndroidCandidateOwner
from .bleng_automation import ScanAutomationOwner
from .bleng_ios_candidate import IosCandidateOwner
from .bleng_scan_host import exercise_owned_scan
from .capture_io import publish_new_json
from .ios_radio_lease import IosRadioLease


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("prepared scan deadline exceeded")
    return remaining


def _error(error: BaseException) -> str:
    return "".join(format_exception_only(error, show_group=True)).strip()


def run_prepared_scan(
    *,
    candidate: AndroidCandidateOwner | IosCandidateOwner,
    phone: AndroidRadioController | IosRadioLease,
    fixture: DirectFixture,
    plan: FixtureSession,
    driver: webdriver.Remote,
    app_id: str,
    output: Path,
    deadline: float,
    cleanup_seconds: float,
    automation: ScanAutomationOwner,
) -> Path:
    """Take ownership after admission and return a scoped result, never an RF exit.

    Admission failures leave all resources with the caller. Once the fresh output
    directory is created, this function owns App finalization, this WebDriver
    session, managed automation closure, passive fixture close and phone finalization,
    even on interruption. Platform preparation remains caller-owned; its mandatory
    finalizer is handed to the automation owner before release. The App must already
    be managed-installed/launched, the fixture active/attested, and the driver
    attach-only. Neither candidate may already have been bound to another runner.
    """
    if type(deadline) not in (float, int) or not math.isfinite(deadline):
        raise ValueError("finite absolute scan deadline required")
    if not 0 < _remaining(deadline) <= 300:
        raise ValueError("scan budget must be at most 300 seconds")
    if (
        type(cleanup_seconds) not in (float, int)
        or not math.isfinite(cleanup_seconds)
        or not 0 < cleanup_seconds <= 300
    ):
        raise ValueError("cleanup_seconds must be finite and in (0, 300]")
    if type(fixture) is not DirectFixture or type(plan) is not FixtureSession:
        raise TypeError("actual prepared fixture and session required")
    if (
        type(candidate) is AndroidCandidateOwner
        and type(phone) is AndroidRadioController
    ):
        platform = "android"
    elif type(candidate) is IosCandidateOwner and type(phone) is IosRadioLease:
        platform = "ios"
    else:
        raise TypeError("candidate and actual phone lease platform must match")
    if plan.contract.fixture_role != "multifunction-rf":
        raise ValueError("formal scan requires the maintained multifunction fixture")
    candidate_info = candidate.require_scan_handoff(
        phone=phone, run_id=plan.session_id, app_id=app_id, deadline=deadline
    )
    if type(automation) is not ScanAutomationOwner:
        raise TypeError("an actual managed automation owner is required")
    automation_info = automation.require_handoff(
        driver=driver,
        phone=phone,
        run_id=plan.session_id,
        app_id=app_id,
        deadline=deadline,
    )
    session_id = driver.session_id
    executor = driver.command_executor
    config = executor.client_config
    original_http_timeout = config.timeout
    if (
        type(original_http_timeout) not in (float, int)
        or not math.isfinite(original_http_timeout)
        or original_http_timeout <= 0
    ):
        raise ValueError("driver must already have a finite HTTP timeout")

    def require_driver() -> None:
        require_attached_driver(
            driver,
            session_id=session_id,
            platform=platform,
            device=phone.device,
            app_id=app_id,
        )
        automation.require_running(deadline=deadline)
        if (
            driver.command_executor is not executor
            or executor.client_config is not config
        ):
            raise RuntimeError("prepared driver executor changed")

    def http_budget(until: float) -> None:
        require_driver()
        config.timeout = min(original_http_timeout, _remaining(until))

    require_driver()
    fixture.require_active_session(plan)
    snapshots = {
        name: fixture_session._read_bytes(path)
        for name, path in (
            ("fixture-request.json", plan.request_path),
            ("fixture-receipt.json", plan.receipt_path),
        )
    }

    def require_snapshots() -> None:
        for name, path in (
            ("fixture-request.json", plan.request_path),
            ("fixture-receipt.json", plan.receipt_path),
        ):
            if fixture_session._read_bytes(path) != snapshots[name]:
                raise RuntimeError("prepared scan fixture evidence changed")

    def require_fixture() -> None:
        _remaining(deadline)
        phone.require_owner()
        fixture.require_active_session(plan)
        require_snapshots()
        fixture_session.verify_receipt(
            plan, fixture_identity=(os.getpid(), os.getpgid(0))
        )
        require_snapshots()
        _remaining(deadline)

    require_fixture()
    output = output.absolute()
    output.mkdir(mode=0o700)  # Exclusive admission boundary; never overwrite a run.
    result: dict[str, Any] = {
        "schema_version": 1,
        "scope": "formal-rn-prepared-scan",
        "run_id": plan.session_id,
        "platform": platform,
        "device": phone.device,
        "app_id": app_id,
        "driver_session_id": session_id,
        "candidate": candidate_info,
        "automation": automation_info,
        "fixture": {
            "resource_identity": plan.resource_identity,
            "contract_sha256": plan.contract_sha256,
            "profile_sha256": plan.profile_sha256,
        },
        "fixture_input_sha256": {
            name: hashlib.sha256(raw).hexdigest() for name, raw in snapshots.items()
        },
        "scenario_status": "not_run",
        "session_status": "pending",
        "host_gate_status": "pending",
        "pending_host_requirements": [
            "physical-selection-and-platform-preflight",
            "automation-and-borrowed-fixture-lease-finalization",
            "complete-host-evidence-bundle",
        ],
        "observations": [],
        "startup_snapshot": {},
        "primary_error": None,
        "cleanup_errors": [],
        "phone_cleanup_errors": [],
        "cleanup_order": [],
        "fixture_close": None,
    }
    primary: BaseException | None = None
    try:
        for name, raw in snapshots.items():
            with (output / name).open("xb") as stream:
                stream.write(raw)
        require_app = candidate.bind(
            driver, require_owner=require_fixture, deadline=deadline
        )

        def require_app_authority() -> None:
            http_budget(deadline)
            candidate.require_scan_authority()
            http_budget(deadline)

        def require_app_identity() -> None:
            http_budget(deadline)
            require_app()
            http_budget(deadline)

        result["scenario_status"] = "failed"
        exercise_owned_scan(
            driver,
            phone=phone,
            fixture=fixture,
            plan=plan,
            app_id=app_id,
            require_app_identity=require_app_identity,
            require_app_authority=require_app_authority,
            timeout=_remaining(deadline),
            observations=result["observations"],
            startup_snapshot=result["startup_snapshot"],
        )
        if result["observations"] != [
            "idle",
            "started",
            "scan:running",
            "scan:advertisement-matched",
            "scan:stopped",
            "closed",
        ]:
            raise RuntimeError("incomplete formal scan observation trace")
        result["scenario_status"] = "passed"
    except BaseException as error:  # noqa: BLE001 -- finalize all handed-off resources, then re-raise interruptions.
        primary = error
        result["primary_error"] = _error(error)

    cleanup_deadline = time.monotonic() + cleanup_seconds
    interruptions: list[BaseException] = []

    def cleanup(label, action) -> None:
        result["cleanup_order"].append(label)
        try:
            action()
        except BaseException as error:  # noqa: BLE001 -- continue independent owned cleanup.
            result["cleanup_errors"].append(label + ": " + _error(error))
            if not isinstance(error, Exception):
                interruptions.append(error)

    def close_fixture() -> None:
        # Even after phone loss/expiry, the independently owned fixture must release
        # its own resources. Its passive close checks its lease and wire deadline.
        receipt = fixture.stop_advertising_and_close(deadline=cleanup_deadline)
        if (
            type(receipt) is not AdvertisingCloseReceipt
            or receipt.session_id != plan.session_id
            or receipt.resource_identity != plan.resource_identity
        ):
            raise RuntimeError("foreign passive fixture close receipt")
        result["fixture_close"] = asdict(receipt)

    cleanup("automation.close", lambda: automation.close(deadline=cleanup_deadline))
    cleanup(
        "candidate.finalize",
        lambda: candidate.finalize(primary, deadline=cleanup_deadline),
    )
    cleanup("fixture.close", close_fixture)
    cleanup("fixture.evidence", require_snapshots)
    prepared_sha: str | None = None

    def publish_prepared() -> None:
        nonlocal prepared_sha
        _remaining(cleanup_deadline)
        prepared_sha = publish_new_json(output / "prepared-result.json", result)
        _remaining(cleanup_deadline)

    cleanup("bundle.prepare", publish_prepared)

    def authorize_release() -> None:
        _remaining(cleanup_deadline)
        if result["cleanup_errors"]:
            raise RuntimeError("; ".join(result["cleanup_errors"]))
        require_snapshots()
        automation.require_closed()
        raw = fixture_session._read_bytes(output / "prepared-result.json")
        if prepared_sha is None or hashlib.sha256(raw).hexdigest() != prepared_sha:
            raise RuntimeError("prepared scan result changed before phone release")
        _remaining(cleanup_deadline)

    result["cleanup_order"].append("phone.finish")
    if platform == "ios":
        result["phone_cleanup_errors"] = phone.finish_observation(
            primary, finalize_owned=authorize_release
        )
    else:
        result["phone_cleanup_errors"] = phone.finish_app_observation(
            primary, finalize_owned=authorize_release
        )
    result["phone_journal"] = {
        "path": str(phone.path),
        "status": phone.document["status"],
    }
    try:
        raw = fixture_session._read_bytes(phone.path)
        result["phone_journal"]["sha256"] = hashlib.sha256(raw).hexdigest()
    except Exception as error:  # noqa: BLE001 -- a missing journal cannot become a pass.
        result["cleanup_errors"].append("phone.evidence: " + _error(error))
    failed = (
        primary is not None
        or result["cleanup_errors"]
        or result["phone_cleanup_errors"]
    )
    result["session_status"] = "failed" if failed else "passed"
    result["host_gate_status"] = "failed" if failed else "pending"
    path = output / "result.json"
    try:
        publish_new_json(path, result)
    except BaseException as publication:
        prior = (
            primary
            if primary is not None
            else interruptions[0]
            if interruptions
            else None
        )
        if prior is not None:
            prior.add_note(f"final scan evidence publication: {_error(publication)}")
            raise prior from publication
        if result["cleanup_errors"] or result["phone_cleanup_errors"]:
            publication.add_note(
                f"earlier cleanup errors: {result['cleanup_errors']}; phone: {result['phone_cleanup_errors']}"
            )
        raise
    # Publication after lease release cannot retroactively quarantine a released
    # lock. On failure no final result exists; prepared-result remains pending.
    if primary is not None and not isinstance(primary, Exception):
        primary.add_note(f"prepared scan evidence: {path}")
        raise primary
    if interruptions:
        interruptions[0].add_note(f"prepared scan evidence: {path}")
        raise interruptions[0]
    return path
