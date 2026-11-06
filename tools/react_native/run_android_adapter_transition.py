"""Run the attested Android OFF -> ON gate without flashing or retrying devices."""

from __future__ import annotations

import argparse
import hashlib
import re
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from tests.peripherals.nrf52840 import provider
from tests.peripherals.nrf52840.adapter_transition import (
    ADAPTER_MODES,
    CONTROL_NAME,
    MODE,
    PHYSICAL_NAME,
    adapter_mode,
)
from tests.peripherals.nrf52840.cancellation_control import (
    read_document,
    require_multifunction,
    write_document,
)
from tests.peripherals.nrf52840.direct_fixture import require_v2_coordination
from tests.peripherals.nrf52840.fixture_session import (
    _json,
    _read_bytes,
    load_session_profile,
    prepare_session,
    verify_receipt,
)
from tools.react_native import adapter_transition_capture as capture_owner
from tools.react_native import adapter_transition_ledger as ledger
from tools.react_native import android_sdk
from tools.react_native.android_rf_host import verify_install_identity
from tools.react_native.capture_io import MAX_BYTES
from tools.react_native.host_contract import ANDROID_PACKAGE
from tools.react_native.host_paths import PROJECT_ROOT
from tools.react_native.rf_evidence_bundle import EvidenceBundle, _read_small
from tools.react_native.rf_fixture import (
    fixture_identity_from_stdout,
    guard_fixture,
    preflight_fixture,
)

if TYPE_CHECKING:
    from tools.android.state_control import AndroidRadioController


def _new_controller(
    device: str, run_id: str, evidence_path: Path
) -> AndroidRadioController:
    """Construct the phone owner only for an actual runner invocation."""
    from tools.android.state_control import AndroidRadioController

    return AndroidRadioController(device, run_id, evidence_path)


def _confirm_permissions(device: str, attempt: Path) -> None:
    api_result = android_sdk.run_command(
        android_sdk.adb_command(device, "shell", "getprop", "ro.build.version.sdk"),
        timeout=5,
        check=True,
    )
    api = api_result.stdout.strip()
    if not api.isdecimal() or int(api) < 23:
        raise RuntimeError("adapter gate requires an Android runtime-permission API")
    required = (
        ("android.permission.BLUETOOTH_SCAN", "android.permission.BLUETOOTH_CONNECT")
        if int(api) >= 31
        else ("android.permission.ACCESS_FINE_LOCATION",)
    )
    result = android_sdk.run_command(
        android_sdk.adb_command(device, "shell", "dumpsys", "package", ANDROID_PACKAGE),
        timeout=5,
        check=True,
    )
    (attempt / "adapter-permissions.log").write_text(result.stdout, encoding="utf-8")
    for permission in required:
        states = re.findall(
            rf"(?m)^\s*{re.escape(permission)}:\s*granted=(true|false)(?:[,\s]|$)",
            result.stdout,
        )
        if states != ["true"]:
            raise RuntimeError(
                f"permission readback missing/ambiguous/denied: {permission}"
            )


def run(
    device: str,
    apk: Path,
    fixture_contract: Path,
    fixture_profile: Path,
    *,
    timeout: float = 180,
    mode: str = "startup-off",
) -> Path:
    """Hold the phone owner from initial ON through installation and final readback."""
    mode = adapter_mode(mode)
    capture_owner.validate_inputs(device, timeout)
    service = preflight_fixture(
        service_uuid=None,
        fixture_contract=fixture_contract,
        fixture_profile=fixture_profile,
    )
    require_v2_coordination("adapter-transition-attested")
    build = PROJECT_ROOT / "tests/consumers/react-native-internal/android/build"
    attempt = build / f"rf-adapter-transition-{uuid.uuid4().hex}"
    attempt.mkdir(parents=True, mode=0o700)
    coordination = attempt / "coordination"
    coordination.mkdir(mode=0o700)
    log = attempt / "adapter-transition-runtime.log"
    control_path = attempt / "adapter-state-control.json"
    bundle: EvidenceBundle | None = None
    controller: AndroidRadioController | None = None
    acquired = False
    identity: tuple[int, int] | None = None
    completed = False
    capture_returned = False
    primary: BaseException | None = None
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(_signal: int, _frame: object) -> None:
        raise InterruptedError("Android adapter transition interrupted by SIGTERM")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        bundle = EvidenceBundle(
            attempt,
            platform="android",
            device=device,
            project_root=PROJECT_ROOT,
            scenario="adapter-transition",
            adapter_mode=mode,
        )
        if provider.PID_FILE.exists():
            raise RuntimeError("refusing to take over an existing nRF fixture")
        snapshot = bundle.prepare_artifact(apk)
        verify_install_identity(snapshot, attempt)
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
            {"schema_version": 1, "run_id": plan.session_id, "adapter_mode": mode},
        )
        request_raw = _read_bytes(plan.request_path)
        controller = _new_controller(device, plan.session_id, control_path)
        controller.acquire()
        acquired = True
        phone_guard = controller.require_owner
        phone_guard()
        controller.initial()  # OFF is a rejected precondition, never auto-corrected.
        startup = android_sdk.run_command(
            [
                sys.executable,
                "-m",
                "tests.peripherals.nrf52840.provider",
                "direct-start",
                "--coordination-mode",
                MODE,
                "--coordination-path",
                str(coordination),
                "--fixture-request",
                str(plan.request_path),
            ],
            timeout=provider.DEFAULT_READY_TIMEOUT + 15,
            check=True,
        )
        identity = fixture_identity_from_stdout(startup.stdout)
        guard_fixture(identity)
        verify_receipt(plan, fixture_identity=identity)
        receipt_raw = _read_bytes(plan.receipt_path)

        def guard() -> None:
            guard_fixture(identity)
            verify_receipt(plan, fixture_identity=identity)
            if (
                _read_bytes(plan.request_path) != request_raw
                or _read_bytes(plan.receipt_path) != receipt_raw
            ):
                raise RuntimeError("live fixture request/receipt bytes changed")

        guard()
        bundle.verify_artifact()
        phone_guard()
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
        # Exactly one explicit replacement of the old known host, under the
        # acquired phone lock. Later failure cleanup may stop only our pinned PID.
        phone_guard()
        android_sdk.run_command(
            android_sdk.adb_command(
                device, "shell", "am", "force-stop", ANDROID_PACKAGE
            ),
            timeout=5,
            check=True,
        )
        phone_guard()
        android_sdk.run_command(
            android_sdk.adb_command(device, "logcat", "-c"),
            timeout=5,
            check=True,
        )
        phone_guard()
        android_sdk.grant_bluetooth_permissions(device)
        _confirm_permissions(device, attempt)
        bundle.verify_artifact()
        guard()
        phone_guard()
        if mode == "startup-off":
            controller.off()
        guard()
        phone_guard()
        write_document(
            coordination / "go",
            {"schema_version": 1, "run_id": plan.session_id, "adapter_mode": mode},
        )
        text = capture_owner.capture(
            device,
            log,
            coordination,
            plan.session_id,
            profile.service_uuid,
            profile.local_name,
            timeout=timeout,
            guard=guard,
            radio_on=controller.on,
            phone_guard=phone_guard,
            **(
                {"mode": mode, "radio_off": controller.off}
                if mode == "active-loss"
                else {}
            ),
        )
        capture_returned = True
        guard()
        runtime = ledger.verify_runtime(
            text, plan.session_id, profile.service_uuid, log
        )
        ledger.verify_runtime(
            _read_small(log.with_suffix(".raw.log"), MAX_BYTES).decode("utf-8"),
            plan.session_id,
            profile.service_uuid,
            log.with_suffix(".raw.log"),
        )
        ledger.verify_physical(
            read_document(coordination / PHYSICAL_NAME),
            request=_json(request_raw),
            receipt=_json(receipt_raw),
            receipt_sha256=hashlib.sha256(receipt_raw).hexdigest(),
            runtime=runtime,
        )
        write_document(attempt / "adapter-transition-ledger.json", runtime)
        bundle.verify_artifact()
        completed = True
        return attempt / "evidence-bundle.json"
    except BaseException as error:
        primary = error
        raise
    finally:
        errors: list[str] = []
        try:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if primary is not None:
                try:
                    (coordination / "abort").write_text(
                        "parent-failed\n", encoding="utf-8"
                    )
                except OSError as error:
                    errors.append(f"abort publication: {error}")
            if primary is not None and capture_returned:
                try:
                    capture_owner.abort_owned_app(
                        device, log, plan.session_id, phone_guard=phone_guard
                    )
                except Exception as error:  # noqa: BLE001 - do not mask checker failure
                    errors.append(f"owned app cleanup: {error}")
            # Restore/final-readback/journal/release precede fixture and bundle finish.
            if acquired:
                assert controller is not None
                try:
                    errors.extend(controller.finish(primary))
                    if completed and primary is None and not errors:
                        ledger.verify_state_control(
                            _json(_read_small(control_path, MAX_BYTES)),
                            plan.session_id,
                            device,
                        )
                except Exception as error:  # noqa: BLE001 - still stop owned fixture/finalize failure
                    errors.append(f"adapter control finalization: {error}")
            if identity is not None:
                try:
                    if provider._process_identity() != identity:
                        raise RuntimeError("fixture identity changed; refusing cleanup")
                    android_sdk.run_command(
                        [
                            sys.executable,
                            "-m",
                            "tests.peripherals.nrf52840.provider",
                            "stop-owned",
                            "--pid",
                            str(identity[0]),
                            "--pgid",
                            str(identity[1]),
                        ],
                        timeout=35,
                        check=True,
                    )
                except Exception as error:  # noqa: BLE001 - preserve primary and finish failed bundle
                    errors.append(f"fixture cleanup: {error}")
            if bundle is not None:
                try:
                    bundle.finish(
                        completed=completed and primary is None and not errors,
                        primary_error=primary,
                        cleanup_errors=errors,
                        runtime_log=log,
                    )
                except Exception as error:  # noqa: BLE001 - never replace primary
                    errors.append(f"bundle finalization: {error}")
            if errors:
                if primary is not None:
                    primary.add_note("; ".join(errors))
                else:
                    raise RuntimeError("; ".join(errors))
        finally:
            signal.signal(signal.SIGTERM, previous)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--fixture-contract", type=Path, required=True)
    parser.add_argument("--fixture-profile", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--mode", choices=ADAPTER_MODES, default="startup-off")
    args = parser.parse_args()
    try:
        manifest = run(
            args.device,
            args.apk,
            args.fixture_contract,
            args.fixture_profile,
            timeout=args.timeout,
            mode=args.mode,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"rf-adapter-transition:failed {error}", file=sys.stderr)
        return 2
    print(f"rf-adapter-transition:passed bundle={manifest} app=kept-open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
