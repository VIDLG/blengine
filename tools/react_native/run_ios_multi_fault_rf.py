"""Run the bounded iOS RN multi-DK fault-isolation/reconnect gate."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import cast

from tests.peripherals.nrf52840 import multi_fixture, provider
from tests.peripherals.nrf52840.profile import load_profile
from tools.react_native.ios_device import (
    IosDevicePreconditionError,
    require_ready_unlocked_device,
)
from tools.react_native.rf_runner import ProgressReporter

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOST_BUILD = PROJECT_ROOT / "tests/consumers/react-native-internal/ios/build"
APP = HOST_BUILD / "device-derived/Build/Products/Debug-iphoneos/BleHubNitroHost.app"
DEFAULT_TIMEOUT_SECONDS = 300
HEARTBEAT_SECONDS = 12.0
READY_MARKER = "rf-multi-fault:ready-for-a-fault:"
RESTART_MARKER = "rf-multi-fault:awaiting-a-restart"
PASS_MARKER = "rf-multi-fault:passed:"


class IosMultiFaultRfError(RuntimeError):
    """Raised when the bounded iOS multi-DK fault gate cannot complete safely."""


def _device_ready(udid: str) -> None:
    try:
        require_ready_unlocked_device(udid, cwd=PROJECT_ROOT)
    except IosDevicePreconditionError as error:
        raise IosMultiFaultRfError(str(error)) from error


def _run(command: list[str], *, timeout: float) -> None:
    try:
        _ = subprocess.run(command, cwd=PROJECT_ROOT, check=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise IosMultiFaultRfError(
            f"command timed out after {timeout:g}s: {command[0]}"
        ) from error
    except subprocess.CalledProcessError as error:
        raise IosMultiFaultRfError(
            f"command failed with exit code {error.returncode}: {command}"
        ) from error


def _wait_for_runtime(
    log_path: Path,
    service_uuids: str,
    controller: multi_fixture.MultiFixtureController,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    progress = ProgressReporter(
        "rf-multi-fault",
        prefix="rn_ios_rf_multi_fault",
        heartbeat_seconds=HEARTBEAT_SECONDS,
    )
    phase = "runtime"
    fault_triggered = False
    restarted = False
    log = ""
    progress.phase_started(phase)
    while time.monotonic() < deadline:
        if log_path.is_file():
            log = log_path.read_text(encoding="utf-8", errors="replace")
        if "rf-multi-fault:failed:" in log:
            reason = next(
                line for line in log.splitlines() if "rf-multi-fault:failed:" in line
            )
            progress.failed(f"runtime:{reason}")
            raise IosMultiFaultRfError(
                f"iOS RN multi fault RF failed: {reason}; see {log_path}"
            )
        if not fault_triggered and READY_MARKER in log:
            progress.phase_passed("two-connections")
            progress.phase_started("fault-dk-a")
            controller.stop_device("dk_a")
            fault_triggered = True
            progress.phase_passed("fault-dk-a")
            progress.phase_started("awaiting-reconnect")
        if fault_triggered and not restarted and RESTART_MARKER in log:
            controller.restart_device("dk_a")
            restarted = True
            progress.phase_passed("awaiting-reconnect")
            progress.phase_started("reconnect")
        if PASS_MARKER in log:
            if not restarted:
                raise IosMultiFaultRfError(
                    "fault gate reported pass before DK-A restart"
                )
            progress.phase_passed("reconnect", marker=PASS_MARKER)
            return
        progress.waiting(
            f"phase:{phase}", remaining=f"{max(0, int(deadline - time.monotonic()))}s"
        )
        _ = time.sleep(1.0)
    raise IosMultiFaultRfError(
        f"iOS RN multi fault marker timed out; serviceUuids={service_uuids}; see {log_path}"
    )


def run(udid: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> Path:
    if not udid.strip():
        raise IosMultiFaultRfError("an iOS device identifier is required")
    if timeout_seconds <= 0:
        raise IosMultiFaultRfError("multi fault RF timeout must be positive")
    if not APP.is_dir():
        raise IosMultiFaultRfError(f"signed iOS RN host app is missing: {APP}")
    if provider.PID_FILE.exists():
        raise IosMultiFaultRfError(
            f"refusing to run while single-DK provider marker exists: {provider.PID_FILE}"
        )
    _device_ready(udid)
    devices = multi_fixture.load_devices()
    service_uuids = ",".join(
        load_profile(device.profile).service_uuid for device in devices
    )
    log_path = HOST_BUILD / "device-rf-multi-fault.log"
    fixture_root = HOST_BUILD / f"rf-multi-fault-fixture-{uuid.uuid4().hex[:12]}"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.unlink(missing_ok=True)
    controller = multi_fixture.MultiFixtureController(devices, work_root=fixture_root)
    primary_error: BaseException | None = None
    try:
        controller.start()
        xcrun = shutil.which("xcrun")
        if xcrun is None:
            raise IosMultiFaultRfError("xcrun is required for the iOS device gate")
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
                str(HOST_BUILD / "device-rf-multi-fault-install.json"),
                str(APP),
            ],
            timeout=120,
        )
        runtime_command = [
            sys.executable,
            "-m",
            "tools.react_native.run_ios_device_runtime",
            udid,
            str(log_path),
            "--seconds",
            str(timeout_seconds),
            "--rf-multi-fault-service-uuids",
            service_uuids,
        ]
        runtime = subprocess.Popen(runtime_command, cwd=PROJECT_ROOT)
        try:
            _wait_for_runtime(log_path, service_uuids, controller, timeout_seconds + 45)
            returncode = runtime.wait(timeout=30)
            if returncode != 0:
                raise IosMultiFaultRfError(
                    f"iOS device runtime exited with code {returncode}"
                )
        finally:
            if runtime.poll() is None:
                runtime.terminate()
                try:
                    _ = runtime.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    runtime.kill()
                    _ = runtime.wait(timeout=10)
        _run(
            [
                sys.executable,
                "-m",
                "tools.react_native.check_runtime_log",
                str(log_path),
                "--rf-multi-fault-service-uuids",
                service_uuids,
            ],
            timeout=30,
        )
        return log_path
    except BaseException as error:
        primary_error = error
        if isinstance(error, multi_fixture.MultiFixtureError):
            raise IosMultiFaultRfError(str(error)) from error
        raise
    finally:
        try:
            controller.stop()
        except (multi_fixture.MultiFixtureError, OSError) as error:
            print(
                f"ios-rf-multi-fault:cleanup-failed:{error}",
                file=sys.stderr,
                flush=True,
            )
            if primary_error is None:
                raise IosMultiFaultRfError("iOS multi fault cleanup failed") from error
        else:
            if primary_error is None:
                shutil.rmtree(fixture_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("udid", help="connected iOS device identifier")
    _ = parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    arguments = parser.parse_args()
    try:
        log_path = run(cast(str, arguments.udid), cast(float, arguments.timeout))
    except IosMultiFaultRfError as error:
        print(f"iOS multi fault RF error: {error}", file=sys.stderr)
        return 2
    print(f"ios-rf-multi-fault:passed log={log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
