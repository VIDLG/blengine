"""Run the bounded Android RN multi-DK fault-isolation/reconnect gate."""

from __future__ import annotations

import argparse
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import cast

from tests.peripherals.nrf52840 import multi_fixture, provider
from tests.peripherals.nrf52840.profile import load_profile
from tools.react_native import run_android_rf
from tools.react_native.rf_runner import ProgressReporter, runtime_failure

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TIMEOUT_SECONDS = 240
HEARTBEAT_SECONDS = 12
READY_MARKER = "BLEHUB_NITRO_RUNTIME rf-multi-fault:ready-for-a-fault:"
RESTART_MARKER = "BLEHUB_NITRO_RUNTIME rf-multi-fault:awaiting-a-restart"
PASS_MARKER = "BLEHUB_NITRO_RUNTIME rf-multi-fault:passed:"


class AndroidMultiFaultRfError(RuntimeError):
    """Raised when the bounded Android multi-DK fault gate cannot complete."""


def _wait_for_runtime(
    serial: str,
    log_path: Path,
    service_uuids: str,
    controller: multi_fixture.MultiFixtureController,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    progress = ProgressReporter(
        "rf-multi-fault",
        prefix="rn_android_rf_multi_fault",
        heartbeat_seconds=HEARTBEAT_SECONDS,
    )
    phase = "launch"
    fault_triggered = False
    restarted = False
    log = ""
    progress.phase_started(phase)
    while time.monotonic() < deadline:
        log = run_android_rf.logcat(serial)
        _ = log_path.write_text(log, encoding="utf-8")
        failure = runtime_failure(log)
        if failure is not None:
            progress.failed(f"runtime:{failure}")
            raise AndroidMultiFaultRfError(
                f"Android RN multi fault RF failed: {failure}; see {log_path}"
            )
        if (
            phase == "launch"
            and "BLEHUB_NITRO_RUNTIME rf-multi-fault:listener:registered-before-initialize"
            in log
        ):
            progress.phase_passed("launch")
            phase = "two-connections"
            progress.phase_started(phase)
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
                raise AndroidMultiFaultRfError(
                    "fault gate reported pass before DK-A restart"
                )
            progress.phase_passed("reconnect", marker=PASS_MARKER)
            return
        if phase != "launch" and not run_android_rf.runtime_alive(serial):
            progress.failed("runtime-exited:com.blehubnitrohost")
            raise AndroidMultiFaultRfError(
                f"Android RN multi fault host exited before the pass marker; see {log_path}"
            )
        progress.waiting(
            f"phase:{phase}",
            remaining=f"{max(0, int(deadline - time.monotonic()))}s",
        )
        _ = time.sleep(1.0)
    _ = log_path.write_text(log, encoding="utf-8")
    progress.failed(f"timeout:{phase}")
    raise AndroidMultiFaultRfError(
        f"Android RN multi fault marker timed out; serviceUuids={service_uuids}; see {log_path}"
    )


def run(serial: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> Path:
    if not serial.strip():
        raise AndroidMultiFaultRfError("an Android ADB serial is required")
    if timeout_seconds <= 0:
        raise AndroidMultiFaultRfError("multi fault RF timeout must be positive")
    if provider.PID_FILE.exists():
        raise AndroidMultiFaultRfError(
            f"refusing to run multi-DK fault gate while single-DK provider marker exists: {provider.PID_FILE}"
        )
    devices = multi_fixture.load_devices()
    service_uuids = ",".join(
        load_profile(device.profile).service_uuid for device in devices
    )
    build_root = PROJECT_ROOT / "tests/consumers/react-native-internal/android/build"
    log_path = build_root / "rf-multi-fault-runtime.log"
    fixture_root = build_root / f"rf-multi-fault-fixture-{uuid.uuid4().hex[:12]}"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.unlink(missing_ok=True)
    controller = multi_fixture.MultiFixtureController(devices, work_root=fixture_root)
    app_attempted = False
    primary_error: BaseException | None = None
    try:
        print("android-rf-multi-fault:fixture-start", flush=True)
        controller.start()
        print("android-rf-multi-fault:fixture-ready", flush=True)
        _ = run_android_rf.run_command(
            run_android_rf.adb_command(
                serial, "shell", "am", "force-stop", "com.blehubnitrohost"
            ),
            timeout=15,
            check=True,
        )
        _ = run_android_rf.run_command(
            run_android_rf.adb_command(serial, "logcat", "-c"), timeout=15, check=True
        )
        run_android_rf.grant_bluetooth_permissions(serial)
        app_attempted = True
        started = run_android_rf.run_command(
            run_android_rf.adb_command(
                serial,
                "shell",
                "am",
                "start",
                "-n",
                "com.blehubnitrohost/.MainActivity",
                "--es",
                "blehub.runtimeMode",
                "rf-multi-fault",
                "--es",
                "blehub.rfServiceUuid",
                service_uuids,
            ),
            timeout=20,
            check=True,
        )
        if any(
            value in started.stdout
            for value in ("Error:", "Exception", "Activity not started")
        ):
            raise AndroidMultiFaultRfError(
                f"unable to start RN multi fault host: {started.stdout[-1000:]}"
            )
        _wait_for_runtime(serial, log_path, service_uuids, controller, timeout_seconds)
        checker = [
            sys.executable,
            "-m",
            "tools.react_native.check_runtime_log",
            str(log_path),
            "--rf-multi-fault-service-uuids",
            service_uuids,
        ]
        _ = run_android_rf.run_command(checker, timeout=30, check=True)
        return log_path
    except BaseException as error:
        primary_error = error
        if isinstance(error, multi_fixture.MultiFixtureError):
            raise AndroidMultiFaultRfError(str(error)) from error
        raise
    finally:
        cleanup_errors: list[Exception] = []
        if app_attempted:
            try:
                _ = run_android_rf.run_command(
                    run_android_rf.adb_command(
                        serial, "shell", "am", "force-stop", "com.blehubnitrohost"
                    ),
                    timeout=15,
                    check=True,
                )
            except (run_android_rf.AndroidRfError, OSError) as error:
                cleanup_errors.append(error)
        try:
            controller.stop()
        except (multi_fixture.MultiFixtureError, OSError) as error:
            cleanup_errors.append(error)
        if not cleanup_errors or primary_error is None:
            shutil.rmtree(fixture_root, ignore_errors=True)
        for error in cleanup_errors:
            print(
                f"android-rf-multi-fault:cleanup-failed:{error}",
                file=sys.stderr,
                flush=True,
            )
        if cleanup_errors and primary_error is None:
            raise AndroidMultiFaultRfError(
                "Android multi fault cleanup failed"
            ) from cleanup_errors[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("serial", help="ADB serial of the Android Central device")
    _ = parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    arguments = parser.parse_args()
    try:
        log_path = run(cast(str, arguments.serial), cast(float, arguments.timeout))
    except AndroidMultiFaultRfError as error:
        print(f"android multi fault RF error: {error}", file=sys.stderr)
        return 2
    print(f"android-rf-multi-fault:passed log={log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
