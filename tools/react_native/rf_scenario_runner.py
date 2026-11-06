"""Shared attested full-suite, RecoveryTen and named opt-in REJECT orchestration.

Ordinary execution installs a frozen artifact and never builds or flashes.
Each CLI selects its scenario; capture, proof and cleanup retain one owner.
"""

from __future__ import annotations

import signal
import sys
import uuid
from functools import partial
from pathlib import Path

from tests.peripherals.nrf52840 import provider, write_reject
from tests.peripherals.nrf52840.cancellation_control import (
    require_multifunction,
    write_document,
)
from tests.peripherals.nrf52840.direct_fixture import require_v2_coordination
from tests.peripherals.nrf52840.fixture_session import (
    load_session_profile,
    prepare_session,
    verify_receipt,
)
from tests.peripherals.nrf52840.recovery_ten import (
    CONTROL_NAME,
    MODE,
    PHYSICAL_NAME,
)
from tools.react_native import (
    android_sdk,
    full_suite_capture,
    full_suite_ledger,
    ios_sdk,
    recovery_ten_capture,
    recovery_ten_ledger,
    write_reject_capture,
    write_reject_ledger,
)
from tools.react_native.android_rf_host import verify_install_identity
from tools.react_native.host_contract import ANDROID_PACKAGE
from tools.react_native.host_paths import PROJECT_ROOT
from tools.react_native.ios_device import require_ready_unlocked_device
from tools.react_native.rf_attempt_finalizer import finalize_rf_attempt
from tools.react_native.rf_evidence_bundle import EvidenceBundle
from tools.react_native.rf_fixture import (
    fixture_identity_from_stdout,
    guard_fixture,
    preflight_fixture,
)


def run(
    platform: str,
    device: str,
    artifact: Path,
    fixture_contract: Path,
    fixture_profile: Path,
    *,
    timeout: float = 900,
    scenario: str,
    port: str | None = None,
    observe_notification_tx: bool = False,
) -> Path:
    """Install only the frozen candidate; publish success after proof and cleanup."""
    full = scenario == "full-suite"
    write = scenario == "write-reject"
    ccc = scenario == "ccc-enable-reject"
    disable = scenario == "ccc-disable-reject"
    reject = write or ccc or disable
    if (
        type(observe_notification_tx) is not bool
        or observe_notification_tx
        and not disable
    ):
        raise ValueError("notification TX observation is opt-in for CCC disable only")
    maximum = 2700 if full else 300 if reject else 900
    if (
        scenario
        not in (
            "recovery-ten",
            "full-suite",
            "write-reject",
            "ccc-enable-reject",
            "ccc-disable-reject",
        )
        or platform not in ("android", "ios")
        or not device.strip()
        or reject
        and (device != device.strip() or type(timeout) not in (int, float))
        or not 0 < timeout <= maximum
    ):
        raise ValueError(
            f"explicit platform/device and timeout in (0, {maximum}] required"
        )
    if ccc:
        from tests.peripherals.nrf52840 import ccc_enable_reject
        from tools.react_native import (
            ccc_enable_reject_capture,
            ccc_enable_reject_ledger,
        )

    if disable:
        from tests.peripherals.nrf52840 import ccc_disable_reject
        from tools.react_native import (
            ccc_disable_reject_capture,
            ccc_disable_reject_ledger,
        )

    service = preflight_fixture(
        service_uuid=None,
        fixture_contract=fixture_contract,
        fixture_profile=fixture_profile,
    )
    if full:
        require_v2_coordination("rf-lab-full-attested")
    build = PROJECT_ROOT / f"tests/consumers/react-native-internal/{platform}/build"
    attempt = build / f"rf-{scenario}-{uuid.uuid4().hex}"
    attempt.mkdir(parents=True, mode=0o700)
    coordination = attempt / "coordination"
    coordination.mkdir(mode=0o700)
    log = attempt / f"{scenario}-runtime.log"
    install_json = attempt / "device-install.json" if platform == "ios" else None
    bundle: EvidenceBundle | None = None
    identity: tuple[int, int] | None = None
    completed = False
    primary: BaseException | None = None
    previous_handler = signal.getsignal(signal.SIGTERM)

    def interrupted(_signal: int, _frame: object) -> None:
        raise InterruptedError(f"{scenario} interrupted by SIGTERM")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        bundle = EvidenceBundle(
            attempt,
            platform=platform,
            device=device,
            project_root=PROJECT_ROOT,
            scenario=scenario,
        )
        if provider.PID_FILE.exists():
            raise RuntimeError("refusing to take over an existing nRF fixture")
        snapshot = bundle.prepare_artifact(artifact)
        if platform == "android":
            verify_install_identity(snapshot, attempt)
        else:
            require_ready_unlocked_device(device, cwd=PROJECT_ROOT)
        selected_port = provider._port(port)
        if selected_port is None:
            raise RuntimeError("nRF fixture port is not configured")
        plan = prepare_session(
            fixture_contract,
            fixture_profile,
            coordination / "fixture-session",
            port=selected_port,
            service_uuid=service,
        )
        require_multifunction(plan)
        if observe_notification_tx:
            from tests.peripherals.nrf52840.notification_observation_capture import (
                prepare_request,
            )

            prepare_request(coordination, plan)
        profile = load_session_profile(plan.contract, plan.profile_path)
        mode = MODE
        if full:
            from tests.peripherals.nrf52840 import full_suite
            from tests.peripherals.nrf52840.cancellation_control import (
                prepare_suite_controls,
            )

            prepare_suite_controls(coordination, plan)
            full_suite.prepare_control(coordination, plan)
            mode = full_suite.MODE
        elif reject:
            protocol = (
                ccc_disable_reject
                if disable
                else ccc_enable_reject
                if ccc
                else write_reject
            )
            protocol.prepare_control(coordination, plan)
            mode = protocol.MODE
        else:
            write_document(
                coordination / CONTROL_NAME,
                {"schema_version": 1, "run_id": plan.session_id, "cycles": 10},
            )
        startup = android_sdk.run_command(
            [
                sys.executable,
                "-m",
                "tests.peripherals.nrf52840.provider",
                "direct-start",
                "--port",
                selected_port,
                "--coordination-mode",
                mode,
                "--coordination-path",
                str(coordination),
                "--fixture-request",
                str(plan.request_path),
            ],
            timeout=provider.DEFAULT_READY_TIMEOUT + 15,
            check=True,
        )
        identity = fixture_identity_from_stdout(startup.stdout)

        def guard() -> None:
            guard_fixture(identity)
            verify_receipt(plan, fixture_identity=identity)

        guard()
        bundle.verify_artifact()
        if platform == "android":
            android_sdk.run_command(
                [
                    "pixi",
                    "run",
                    "-e",
                    "android",
                    "python",
                    "-m",
                    "tools.android.install_apk",
                    "--serial",
                    device,
                    "--device",
                    "physical",
                    "--apk",
                    str(snapshot),
                    "--log",
                    str(attempt / "apk-install.log"),
                ],
                timeout=300,
                check=True,
            )
            guard()
            android_sdk.run_command(
                android_sdk.adb_command(
                    device, "shell", "am", "force-stop", ANDROID_PACKAGE
                ),
                timeout=15,
                check=True,
            )
            android_sdk.run_command(
                android_sdk.adb_command(device, "logcat", "-c"),
                timeout=15,
                check=True,
            )
            android_sdk.grant_bluetooth_permissions(device)
        else:
            assert install_json is not None
            ios_sdk.run_command(
                [
                    "pixi",
                    "run",
                    "-e",
                    "apple-ci",
                    "xcrun",
                    "devicectl",
                    "device",
                    "install",
                    "app",
                    "--device",
                    device,
                    "--json-output",
                    str(install_json),
                    str(snapshot),
                ],
                timeout=120,
                check=True,
            )
        bundle.verify_artifact()
        guard()
        capture = recovery_ten_capture.capture
        capture_options = {}
        if full:
            capture = full_suite_capture.capture
            capture_options["fixture_identity"] = identity
        elif reject:
            capture = (
                ccc_disable_reject_capture.capture
                if disable
                else ccc_enable_reject_capture.capture
                if ccc
                else write_reject_capture.capture
            )
            capture_options["fixture_identity"] = identity
        text, cycles = capture(
            platform,
            device,
            log,
            coordination,
            plan.session_id,
            profile.service_uuid,
            profile.local_name,
            timeout=timeout,
            guard=guard,
            install_json=install_json,
            **capture_options,
        )
        guard()
        if full:
            ledger = full_suite_ledger.verify(
                text,
                log=log,
                platform=platform,
                run_id=plan.session_id,
                service_uuid=profile.service_uuid,
                fixture_request=plan.request_path,
                fixture_identity=identity,
                host_cycles=cycles,
            )
        elif reject:
            if cycles != ():
                raise RuntimeError(
                    f"{scenario} capture returned recovery cycle identities"
                )
            verify = (
                ccc_disable_reject_ledger.verify
                if disable
                else ccc_enable_reject_ledger.verify
                if ccc
                else write_reject_ledger.verify
            )
            ledger = verify(
                text,
                platform=platform,
                run_id=plan.session_id,
                service_uuid=profile.service_uuid,
                physical_path=coordination / protocol.PHYSICAL_NAME,
                fixture_request=plan.request_path,
                fixture_identity=identity,
            )
        else:
            ledger = recovery_ten_ledger.verify(
                text,
                platform=platform,
                run_id=plan.session_id,
                service_uuid=profile.service_uuid,
                physical_path=coordination / PHYSICAL_NAME,
                fixture_request=plan.request_path,
                fixture_identity=identity,
                host_cycles=cycles,
            )
        write_document(attempt / f"{scenario}-ledger.json", ledger)
        bundle.verify_artifact()
        completed = True
        return attempt / "evidence-bundle.json"
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)

            def stop_owned(owned_identity: tuple[int, int]) -> None:
                if provider._process_identity() != owned_identity:
                    raise RuntimeError("fixture identity changed; refusing cleanup")
                android_sdk.run_command(
                    [
                        sys.executable,
                        "-m",
                        "tests.peripherals.nrf52840.provider",
                        "stop-owned",
                        "--pid",
                        str(owned_identity[0]),
                        "--pgid",
                        str(owned_identity[1]),
                    ],
                    timeout=35,
                    check=True,
                )

            def report(ok: bool) -> None:
                print(
                    f"{platform} {scenario} {'completed' if ok else 'failed'}; evidence={attempt}",
                    flush=True,
                )

            finalize_rf_attempt(
                coordination=coordination,
                bundle=bundle,
                completed=completed,
                primary=primary,
                runtime_log=log,
                stop_owned=partial(stop_owned, identity)
                if identity is not None
                else None,
                report=report,
            )
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
