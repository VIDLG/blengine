"""Run Native iOS HIL Flow 12 without RN/Nitro or synthetic BLE events."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840 import ccc_disable_reject, provider
from tests.peripherals.nrf52840.cancellation_control import write_document
from tests.peripherals.nrf52840.fixture_session import (
    load_session_profile,
    prepare_session,
    verify_receipt,
)
from tests.peripherals.nrf52840.profile import load_profile
from tools.apple import test_ios_runtime
from tools.apple.rf_fixture import RfFixtureConfig
from tools.local_config import resolve_local_value
from tools.react_native.rf_fixture import fixture_identity_from_stdout, guard_fixture

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORK_ROOT = PROJECT_ROOT / ".work" / "apple-ios-native-hil"


def _profile_config(profile_path: Path) -> RfFixtureConfig:
    profile = load_profile(profile_path)
    roles = {item.role: item for item in profile.characteristics}
    return RfFixtureConfig(
        profileId=profile.profile_id,
        serviceUuid=profile.service_uuid,
        hasIndicationOnly="indications" in roles,
        initialValueHex=roles["read-write"].initial_value.hex(),
        notificationValueHex=profile.notification_value.hex(),
        writeWithResponseHex=profile.writeWithResponseHex,
        writeWithoutResponseHex=profile.writeWithoutResponseHex,
    )


def _provider_start(coordination: Path, plan: Any, port: str) -> tuple[int, int]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.peripherals.nrf52840.provider",
            "direct-start",
            "--port",
            port,
            "--coordination-mode",
            ccc_disable_reject.MODE,
            "--coordination-path",
            str(coordination),
            "--fixture-request",
            str(plan.request_path),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=provider.DEFAULT_READY_TIMEOUT + 30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "nRF Flow 12 provider failed to start:\n"
            + result.stdout[-4000:]
            + result.stderr[-4000:]
        )
    return fixture_identity_from_stdout(result.stdout)


def _provider_stop(identity: tuple[int, int]) -> None:
    if provider._process_identity() != identity:
        raise RuntimeError("refusing to stop a foreign nRF provider")
    result = subprocess.run(
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
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=35,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "nRF Flow 12 provider cleanup failed:\n"
            + result.stdout[-4000:]
            + result.stderr[-4000:]
        )


def _post_notification(
    xcrun: str,
    device: test_ios_runtime.IOSDevice,
    phase: str,
    control: ccc_disable_reject.Control,
    output: Path,
) -> None:
    _ = test_ios_runtime._devicectl_json(
        xcrun,
        [
            "device",
            "notification",
            "post",
            "--device",
            device.core_device_id,
            "--name",
            control.notification(phase),
            "--timeout",
            "10",
        ],
        output,
        timeout=30,
        success_marker=f"apple_ios_native_hil_notification={phase}",
    )


def _native_process_identity(
    xcrun: str,
    device: test_ios_runtime.IOSDevice,
    output: Path,
    bundle_id: str,
) -> dict[str, Any]:
    document = test_ios_runtime._devicectl_json(
        xcrun,
        [
            "device",
            "info",
            "processes",
            "--device",
            device.core_device_id,
            "--quiet",
        ],
        output,
        timeout=30,
    )
    result = document.get("result", {})
    rows = result.get("runningProcesses", [])
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("executable"), str)
        and row["executable"].endswith("/BleHubAppleIOSProbe")
    ]
    if len(matches) != 1 or type(matches[0].get("processIdentifier")) is not int:
        raise RuntimeError("could not pin the Native HIL process identity")
    return {
        "pid": matches[0]["processIdentifier"],
        "bundle_id": bundle_id,
        "executable": matches[0]["executable"],
        "device_identifier": result.get("deviceIdentifier"),
    }


def run(
    contract: Path,
    profile: Path,
    *,
    port: str | None = None,
    device: str | None = None,
    timeout: float = 300,
) -> Path:
    if not 0 < timeout <= 300:
        raise ValueError("Native HIL Flow 12 timeout must be in (0, 300]")
    selected_port = (
        port
        or resolve_local_value(
            "peripheral_nrf52840", "port", "BLEHUB_NRF52840_PORT", None
        ).value
    )
    if not selected_port:
        raise RuntimeError("Flow 12 requires an explicit configured nRF52840 port")

    xcodegen, xcodebuild, xcrun, ios_device, team, bundle_id = (
        test_ios_runtime._environment()
    )
    if device is not None and device not in {
        ios_device.core_device_id,
        ios_device.udid,
        ios_device.name,
    }:
        raise RuntimeError("requested Native HIL iOS device does not match discovery")
    test_ios_runtime._print_environment(ios_device, bundle_id)
    test_ios_runtime._generate_project(xcodegen)
    test_ios_runtime._build_app(xcodebuild, ios_device, team, bundle_id)
    test_ios_runtime._install_app(xcrun, ios_device)

    run_id = str(uuid.uuid4())
    attempt = WORK_ROOT / run_id
    coordination = attempt / "coordination"
    fixture_session = coordination / "fixture-session"
    attempt.mkdir(parents=True, mode=0o700, exist_ok=False)
    coordination.mkdir(mode=0o700)
    plan = prepare_session(
        contract,
        profile,
        fixture_session,
        port=selected_port,
        service_uuid=load_profile(profile).service_uuid,
    )
    protocol = ccc_disable_reject
    run_id = plan.session_id
    control = protocol.prepare_control(coordination, plan)
    fixture_config = _profile_config(profile)
    profile_plan = load_session_profile(plan.contract, plan.profile_path)
    identity: tuple[int, int] | None = None
    primary: BaseException | None = None
    failed_result: str | None = None
    try:
        identity = _provider_start(coordination, plan, selected_port)
        guard_fixture(identity)
        verify_receipt(plan, fixture_identity=identity)
        control_json = json.dumps(asdict(control), separators=(",", ":"))
        test_ios_runtime._launch_app(
            xcrun,
            ios_device,
            bundle_id,
            run_id,
            rf_service_uuid=profile_plan.service_uuid,
            rf_scenario="ccc-disable-reject",
            fixture=fixture_config,
            extra_arguments=[
                "--rf-ccc-disable-reject-control",
                control_json,
            ],
        )
        native_identity = _native_process_identity(
            xcrun, ios_device, attempt / "native-processes.json", bundle_id
        )
        last_runtime = ""
        sent: set[str] = set()
        relay = ccc_disable_reject.CccDisableRejectRelay(coordination)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            guard_fixture(identity)
            runtime = test_ios_runtime._try_pull_result(
                xcrun,
                ios_device,
                bundle_id,
                f"Documents/blehub-rf-{run_id}/runtime.log",
            )
            if runtime is not None and runtime != last_runtime:
                relay.observe(runtime)
                last_runtime = runtime
            phase = relay.pending()
            if phase is not None and phase not in sent:
                relay.forwarded(phase)
                _post_notification(
                    xcrun,
                    ios_device,
                    phase,
                    control,
                    attempt / f"notification-{phase}.json",
                )
                sent.add(phase)
            result = test_ios_runtime._try_pull_result(
                xcrun,
                ios_device,
                bundle_id,
                f"Documents/blehub-rf-{run_id}/result.txt",
            )
            if result is not None:
                if "apple_ios_corebluetooth_rf=passed" in result.splitlines():
                    break
                if "apple_ios_corebluetooth_rf=failed" in result.splitlines():
                    failed_result = result
                    break
            time.sleep(0.2)
        else:
            raise TimeoutError("timed out coordinating Native HIL Flow 12")

        if failed_result is not None:
            runtime_path = coordination / "runtime.log"
            runtime = (
                runtime_path.read_text(encoding="utf-8")
                if runtime_path.is_file()
                else ""
            )
            native_identity = _native_process_identity(
                xcrun, ios_device, attempt / "native-processes-failed.json", bundle_id
            )
            classification = (
                "ios-corebluetooth-callback-missing"
                if "CCC value e1" in failed_result
                else "native-hil-flow12-failed"
            )
            try:
                ccc_disable_reject.runtime_facts(
                    runtime, run_id=run_id, platform="ios", complete=False
                )
            except (ValueError, RuntimeError) as error:
                classification = f"native-hil-runtime-invalid-after-failure: {error}"
            native_identity["boot_id"] = (
                next(
                    line.split("=", 1)[1]
                    for line in failed_result.splitlines()
                    if line.startswith("apple_ios_native_hil_boot_id=")
                )
                if any(
                    line.startswith("apple_ios_native_hil_boot_id=")
                    for line in failed_result.splitlines()
                )
                else None
            )
            write_document(attempt / "native-hil-identity.json", native_identity)
            write_document(
                attempt / "native-hil-result.json",
                {
                    "scenario": protocol.SCENARIO,
                    "platform": "ios",
                    "run_id": run_id,
                    "control": asdict(control),
                    "fixture_identity": {"pid": identity[0], "pgid": identity[1]},
                    "classification": classification,
                    "result": failed_result,
                    "runtime_log": str(coordination / "runtime.log"),
                    "physical": str(coordination / protocol.PHYSICAL_NAME),
                },
            )
            raise RuntimeError(
                "Native HIL Flow 12 classified as "
                + classification
                + "; evidence="
                + str(attempt / "native-hil-result.json")
            )

        result = test_ios_runtime._wait_for_runtime_result(
            xcrun,
            ios_device,
            bundle_id,
            run_id,
            f"Documents/blehub-rf-{run_id}/result.txt",
        )
        runtime = (coordination / "runtime.log").read_text(encoding="utf-8")
        ledger = __import__(
            "tools.react_native.ccc_disable_reject_ledger",
            fromlist=["verify"],
        ).verify(
            runtime,
            platform="ios",
            run_id=run_id,
            service_uuid=profile_plan.service_uuid,
            physical_path=coordination / protocol.PHYSICAL_NAME,
            fixture_request=plan.request_path,
            fixture_identity=identity,
        )
        write_document(attempt / "ccc-disable-reject-ledger.json", ledger)
        native_identity["boot_id"] = next(
            line.split("=", 1)[1]
            for line in result.splitlines()
            if line.startswith("apple_ios_native_hil_boot_id=")
        )
        write_document(attempt / "native-hil-identity.json", native_identity)
        write_document(
            attempt / "native-hil-result.json",
            {
                "scenario": protocol.SCENARIO,
                "platform": "ios",
                "run_id": run_id,
                "control": asdict(control),
                "fixture_identity": {"pid": identity[0], "pgid": identity[1]},
                "result": result,
                "runtime_log": str(coordination / "runtime.log"),
                "ledger": str(attempt / "ccc-disable-reject-ledger.json"),
            },
        )
        return attempt / "native-hil-result.json"
    except BaseException as error:
        primary = error
        (attempt / "abort").write_text(
            f"{type(error).__name__}: {error}\n", encoding="utf-8"
        )
        raise
    finally:
        if identity is not None:
            try:
                _provider_stop(identity)
            except BaseException as cleanup:
                if primary is not None:
                    primary.add_note(f"provider cleanup failed: {cleanup}")
                else:
                    raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-contract", type=Path, required=True)
    parser.add_argument("--fixture-profile", type=Path, required=True)
    parser.add_argument("--port")
    parser.add_argument("--device")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    try:
        result = run(
            args.fixture_contract,
            args.fixture_profile,
            port=args.port,
            device=args.device,
            timeout=args.timeout,
        )
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"native-ios-hil-ccc-disable-reject:failed {error}", file=sys.stderr)
        return 2
    print(f"native-ios-hil-ccc-disable-reject:passed result={result} app=kept-open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
