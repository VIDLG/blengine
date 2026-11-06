"""Live-attested pending ATT read across two RN engines in one mobile process.

The provider owns the only BTP lease/session. This controller never flashes,
resets GATT, or infers native entry from dispatch acceptance.
"""

from __future__ import annotations

import re
import signal
import sys
import uuid
from functools import partial
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840 import provider
from tests.peripherals.nrf52840.cancellation_control import (
    require_multifunction,
    write_document,
)
from tests.peripherals.nrf52840.fixture_session import (
    load_session_profile,
    prepare_session,
    verify_receipt,
)
from tests.peripherals.nrf52840.pending_reload import CONTROL_NAME, PHYSICAL_NAME
from tools.react_native import (
    android_pending_reload_capture,
    android_sdk,
    ios_sdk,
    pending_reload_ledger,
)
from tools.react_native.android_rf_host import verify_install_identity
from tools.react_native.host_contract import ANDROID_ACTIVITY, ANDROID_PACKAGE
from tools.react_native.host_paths import PROJECT_ROOT
from tools.react_native.rf_attempt_finalizer import finalize_rf_attempt
from tools.react_native.rf_evidence_bundle import EvidenceBundle
from tools.react_native.rf_fixture import (
    fixture_identity_from_stdout,
    guard_fixture,
    preflight_fixture,
)

DEFAULT_TIMEOUT = 180.0


def run(
    platform: str,
    device: str,
    artifact: Path,
    *,
    fixture_contract: Path,
    fixture_profile: Path,
    timeout: float = DEFAULT_TIMEOUT,
) -> Path:
    if (
        platform not in ("android", "ios")
        or not device.strip()
        or type(timeout) not in (int, float)
        or not 0 < timeout <= 300
    ):
        raise ValueError("explicit device and host timeout in (0, 300] required")
    service = preflight_fixture(
        service_uuid=None,
        fixture_contract=fixture_contract,
        fixture_profile=fixture_profile,
    )
    attempt = (
        PROJECT_ROOT
        / f"tests/consumers/react-native-internal/{platform}/build"
        / f"rf-pending-read-reload-{uuid.uuid4().hex}"
    )
    attempt.mkdir(parents=True, mode=0o700)
    coordination = attempt / "coordination"
    coordination.mkdir(mode=0o700)
    log = attempt / "pending-read-reload-runtime.log"
    bundle: EvidenceBundle | None = None
    identity: tuple[int, int] | None = None
    completed = False
    primary: BaseException | None = None
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupted(_signal: int, _frame: object) -> None:
        raise InterruptedError("pending reload interrupted by SIGTERM")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        bundle = EvidenceBundle(
            attempt,
            platform=platform,
            device=device,
            project_root=PROJECT_ROOT,
            scenario="pending-read-reload",
        )
        if provider.PID_FILE.exists():
            raise RuntimeError("refusing to take over an existing nRF fixture")
        snapshot = bundle.prepare_artifact(artifact)
        if platform == "android":
            verify_install_identity(snapshot, attempt)
        else:
            import plistlib

            from tools.react_native.host_contract import SCHEME
            from tools.react_native.ios_device import require_ready_unlocked_device

            if (
                plistlib.loads((snapshot / "Info.plist").read_bytes()).get(
                    "CFBundleExecutable"
                )
                != SCHEME
            ):
                raise RuntimeError(
                    "iOS pending artifact executable does not match the native host"
                )
            require_ready_unlocked_device(device, cwd=PROJECT_ROOT)
        port = provider._port()
        if port is None:
            raise RuntimeError("nRF fixture port is not configured")
        plan = prepare_session(
            fixture_contract,
            fixture_profile,
            coordination / "fixture-session",
            port=port,
            service_uuid=service,
        )
        require_multifunction(plan)
        profile = load_session_profile(plan.contract, plan.profile_path)
        write_document(
            coordination / CONTROL_NAME,
            {
                "schema_version": 1,
                "run_id": plan.session_id,
                **({"platform": "ios"} if platform == "ios" else {}),
            },
        )
        start = android_sdk.run_command(
            [
                sys.executable,
                "-m",
                "tests.peripherals.nrf52840.provider",
                "direct-start",
                "--coordination-path",
                str(coordination),
                "--coordination-mode",
                f"{platform}-pending-reload",
                "--fixture-request",
                str(plan.request_path),
            ],
            timeout=provider.DEFAULT_READY_TIMEOUT + 15,
            check=True,
        )
        identity = fixture_identity_from_stdout(start.stdout)

        def guard() -> None:
            guard_fixture(identity)
            verify_receipt(plan, fixture_identity=identity)

        guard()
        bundle.verify_artifact()
        if platform == "ios":
            from tools.react_native import ios_pending_reload_capture

            install_json = attempt / "device-install.json"
            ios_sdk.run_command(
                [
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
            guard()
            bundle.verify_artifact()
            text, actions = ios_pending_reload_capture.capture(
                device,
                log,
                coordination,
                run_id=plan.session_id,
                service_uuid=profile.service_uuid,
                local_name=profile.local_name,
                seconds=timeout,
                guard=guard,
                install_json=install_json,
            )
        else:
            text, actions = _install_launch_android(
                device,
                snapshot,
                attempt,
                profile,
                plan.session_id,
                log,
                coordination,
                timeout,
                guard,
                bundle,
            )
        guard()
        ledger_options = {"platform": "ios"} if platform == "ios" else {}
        ledger = pending_reload_ledger.verify(
            text,
            run_id=plan.session_id,
            service_uuid=profile.service_uuid,
            physical_path=coordination / PHYSICAL_NAME,
            fixture_request=plan.request_path,
            fixture_identity=identity,
            host_actions=actions,
            **ledger_options,
        )
        write_document(attempt / "pending-reload-ledger.json", ledger)
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
                    f"{platform} pending reload {'completed' if ok else 'failed'}; evidence={attempt}",
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
            signal.signal(signal.SIGTERM, previous_sigterm)


def _install_launch_android(
    serial: str,
    snapshot: Path,
    attempt: Path,
    profile: Any,
    run_id: str,
    log: Path,
    coordination: Path,
    timeout: float,
    guard: Any,
    bundle: EvidenceBundle,
) -> tuple[str, tuple[str, ...]]:
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
            serial,
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
        android_sdk.adb_command(serial, "shell", "am", "force-stop", ANDROID_PACKAGE),
        timeout=15,
        check=True,
    )
    android_sdk.run_command(
        android_sdk.adb_command(serial, "logcat", "-c"),
        timeout=15,
        check=True,
    )
    android_sdk.grant_bluetooth_permissions(serial)
    bundle.verify_artifact()
    guard()
    launch = android_sdk.run_command(
        android_sdk.adb_command(
            serial,
            "shell",
            "am",
            "start",
            "-n",
            f"{ANDROID_PACKAGE}/{ANDROID_ACTIVITY}",
            "--es",
            "blehub.runtimeMode",
            "rf-pending-read-reload",
            "--es",
            "blehub.rfServiceUuid",
            profile.service_uuid,
            "--es",
            "blehub.rfExpectedLocalName",
            profile.local_name,
            "--es",
            "blehub.rfPendingReadRunId",
            run_id,
            "--es",
            "blehub.rfReloadNotificationName",
            f"dev.blehub.rn.reload.{run_id}",
        ),
        timeout=15,
        check=True,
    )
    if re.search(r"(?:Error:|Exception|Activity not started)", launch.stdout):
        raise RuntimeError(f"pending reload launch failed: {launch.stdout[-1000:]}")
    return android_pending_reload_capture.capture(
        serial,
        log,
        coordination,
        run_id=run_id,
        seconds=timeout,
        guard=guard,
    )
