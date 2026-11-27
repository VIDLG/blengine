"""Consume prepared scan owners, run the formal UI scene, and finalize them once.

No CLI, device selection, installation, firmware provisioning or automation-server
startup is hidden here. The caller prepares those resources before handoff. A
successful session still leaves the outer host gate pending until those external
responsibilities and the full bundle are completed.

The envelope below — admission, candidate binding, fixture/phone guards, cleanup order,
phone finalization and publication — is *driver-independent*. `SceneDriver` is the seam:
the platform drivers are `scene_android_native.py` (instrumentation in the App's own
process) and `scene_ios_native.py` (in-process XCTest). Neither owns a remote UI server.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from traceback import format_exception_only
from typing import Any, Protocol

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.direct_fixture import (
    AdvertisingCloseReceipt,
    DirectFixture,
)
from tests.peripherals.nrf52840.fixture_session import FixtureSession
from tools.android.state_control import AndroidRadioController

from .bleng_android_candidate import AndroidCandidateOwner
from .bleng_ios_candidate import IosCandidateOwner
from .bleng_scan_evidence import SCAN_SCENE, SceneEvidence
from .capture_io import publish_new_json
from .ios_radio_lease import IosRadioLease
from .rf_runner import ProgressReporter, ReportedObservations, phase_scope

# One scene's own round-trip budget: the envelope takes this much of a run's single finite
# budget (or whatever is left of it) and never extends it.
MAX_SCENE_SECONDS = 2400


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("prepared scan deadline exceeded")
    return remaining


def _error(error: BaseException) -> str:
    return "".join(format_exception_only(error, show_group=True)).strip()


@dataclass
class PreparedScanHandoff:
    """In-process admission acknowledgement and the one shared cleanup deadline.

    This is bookkeeping for the named producer, not a lease or readiness token.
    Only successful exclusive admission transfers cleanup to the prepared runner.
    """

    _attempted: bool = field(default=False, init=False)
    _admitted: bool = field(default=False, init=False)
    cleanup_deadline: float | None = field(default=None, init=False)

    @property
    def admitted(self) -> bool:
        return self._admitted


class SceneDriver(Protocol):
    """What the prepared envelope needs from whatever drives one UI scene.

    The envelope is identical for every driver, so it lives once in
    `run_prepared_scene`. A driver supplies only the parts that are genuinely about the
    UI channel:

    * `require_handoff` — prove the driving resource is this run's before taking it;
    * `session` — the identity published in the result;
    * `arm` — cheap local re-verification around an operation boundary;
    * `bind` — take the candidate App this run installed and launched;
    * `click_identity` — the per-click tier, or `None` when the channel cannot offer one;
    * `drive` — run the scene and append its observations;
    * `require_closed`/`close` — retirement, in the envelope's cleanup order.
    """

    def require_handoff(
        self,
        *,
        platform: str,
        candidate: Any,
        phone: Any,
        run_id: str,
        app_id: str,
        deadline: float,
    ) -> dict[str, Any]: ...

    @property
    def session(self) -> str: ...

    def arm(self, deadline: float) -> None: ...

    def bind(
        self,
        candidate: Any,
        *,
        require_owner: Callable[[], None],
        deadline: float,
    ) -> Callable[[], None]: ...

    def click_identity(self) -> Callable[[], None] | None: ...

    def drive(self, scene: SceneEvidence, options: dict[str, Any]) -> None: ...

    def require_closed(self) -> None: ...

    def close(self, deadline: float) -> None: ...


def run_prepared_scene(
    *,
    scene_driver: SceneDriver,
    candidate: AndroidCandidateOwner | IosCandidateOwner,
    phone: AndroidRadioController | IosRadioLease,
    fixture: DirectFixture,
    plan: FixtureSession,
    app_id: str,
    output: Path,
    deadline: float,
    cleanup_seconds: float,
    scene: SceneEvidence = SCAN_SCENE,
    handoff: PreparedScanHandoff | None = None,
    before_phone_release: Callable[[BaseException | None], list[str]] | None = None,
    release_retained_app: bool = False,
    progress: ProgressReporter | None = None,
) -> Path:
    """Take ownership after admission and return a scoped result, never an RF exit.

    Admission failures leave all resources with the caller. Once the fresh output
    directory is created, this function owns App finalization, the driver's retirement,
    passive fixture close and phone finalization, even on interruption. Platform
    preparation remains caller-owned; its mandatory finalizer is handed to the driver
    owner before release. The App must already be managed-installed/launched and the
    fixture active/attested.

    `scene` selects the workbench scene this prepared run drives and the evidence
    vocabulary it may claim.

    `release_retained_app` stops the App this run launched even when the scene passes.
    The default keeps it running as the evidence a human inspects; a sequence of scenes
    sets this so the next one starts from a device with nothing of ours running.

    `progress` is the caller's stage reporter for this one scene and its cleanup. It is
    observation only: the returned bundle and its verdict never depend on it.
    """
    if handoff is None:
        handoff = PreparedScanHandoff()
    if type(handoff) is not PreparedScanHandoff or handoff._attempted:
        raise ValueError("fresh prepared scan handoff acknowledgement required")
    if not isinstance(scene, SceneEvidence):
        raise TypeError("an actual managed scene descriptor is required")
    handoff._attempted = True
    if type(deadline) not in (float, int) or not math.isfinite(deadline):
        raise ValueError("finite absolute scan deadline required")
    if not 0 < _remaining(deadline) <= MAX_SCENE_SECONDS:
        raise ValueError(f"scan budget must be at most {MAX_SCENE_SECONDS} seconds")
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
    driver_info = scene_driver.require_handoff(
        platform=platform,
        candidate=candidate,
        phone=phone,
        run_id=plan.session_id,
        app_id=app_id,
        deadline=deadline,
    )
    session_id = scene_driver.session

    def http_budget(until: float) -> None:
        scene_driver.arm(until)

    http_budget(deadline)
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
    observations: ReportedObservations = ReportedObservations(progress)
    result: dict[str, Any] = {
        "schema_version": 1,
        "scope": scene.inner_scope,
        "run_id": plan.session_id,
        "platform": platform,
        "device": phone.device,
        "app_id": app_id,
        "driver_session_id": session_id,
        "driver": driver_info,
        "candidate": candidate_info,
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
        "observations": observations,
        "startup_snapshot": {},
        "primary_error": None,
        "cleanup_errors": [],
        "phone_cleanup_errors": [],
        "cleanup_order": [],
        "fixture_close": None,
    }
    primary: BaseException | None = None
    try:
        output.mkdir(mode=0o700)  # Exclusive admission boundary; never overwrite a run.
        handoff._admitted = True
        for name, raw in snapshots.items():
            with (output / name).open("xb") as stream:
                stream.write(raw)
        require_app = scene_driver.bind(
            candidate, require_owner=require_fixture, deadline=deadline
        )

        def require_app_authority() -> None:
            http_budget(deadline)
            candidate.require_scan_authority()
            http_budget(deadline)

        def require_app_identity() -> None:
            http_budget(deadline)
            require_app()
            http_budget(deadline)

        # The per-click tier is the driver's own offer, not the envelope's: a remote
        # server can re-prove the process it dispatches into between clicks, an
        # in-process test target cannot (it owns the taps it makes).
        click_tier = scene_driver.click_identity()
        require_click_identity: Callable[[], None] | None = None
        if click_tier is not None:

            def require_click_identity() -> None:
                http_budget(deadline)
                click_tier()
                http_budget(deadline)

        result["scenario_status"] = "failed"
        scene_options = {
            "phone": phone,
            "fixture": fixture,
            "plan": plan,
            "app_id": app_id,
            "require_app_identity": require_app_identity,
            "require_click_identity": require_click_identity,
            "require_app_authority": require_app_authority,
            "timeout": min(_remaining(deadline), MAX_SCENE_SECONDS),
            "observations": observations,
            "startup_snapshot": result["startup_snapshot"],
        }
        # A repeating scene states the budget it needs. Refusing here costs one run;
        # refusing in cycle seven costs the device time and leaves a half-driven App to
        # clean up.
        if (
            scene.min_seconds is not None
            and scene_options["timeout"] < scene.min_seconds
        ):
            raise RuntimeError(
                f"the {scene.name} scene needs at least {scene.min_seconds}s of scenario "
                f"budget; this run declares {scene_options['timeout']:.0f}s"
            )
        with phase_scope(progress, "scene", kind=scene.kind):
            scene_driver.drive(scene, scene_options)
        if result["observations"] != list(scene.trace):
            raise RuntimeError(f"incomplete formal {scene.name} observation trace")
        result["scenario_status"] = "passed"
    except BaseException as error:
        if not handoff.admitted:
            raise
        primary = error
        result["primary_error"] = _error(error)

    cleanup_deadline = time.monotonic() + cleanup_seconds
    handoff.cleanup_deadline = cleanup_deadline
    interruptions: list[BaseException] = []

    def cleanup(label, action) -> None:
        result["cleanup_order"].append(label)
        try:
            with phase_scope(progress, "cleanup." + label):
                action()
        except BaseException as error:  # noqa: BLE001 -- continue independent owned cleanup.
            result["cleanup_errors"].append(label + ": " + _error(error))
            if not isinstance(error, Exception):
                interruptions.append(error)

    def close_fixture() -> None:
        # Even after phone loss/expiry, the independently owned fixture must release
        # its own resources. Its passive close checks its lease and wire deadline.
        # The disconnect is the App's act, not ours, and its GAP events are still queued
        # here; the fixture settles that itself before the one-shot close.
        fixture.settle_no_peer(deadline=cleanup_deadline)
        receipt = fixture.stop_advertising_and_close(deadline=cleanup_deadline)
        if (
            type(receipt) is not AdvertisingCloseReceipt
            or receipt.session_id != plan.session_id
            or receipt.resource_identity != plan.resource_identity
        ):
            raise RuntimeError("foreign passive fixture close receipt")
        result["fixture_close"] = asdict(receipt)

    cleanup("driver.close", lambda: scene_driver.close(deadline=cleanup_deadline))
    cleanup(
        "candidate.finalize",
        lambda: candidate.finalize(
            primary,
            deadline=cleanup_deadline,
            release_retained=release_retained_app,
        ),
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
        if before_phone_release is not None:
            permission_errors = before_phone_release(primary)
            if permission_errors:
                raise RuntimeError("; ".join(permission_errors))
        if result["cleanup_errors"]:
            raise RuntimeError("; ".join(result["cleanup_errors"]))
        require_snapshots()
        scene_driver.require_closed()
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
        with phase_scope(progress, "bundle.publish"):
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
