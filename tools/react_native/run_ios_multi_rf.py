"""Run the bounded iOS RN two-nRF52840-DK RF gate."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import cast

from tests.peripherals.nrf52840 import multi_fixture, provider
from tests.peripherals.nrf52840.profile import load_profile
from tools.react_native.ios_device import (
    IosDevicePreconditionError,
    require_ready_unlocked_device,
)
from tools.react_native.rf_runner import (
    ProgressReporter,
    multi_rf_timeout_seconds,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOST_BUILD = PROJECT_ROOT / "tests/consumers/react-native-internal/ios/build"
APP = HOST_BUILD / "device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app"
DEFAULT_TIMEOUT_SECONDS: float | None = None
HEARTBEAT_SECONDS = 12.0


class IosMultiRfError(RuntimeError):
    """Raised when the bounded iOS multi-DK gate cannot complete safely."""


def _device_ready(udid: str) -> None:
    try:
        require_ready_unlocked_device(udid, cwd=PROJECT_ROOT)
    except IosDevicePreconditionError as error:
        raise IosMultiRfError(str(error)) from error


def _run(command: list[str], *, timeout: float) -> None:
    try:
        _ = subprocess.run(command, cwd=PROJECT_ROOT, check=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise IosMultiRfError(
            f"command timed out after {timeout:g}s: {command[0]}"
        ) from error
    except subprocess.CalledProcessError as error:
        raise IosMultiRfError(
            f"command failed with exit code {error.returncode}: {command}"
        ) from error


def run(
    udid: str,
    timeout_seconds: float | None = DEFAULT_TIMEOUT_SECONDS,
    subscription_mode: str = "notification",
    *,
    log_suffix: str = "",
    runtime_mode: str = "rf-multi",
    soak_cycles: int = 10,
) -> Path:
    if not udid.strip():
        raise IosMultiRfError("an iOS device identifier is required")
    try:
        effective_timeout = multi_rf_timeout_seconds(
            timeout_seconds,
            runtime_mode=runtime_mode,
            soak_cycles=soak_cycles,
        )
    except ValueError as error:
        raise IosMultiRfError(str(error)) from error
    if subscription_mode not in {"notification", "indication"}:
        raise IosMultiRfError(f"unsupported subscription mode: {subscription_mode}")
    if not APP.is_dir():
        raise IosMultiRfError(f"signed iOS RN host app is missing: {APP}")
    if provider.PID_FILE.exists():
        raise IosMultiRfError(
            f"refusing to run multi-DK gate while single-DK provider marker exists: {provider.PID_FILE}"
        )

    _device_ready(udid)
    devices = multi_fixture.load_devices()
    service_uuids = [load_profile(device.profile).service_uuid for device in devices]
    service_uuid_argument = ",".join(service_uuids)
    log_path = HOST_BUILD / f"device-{runtime_mode}{log_suffix}.log"
    fixture_root = HOST_BUILD / f"rf-multi-fixture-{uuid.uuid4().hex[:12]}"
    coordination_root = fixture_root / "coordination"
    coordination_paths = {
        service_uuid: coordination_root / device.name
        for service_uuid, device in zip(service_uuids, devices, strict=True)
    }
    coordination_map = fixture_root / "coordination-map.json"
    fixture_root.mkdir(parents=True, exist_ok=True)
    _ = coordination_map.write_text(
        json.dumps(
            {service: str(path) for service, path in coordination_paths.items()}
        ),
        encoding="utf-8",
    )
    log_path.unlink(missing_ok=True)
    HOST_BUILD.mkdir(parents=True, exist_ok=True)
    controller = multi_fixture.MultiFixtureController(devices, work_root=fixture_root)
    progress = ProgressReporter(
        runtime_mode,
        prefix="rn_ios_rf_multi",
        heartbeat_seconds=HEARTBEAT_SECONDS,
    )
    primary_error: BaseException | None = None
    try:
        progress.phase_started("fixture-start")
        controller.start(
            coordination_root=coordination_root,
            coordination_mode=(
                f"soak-{subscription_mode}"
                if runtime_mode == "rf-multi-soak"
                else subscription_mode
            ),
            coordination_cycles=soak_cycles if runtime_mode == "rf-multi-soak" else 1,
        )
        progress.phase_passed("fixture-start")

        xcrun = shutil.which("xcrun")
        if xcrun is None:
            raise IosMultiRfError("xcrun is required for the iOS device gate")
        progress.phase_started("install")
        _run(
            [
                xcrun,
                "devicectl",
                "device",
                "install",
                "app",
                "--device",
                udid,
                "--json-output",
                str(HOST_BUILD / "device-rf-multi-install.json"),
                str(APP),
            ],
            timeout=120,
        )
        progress.phase_passed("install")

        progress.phase_started("runtime")
        _run(
            [
                sys.executable,
                "-m",
                "tools.react_native.run_ios_device_runtime",
                udid,
                str(log_path),
                "--seconds",
                str(effective_timeout),
                "--rf-multi-soak-service-uuids"
                if runtime_mode == "rf-multi-soak"
                else "--rf-multi-service-uuids",
                service_uuid_argument,
                "--rf-multi-subscription-mode",
                subscription_mode,
                "--rf-multi-soak-cycles",
                str(soak_cycles),
                "--rf-multi-coordination-map",
                str(coordination_map),
            ],
            timeout=effective_timeout + 45,
        )
        progress.phase_passed("runtime")

        progress.phase_started("ledger")
        _run(
            [
                sys.executable,
                "-m",
                "tools.react_native.check_runtime_log",
                str(log_path),
                "--rf-multi-soak-service-uuids"
                if runtime_mode == "rf-multi-soak"
                else "--rf-multi-service-uuids",
                service_uuid_argument,
                "--rf-multi-subscription-mode",
                subscription_mode,
                "--rf-multi-soak-cycles",
                str(soak_cycles),
            ],
            timeout=30,
        )
        progress.phase_passed("ledger")
        return log_path
    except BaseException as error:
        primary_error = error
        progress.failed(f"{type(error).__name__}:{error}")
        if isinstance(error, multi_fixture.MultiFixtureError):
            raise IosMultiRfError(str(error)) from error
        raise
    finally:
        try:
            controller.stop()
        except (multi_fixture.MultiFixtureError, OSError) as error:
            print(f"ios-rf-multi:cleanup-failed:{error}", file=sys.stderr, flush=True)
            if primary_error is None:
                raise IosMultiRfError("iOS RN multi-DK cleanup failed") from error
        else:
            if primary_error is None:
                shutil.rmtree(fixture_root, ignore_errors=True)


def run_cycles(
    udid: str,
    cycles: int,
    timeout_seconds: float | None = DEFAULT_TIMEOUT_SECONDS,
    subscription_mode: str = "notification",
) -> Path:
    """Run bounded fresh-probe multi-DK cycles, stopping on the first failure."""
    if cycles < 1 or cycles > 20:
        raise IosMultiRfError("multi RF cycles must be between 1 and 20")
    last_log: Path | None = None
    for cycle in range(1, cycles + 1):
        print(f"ios-rf-multi:cycle={cycle}/{cycles}:started", flush=True)
        last_log = run(
            udid,
            timeout_seconds,
            subscription_mode,
            log_suffix=f"-cycle-{cycle}",
            runtime_mode="rf-multi",
            soak_cycles=1,
        )
        print(f"ios-rf-multi:cycle={cycle}/{cycles}:passed", flush=True)
    assert last_log is not None
    return last_log


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("udid", help="connected iOS device identifier")
    _ = parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    _ = parser.add_argument(
        "--subscription-mode",
        choices=("notification", "indication"),
        default="notification",
    )
    _ = parser.add_argument("--cycles", type=int, default=1)
    _ = parser.add_argument(
        "--runtime-mode", choices=("rf-multi", "rf-multi-soak"), default="rf-multi"
    )
    _ = parser.add_argument("--soak-cycles", type=int, default=10)
    arguments = parser.parse_args()
    try:
        runtime_mode = cast(str, arguments.runtime_mode)
        if runtime_mode == "rf-multi-soak":
            log_path = run(
                cast(str, arguments.udid),
                cast(float | None, arguments.timeout),
                cast(str, arguments.subscription_mode),
                runtime_mode="rf-multi-soak",
                soak_cycles=cast(int, arguments.soak_cycles),
            )
        else:
            log_path = run_cycles(
                cast(str, arguments.udid),
                cast(int, arguments.cycles),
                cast(float | None, arguments.timeout),
                cast(str, arguments.subscription_mode),
            )
    except IosMultiRfError as error:
        print(f"iOS multi RF error: {error}", file=sys.stderr)
        return 2
    print(f"ios-rf-multi:passed log={log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
