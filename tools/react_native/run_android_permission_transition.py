"""Attested Android permission-transition gate; no build, flash, retry or radio toggle."""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840 import permission_transition as wire
from tests.peripherals.nrf52840 import provider
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
from tools.android.permission_control import AndroidPermissionController
from tools.android.run_instrumentation import has_root
from tools.android.state_control import AndroidRadioController
from tools.android_toolchain import adb_executable
from tools.react_native import android_sdk
from tools.react_native import permission_transition_capture as capture_owner
from tools.react_native import permission_transition_ledger as ledger
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


def _finalize(
    *,
    permission: AndroidPermissionController | None,
    radio: AndroidRadioController | None,
    acquired: bool,
    capture: capture_owner.PermissionCapture | None,
    identity: tuple[int, int] | None,
    coordination: Path,
    bundle: EvidenceBundle | None,
    completed: bool,
    primary: BaseException | None,
    log: Path,
) -> None:
    """Restore permissions, recover only our app, final ON, release, fixture, bundle.

    The generic RF finalizer owns only fixture/bundle and cannot express errors
    accumulated before phone release; aggregate here instead of altering it.
    """
    errors: list[str] = []
    if primary is not None:
        try:
            (coordination / "abort").write_text("parent-failed\n", encoding="utf-8")
        except OSError as error:
            errors.append(f"abort publication: {error}")
    if permission is not None:
        try:
            errors.extend(permission.finish(primary))
            if completed and primary is None and not errors:
                ledger.verify_permission_control(
                    _json(_read_bytes(permission.path)),
                    permission.run_id,
                    permission.device,
                )
        except Exception as error:  # noqa: BLE001 - later cleanup is still required
            errors.append(f"permission restoration: {error}")
    if (primary is not None or errors) and capture is not None:
        try:
            capture.stop_owned_app()
        except Exception as error:  # noqa: BLE001 - never replace the gate failure
            errors.append(f"owned app cleanup: {error}")
    if acquired:
        assert radio is not None
        try:
            phone_failure = primary or (
                RuntimeError("; ".join(errors)) if errors else None
            )
            errors.extend(
                radio.finish_observation(
                    phone_failure,
                    on_failure=capture.stop_owned_app if capture is not None else None,
                )
            )
            if completed and primary is None and not errors:
                ledger.verify_state_control(
                    _json(_read_bytes(radio.path)), radio.run_id, radio.device
                )
        except Exception as error:  # noqa: BLE001 - still finish fixture/bundle
            errors.append(f"radio finalization: {error}")
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
        except Exception as error:  # noqa: BLE001 - preserve primary and bundle failure
            errors.append(f"fixture cleanup: {error}")
    if bundle is not None:
        try:
            bundle.finish(
                completed=completed and primary is None and not errors,
                primary_error=primary,
                cleanup_errors=errors,
                runtime_log=log,
            )
        except Exception as error:  # noqa: BLE001 - bundle failure cannot mask primary
            errors.append(f"bundle finalization: {error}")
    if errors:
        if primary is not None:
            primary.add_note("; ".join(errors))
        else:
            raise RuntimeError("; ".join(errors))


def run(
    device: str,
    apk: Path,
    fixture_contract: Path,
    fixture_profile: Path,
    *,
    timeout: float = 180,
) -> Path:
    """Install frozen candidate and run three phases under one shared phone lease."""
    capture_owner.validate_inputs(device, timeout)
    service = preflight_fixture(
        service_uuid=None,
        fixture_contract=fixture_contract,
        fixture_profile=fixture_profile,
    )
    require_v2_coordination("permission-transition")
    attempt = (
        PROJECT_ROOT
        / "tests/consumers/react-native-internal/android/build"
        / f"rf-permission-transition-{uuid.uuid4().hex}"
    )
    attempt.mkdir(parents=True, mode=0o700)
    coordination = attempt / "coordination"
    coordination.mkdir(mode=0o700)
    log = attempt / wire.RUNTIME_NAME
    bundle = None
    radio = None
    permission = None
    capture = None
    identity = None
    acquired = completed = False
    primary: BaseException | None = None
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(_signal: int, _frame: object) -> None:
        raise InterruptedError("Android permission transition interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        bundle = EvidenceBundle(
            attempt,
            platform="android",
            device=device,
            project_root=PROJECT_ROOT,
            scenario=wire.MODE,
        )
        if provider.PID_FILE.exists():
            raise RuntimeError("refusing to take over an existing nRF fixture")
        snapshot = bundle.prepare_artifact(apk)
        verify_install_identity(snapshot, attempt)
        port = provider._port()
        if port is None:
            raise RuntimeError("nRF fixture port not configured")
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
            coordination / wire.CONTROL_NAME,
            {"schema_version": 1, "run_id": plan.session_id},
        )
        request_raw = _read_bytes(plan.request_path)
        radio = AndroidRadioController(
            device, plan.session_id, attempt / "permission-radio-control.json"
        )
        radio.acquire()
        acquired = True
        radio.initial()
        startup = android_sdk.run_command(
            [
                sys.executable,
                "-m",
                "tests.peripherals.nrf52840.provider",
                "direct-start",
                "--coordination-mode",
                wire.MODE,
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
            radio.require_owner()
            guard_fixture(identity)
            verify_receipt(plan, fixture_identity=identity)
            if (
                _read_bytes(plan.request_path) != request_raw
                or _read_bytes(plan.receipt_path) != receipt_raw
            ):
                raise RuntimeError("live fixture request/receipt bytes changed")

        guard()
        bundle.verify_artifact()
        radio.require_owner()
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
        # One setup replacement is permitted only after the frozen package/launch
        # manifest and phone/fixture ownership are known. No later arbitrary stop.
        radio.require_owner()
        replacement: dict[str, Any] = {
            "schema_version": 1,
            "run_id": plan.session_id,
            "package": ANDROID_PACKAGE,
            "method": "replace-old-known-host-under-phone-lease",
            "status": "started",
        }
        write_document(attempt / "permission-setup-replacement.json", replacement)
        result = android_sdk.run_command(
            android_sdk.adb_command(
                device, "shell", "am", "force-stop", ANDROID_PACKAGE
            ),
            timeout=5,
            check=True,
        )
        replacement.update(status="completed", stdout=result.stdout)
        write_document(attempt / "permission-setup-replacement.json", replacement)
        radio.require_owner()
        android_sdk.run_command(
            android_sdk.adb_command(device, "logcat", "-c"), timeout=5, check=True
        )
        permission = AndroidPermissionController(
            device,
            plan.session_id,
            attempt / "permission-state-control.json",
            phone_guard=radio.require_owner,
            use_root=has_root(adb_executable(), device),
        )
        permission.initial()  # No ordinary RF launcher/grant path is called.
        bundle.verify_artifact()
        guard()
        capture = capture_owner.PermissionCapture(
            device,
            log,
            coordination,
            plan.session_id,
            profile.service_uuid,
            profile.local_name,
            timeout=timeout,
            guard=guard,
            permission=permission,
            phone_guard=radio.require_owner,
        )
        text = capture.run()
        guard()
        runtime = ledger.verify_runtime(
            text,
            plan.session_id,
            profile.service_uuid,
            log.name,
            capture_result=capture.result,
            documents=capture.documents,
            receipt_sha256=ledger.digest(receipt_raw),
            death_proof=_read_bytes(attempt / capture.result["os_death_proof"]),
            os_raw=_read_bytes(attempt / capture.result["os_raw_proof"]),
            local_name=profile.local_name,
        )
        raw = attempt / capture_owner.RAW_NAME
        ledger.verify_runtime(
            _read_small(raw, MAX_BYTES).decode(),
            plan.session_id,
            profile.service_uuid,
            raw.name,
            capture_result=capture.result,
            documents=capture.documents,
            receipt_sha256=ledger.digest(receipt_raw),
            death_proof=_read_bytes(attempt / capture.result["os_death_proof"]),
            os_raw=_read_bytes(attempt / capture.result["os_raw_proof"]),
            local_name=profile.local_name,
        )
        ledger.verify_physical(
            read_document(coordination / wire.PHYSICAL_NAME),
            request=_json(request_raw),
            receipt=_json(receipt_raw),
            receipt_sha256=ledger.digest(receipt_raw),
            runtime=runtime,
            documents=capture.documents,
        )
        write_document(attempt / "permission-transition-ledger.json", runtime)
        bundle.verify_artifact()
        completed = True
        return attempt / "evidence-bundle.json"
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            _finalize(
                permission=permission,
                radio=radio,
                acquired=acquired,
                capture=capture,
                identity=identity,
                coordination=coordination,
                bundle=bundle,
                completed=completed,
                primary=primary,
                log=log,
            )
        finally:
            signal.signal(signal.SIGTERM, previous)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--fixture-contract", type=Path, required=True)
    parser.add_argument("--fixture-profile", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    try:
        manifest = run(
            args.device,
            args.apk,
            args.fixture_contract,
            args.fixture_profile,
            timeout=args.timeout,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"rf-permission-transition:failed {error}", file=sys.stderr)
        return 2
    print(f"rf-permission-transition:passed bundle={manifest} app=kept-open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
