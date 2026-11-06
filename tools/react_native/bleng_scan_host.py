"""In-process ownership seam for the formal BLeng scan UI, not a device runner."""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.direct_fixture import DirectFixture
from tests.peripherals.nrf52840.fixture_session import FixtureSession
from tools.android.state_control import AndroidRadioController
from tools.react_native import android_sdk, ios_sdk, run_bleng_ui
from tools.react_native.ios_radio_lease import SCAN_OBSERVATION_SCOPE, IosRadioLease

if TYPE_CHECKING:
    from appium import webdriver


def _require_android_host(
    *,
    driver: webdriver.Remote,
    session_id: str,
    phone: AndroidRadioController,
    serial: str,
    run_id: str,
    app_id: str,
    expected_apk_sha256: str,
    read_candidate_sha256: Callable[[], str],
    require_owner: Callable[[], None],
    deadline: float,
) -> None:
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("deadline must be a finite absolute monotonic time")

    def current() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("Android App binding total deadline exceeded")
        if (
            type(phone) is not AndroidRadioController
            or not serial
            or serial.lower().startswith("emulator-")
            or phone.device != serial
            or phone.run_id != run_id
            or phone.document.get("device") != serial
            or phone.document.get("run_id") != run_id
        ):
            raise RuntimeError("Android phone/device/run binding changed")
        caps = driver.capabilities
        if (
            not isinstance(session_id, str)
            or not session_id.strip()
            or driver.session_id != session_id
            or not isinstance(caps, dict)
            or not isinstance(caps.get("platformName"), str)
            or caps["platformName"].lower() != "android"
        ):
            raise RuntimeError("Android driver session/platform binding changed")
        for names, expected in (
            (("appium:udid", "udid"), serial),
            (("appium:appPackage", "appPackage"), app_id),
        ):
            values = [caps[name] for name in names if name in caps]
            if not values or any(value != expected for value in values):
                raise RuntimeError(
                    "Android driver device/App binding changed or ambiguous"
                )

    current()
    if not callable(require_owner):
        raise TypeError("require_owner must be a protocol/fixture owner callback")
    phone.require_owner()
    current()
    require_owner()
    current()
    if not callable(read_candidate_sha256):
        raise TypeError(
            "read_candidate_sha256 must validate the current host candidate"
        )
    if read_candidate_sha256() != expected_apk_sha256:
        raise RuntimeError("Android candidate SHA256 changed")
    current()


@dataclass(frozen=True)
class AndroidAppBinding:
    """Pinned Android identity callback for exercise_owned_scan's trusted seam.

    Every call performs a fresh, fully bracketed SDK snapshot (13 read commands
    on success), not a cached liveness check. The host-supplied candidate reader
    must revalidate the current candidate and return its lowercase APK SHA256.
    The explicit protocol/fixture owner callback runs alongside the phone lease
    check before and after every SDK command, under the same total deadline.
    Capability checks bind the local driver configuration; they do not attest an
    Appium server, foreground UI, native boot timestamp, RF or resource cleanup.
    Frequent UI guards can be expensive; real-device cost/support is unverified.
    """

    driver: webdriver.Remote
    session_id: str
    phone: AndroidRadioController
    run_id: str
    identity: android_sdk.AndroidAppIdentity
    read_candidate_sha256: Callable[[], str]
    require_protocol_owner: Callable[[], None]
    cwd: Path
    deadline: float

    def _require_owner(self) -> None:
        _require_android_host(
            driver=self.driver,
            session_id=self.session_id,
            phone=self.phone,
            serial=self.identity.serial,
            run_id=self.run_id,
            app_id=self.identity.package,
            expected_apk_sha256=self.identity.apk_sha256,
            read_candidate_sha256=self.read_candidate_sha256,
            require_owner=self.require_protocol_owner,
            deadline=self.deadline,
        )

    def require_current(
        self,
        *,
        driver: webdriver.Remote,
        phone: AndroidRadioController,
        serial: str,
        run_id: str,
        app_id: str,
        expected_apk_sha256: str,
    ) -> None:
        """Reject caller rebinding as well as restart/PID reuse; never repin."""
        if (
            driver is not self.driver
            or phone is not self.phone
            or serial != self.identity.serial
            or run_id != self.run_id
            or app_id != self.identity.package
            or expected_apk_sha256 != self.identity.apk_sha256
        ):
            raise RuntimeError("Android App binding arguments changed")
        self._require_owner()
        current = android_sdk.read_app_identity(
            serial=serial,
            package=app_id,
            expected_apk_sha256=expected_apk_sha256,
            expected_identity=self.identity,
            cwd=self.cwd,
            deadline=self.deadline,
            require_owner=self._require_owner,
        )
        self._require_owner()
        if current != self.identity:
            raise RuntimeError("Android pinned App process/install identity changed")

    def __call__(self) -> None:
        self.require_current(
            driver=self.driver,
            phone=self.phone,
            serial=self.identity.serial,
            run_id=self.run_id,
            app_id=self.identity.package,
            expected_apk_sha256=self.identity.apk_sha256,
        )


def pin_android_app(
    driver: webdriver.Remote,
    *,
    phone: AndroidRadioController,
    serial: str,
    run_id: str,
    app_id: str,
    expected_apk_sha256: str,
    read_candidate_sha256: Callable[[], str],
    require_owner: Callable[[], None],
    cwd: Path,
    deadline: float,
) -> AndroidAppBinding:
    """Bind an already-running App; never install, launch, select or repair it."""
    session_id = driver.session_id

    def guard() -> None:
        _require_android_host(
            driver=driver,
            session_id=session_id,
            phone=phone,
            serial=serial,
            run_id=run_id,
            app_id=app_id,
            expected_apk_sha256=expected_apk_sha256,
            read_candidate_sha256=read_candidate_sha256,
            require_owner=require_owner,
            deadline=deadline,
        )

    guard()
    identity = android_sdk.read_app_identity(
        serial=serial,
        package=app_id,
        expected_apk_sha256=expected_apk_sha256,
        expected_identity=None,
        cwd=cwd,
        deadline=deadline,
        require_owner=guard,
    )
    guard()
    return AndroidAppBinding(
        driver=driver,
        session_id=session_id,
        phone=phone,
        run_id=run_id,
        identity=identity,
        read_candidate_sha256=read_candidate_sha256,
        require_protocol_owner=require_owner,
        cwd=cwd,
        deadline=deadline,
    )


def _require_ios_host(
    *,
    driver: webdriver.Remote,
    session_id: str,
    phone: IosRadioLease,
    device: str,
    run_id: str,
    app_id: str,
    expected_candidate_sha256: str,
    read_candidate_sha256: Callable[[], str],
    require_candidate_provenance: Callable[[], None],
    require_owner: Callable[[], None],
    deadline: float,
) -> None:
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("deadline must be a finite absolute monotonic time")
    if (
        type(expected_candidate_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", expected_candidate_sha256) is None
        or not all(
            callable(callback)
            for callback in (
                read_candidate_sha256,
                require_candidate_provenance,
                require_owner,
            )
        )
    ):
        raise TypeError(
            "explicit frozen candidate SHA256, reader, provenance and owner callbacks required"
        )

    def current() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("iOS App binding total deadline exceeded")
        if (
            type(phone) is not IosRadioLease
            or phone.device != device
            or phone.run_id != run_id
            or phone.scope != SCAN_OBSERVATION_SCOPE
            or phone.document.get("device") != device
            or phone.document.get("run_id") != run_id
            or phone.document.get("scope") != SCAN_OBSERVATION_SCOPE
        ):
            raise RuntimeError("iOS phone/device/run binding changed")
        caps = driver.capabilities
        if (
            type(session_id) is not str
            or not session_id.strip()
            or driver.session_id != session_id
            or type(caps) is not dict
            or type(caps.get("platformName")) is not str
            or caps["platformName"].lower() != "ios"
        ):
            raise RuntimeError("iOS driver session/platform binding changed")
        for names, expected in (
            (("appium:udid", "udid"), device),
            (("appium:bundleId", "bundleId"), app_id),
        ):
            values = [caps[name] for name in names if name in caps]
            if not values or any(value != expected for value in values):
                raise RuntimeError("iOS driver device/App binding changed or ambiguous")

    current()
    phone.require_owner()
    current()
    require_owner()
    current()
    if read_candidate_sha256() != expected_candidate_sha256:
        raise RuntimeError("iOS local frozen candidate SHA256 changed")
    current()
    require_candidate_provenance()
    current()


@dataclass(frozen=True)
class IosAppBinding:
    """Fresh native identity callback for ``exercise_owned_scan``; never a runner.

    Each call performs six SDK commands and a new challenge under one total
    deadline, not a cached success. ``require_candidate_provenance`` must verify
    this run's managed install/launch owner and its exact receipts against the
    local frozen artifact. It is mandatory and has no permissive implementation
    here: CoreDevice receipts/native IPC do not attest an installed artifact hash.
    All callbacks must honor the same deadline; synchronous callbacks cannot be
    preempted. Frequent guard latency/support on real iOS remains unverified.
    """

    driver: webdriver.Remote
    session_id: str
    phone: IosRadioLease
    run_id: str
    expected_candidate_sha256: str
    identity: ios_sdk.IosAppIdentity
    read_candidate_sha256: Callable[[], str]
    require_candidate_provenance: Callable[[], None]
    require_protocol_owner: Callable[[], None]
    install_receipt: Path
    launch_receipt: Path
    attempt: Path
    cwd: Path
    deadline: float

    def _require_owner(self) -> None:
        _require_ios_host(
            driver=self.driver,
            session_id=self.session_id,
            phone=self.phone,
            device=self.identity.device,
            run_id=self.run_id,
            app_id=self.identity.bundle_id,
            expected_candidate_sha256=self.expected_candidate_sha256,
            read_candidate_sha256=self.read_candidate_sha256,
            require_candidate_provenance=self.require_candidate_provenance,
            require_owner=self.require_protocol_owner,
            deadline=self.deadline,
        )

    def require_current(
        self,
        *,
        driver: webdriver.Remote,
        phone: IosRadioLease,
        device: str,
        run_id: str,
        app_id: str,
        expected_candidate_sha256: str,
    ) -> None:
        """Reject caller rebinding and native restart/PID reuse; never repin."""
        if (
            driver is not self.driver
            or phone is not self.phone
            or device != self.identity.device
            or run_id != self.run_id
            or app_id != self.identity.bundle_id
            or expected_candidate_sha256 != self.expected_candidate_sha256
        ):
            raise RuntimeError("iOS App binding arguments changed")
        self._require_owner()
        identity = ios_sdk.read_app_identity(
            device=device,
            run_id=run_id,
            bundle_id=app_id,
            install_receipt=self.install_receipt,
            launch_receipt=self.launch_receipt,
            attempt=self.attempt,
            expected_identity=self.identity,
            require_owner=self._require_owner,
            cwd=self.cwd,
            deadline=self.deadline,
        )
        self._require_owner()
        if identity != self.identity:
            raise RuntimeError("iOS pinned App process/install identity changed")

    def __call__(self) -> None:
        self.require_current(
            driver=self.driver,
            phone=self.phone,
            device=self.identity.device,
            run_id=self.run_id,
            app_id=self.identity.bundle_id,
            expected_candidate_sha256=self.expected_candidate_sha256,
        )


def pin_ios_app(
    driver: webdriver.Remote,
    *,
    phone: IosRadioLease,
    device: str,
    run_id: str,
    app_id: str,
    expected_candidate_sha256: str,
    read_candidate_sha256: Callable[[], str],
    require_candidate_provenance: Callable[[], None],
    require_owner: Callable[[], None],
    install_receipt: Path,
    launch_receipt: Path,
    attempt: Path,
    cwd: Path,
    deadline: float,
) -> IosAppBinding:
    """Pin an existing install/launch only after a current process + fresh IPC.

    The caller supplies existing official receipts and the mandatory provenance
    callback for this run's managed installation of the frozen candidate. Neither
    a historical launch receipt nor a native file alone establishes trust. The
    resulting callable is passed directly as ``require_app_identity``; no WDA or
    scan/UI policy is introduced. Failure never cleans up or restarts the App.
    """
    install_receipt = install_receipt.absolute()
    launch_receipt = launch_receipt.absolute()
    attempt, cwd = attempt.absolute(), cwd.absolute()
    session_id = driver.session_id

    def guard() -> None:
        _require_ios_host(
            driver=driver,
            session_id=session_id,
            phone=phone,
            device=device,
            run_id=run_id,
            app_id=app_id,
            expected_candidate_sha256=expected_candidate_sha256,
            read_candidate_sha256=read_candidate_sha256,
            require_candidate_provenance=require_candidate_provenance,
            require_owner=require_owner,
            deadline=deadline,
        )

    guard()
    identity = ios_sdk.read_app_identity(
        device=device,
        run_id=run_id,
        bundle_id=app_id,
        install_receipt=install_receipt,
        launch_receipt=launch_receipt,
        attempt=attempt,
        expected_identity=None,
        require_owner=guard,
        cwd=cwd,
        deadline=deadline,
    )
    guard()
    return IosAppBinding(
        driver=driver,
        session_id=session_id,
        phone=phone,
        run_id=run_id,
        expected_candidate_sha256=expected_candidate_sha256,
        identity=identity,
        read_candidate_sha256=read_candidate_sha256,
        require_candidate_provenance=require_candidate_provenance,
        require_protocol_owner=require_owner,
        install_receipt=install_receipt,
        launch_receipt=launch_receipt,
        attempt=attempt,
        cwd=cwd,
        deadline=deadline,
    )


def exercise_owned_scan(
    driver: webdriver.Remote,
    *,
    phone: IosRadioLease | AndroidRadioController,
    fixture: DirectFixture,
    plan: FixtureSession,
    app_id: str,
    require_app_identity: Callable[[], None],
    timeout: float,
    require_app_authority: Callable[[], None] | None = None,
    observations: list[str],
    startup_snapshot: dict[str, str] | None = None,
    max_pulls: int = 8,
    observation_timeout_ms: int = 5000,
) -> None:
    """Exercise only UI using the host's already-owned phone, fixture and App.

    The required callback must validate the current candidate/PID/boot/App binding
    for this driver and phone; it has no no-op default. It is a trusted host seam,
    not a sandbox. The caller must bound the callback and driver's HTTP deadlines:
    this function checks elapsed time but cannot interrupt an in-flight call.

    Neither that callback, the phone scope nor this local active-fixture guard
    proves RF liveness or native resource-zero. Phone selection (including Android
    select_test_device("physical")), installation, startup, and all failure cleanup
    and finalization remain with the caller. There are no radio commands, SDK
    setup or fixture wire I/O here.
    """
    entered = time.monotonic()
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= 300
    ):
        raise ValueError("timeout must be finite and in (0, 300] seconds")
    deadline = entered + timeout
    if not callable(require_app_identity):
        raise TypeError("require_app_identity must be a host App identity callback")
    if require_app_authority is not None and not callable(require_app_authority):
        raise TypeError("require_app_authority must be a host ownership callback")
    app_authority = require_app_authority or require_app_identity
    if type(phone) is IosRadioLease:
        platform = "ios"
    elif type(phone) is AndroidRadioController:
        platform = "android"
    else:
        raise TypeError(
            "phone must be an actual IosRadioLease or AndroidRadioController"
        )
    if type(fixture) is not DirectFixture or type(plan) is not FixtureSession:
        raise TypeError("an actual DirectFixture and FixtureSession are required")
    if plan.contract.fixture_role != "multifunction-rf":
        raise ValueError("owned scan requires the multifunction-rf fixture role")
    if not isinstance(app_id, str) or not app_id.strip():
        raise ValueError("app_id must not be empty")
    device = phone.device
    if platform == "android" and device.lower().startswith("emulator-"):
        raise ValueError("owned scan refuses an Android emulator serial")

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("owned scan total deadline exceeded")
        return budget

    def require_owners() -> None:
        remaining()
        if (
            phone.device != device
            or phone.run_id != plan.session_id
            or phone.document["device"] != device
            or phone.document["run_id"] != plan.session_id
        ):
            raise RuntimeError(
                "phone device/run binding changed or differs from fixture session"
            )
        if platform == "ios":
            if (
                phone.scope != SCAN_OBSERVATION_SCOPE
                or phone.document["scope"] != SCAN_OBSERVATION_SCOPE
            ):
                raise RuntimeError("owned scan requires the iOS scan observation scope")
        else:
            if (
                phone.document.get("actions") != []
                or phone.initial_state is not None
                or phone.last_state is not None
                or phone.document["initial_state"] is not None
            ):
                raise RuntimeError(
                    "owned scan requires App-only observation with no radio actions or state"
                )
        phone.require_owner()
        remaining()
        fixture.require_active_session(plan)
        remaining()

    require_owners()
    request_raw = fixture_session._read_bytes(plan.request_path)
    remaining()
    receipt_raw = fixture_session._read_bytes(plan.receipt_path)
    remaining()
    snapshots = ((plan.request_path, request_raw), (plan.receipt_path, receipt_raw))

    def require_frozen_bytes() -> None:
        for path, raw in snapshots:
            current = fixture_session._read_bytes(path)
            remaining()
            if current != raw:
                raise fixture_session.FixtureSessionError(
                    "owned scan request/receipt bytes changed"
                )

    def require_proof() -> None:
        require_frozen_bytes()
        # The authoritative verifier reloads the session and checks its snapshot hashes.
        fixture_session.verify_receipt(
            plan, fixture_identity=(os.getpid(), os.getpgid(0))
        )
        remaining()
        require_frozen_bytes()

    def guard() -> None:
        require_owners()
        require_proof()
        app_authority()
        remaining()
        require_owners()
        require_proof()

    def identity_checkpoint() -> None:
        guard()
        require_app_identity()
        guard()

    guard()
    run_bleng_ui.exercise_scan(
        driver,
        platform=platform,
        app_id=app_id,
        timeout=remaining(),
        observations=observations,
        expected_local_name=plan.contract.local_name,
        expected_service_uuid=plan.contract.service_uuid,
        require_authority=guard,
        require_app_identity=identity_checkpoint,
        startup_snapshot=startup_snapshot,
        max_pulls=max_pulls,
        observation_timeout_ms=observation_timeout_ms,
    )
    guard()
