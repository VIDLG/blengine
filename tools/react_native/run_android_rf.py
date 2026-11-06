"""Run one bounded React Native Android RF gate against the project nRF fixture."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from tests.peripherals.nrf52840 import multi_fixture, provider
from tools.check_peripheral_profiles import NRF_MULTIFUNCTION_A_PATH, load_profile
from tools.react_native import android_sdk
from tools.react_native.rf_runner import (
    ProgressReporter,
    runtime_failure,
    runtime_markers,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SERVICE_UUID = str(
    load_profile(NRF_MULTIFUNCTION_A_PATH)["services"][0]["uuid"]
).casefold()
DEFAULT_TIMEOUT_SECONDS = 120
LAUNCH_TIMEOUT_SECONDS = 20
INITIALIZATION_TIMEOUT_SECONDS = 25
HEARTBEAT_SECONDS = 12
LAUNCH_MARKER = "BLEHUB_NITRO_RUNTIME rf:listener:registered-before-initialize"
INITIALIZATION_MARKER = "BLEHUB_NITRO_RUNTIME rf:step:initialize:passed:"

PASS_MARKERS = {
    "rf": "BLEHUB_NITRO_RUNTIME rf:passed:",
    "rf-connect": "BLEHUB_NITRO_RUNTIME rf-connect:passed:",
    "rf-gatt": "BLEHUB_NITRO_RUNTIME rf-gatt:passed:",
    "rf-io": "BLEHUB_NITRO_RUNTIME rf-io:passed:",
    "rf-rssi": "BLEHUB_NITRO_RUNTIME rf-rssi:passed:",
    "rf-notification": "BLEHUB_NITRO_RUNTIME rf-notification:passed:",
    "rf-indication": "BLEHUB_NITRO_RUNTIME rf-indication:passed:",
    "rf-passive-disconnect": "BLEHUB_NITRO_RUNTIME rf-passive-disconnect:passed:",
    "rf-cancellation": "BLEHUB_NITRO_RUNTIME rf-cancellation:passed:",
}
RECOVERY_MODES = {
    "rf-recovery": ("recovery", 3),
    "rf-recovery-ten": ("recovery-ten", 10),
}
SUPPORTED_MODES = (*PASS_MARKERS, *RECOVERY_MODES)
COORDINATION_MODES = {
    "rf-notification": "notification",
    "rf-indication": "indication",
    "rf-passive-disconnect": "passive-disconnect",
    **{mode: coordination for mode, (coordination, _cycles) in RECOVERY_MODES.items()},
}


class AndroidRfError(RuntimeError):
    """Raised when the bounded Android RF controller cannot complete safely."""


def _advance_fixture_coordination(
    mode: str, log: str, coordination_path: Path | None
) -> None:
    if coordination_path is None:
        return
    if mode == "rf-passive-disconnect":
        if (
            "BLEHUB_NITRO_RUNTIME rf:step:notification-enable:passed:" in log
            and not (coordination_path / "subscription-enabled").exists()
        ):
            (coordination_path / "subscription-enabled").touch()
        if (
            "BLEHUB_NITRO_RUNTIME rf:step:notification-value:passed:" in log
            and not (coordination_path / "connected").exists()
        ):
            (coordination_path / "connected").touch()
        return
    step_prefix = "notification" if mode == "rf-notification" else "indication"
    if (
        f"BLEHUB_NITRO_RUNTIME rf:step:{step_prefix}-enable:passed:" in log
        and not (coordination_path / "subscription-enabled").exists()
    ):
        (coordination_path / "subscription-enabled").touch()
    if (
        f"BLEHUB_NITRO_RUNTIME rf:step:{step_prefix}-disable:passed:" in log
        and not (coordination_path / "subscription-disabled").exists()
    ):
        (coordination_path / "subscription-disabled").touch()


def _wait_for_runtime(
    serial: str,
    mode: str,
    log_path: Path,
    timeout_seconds: float,
    coordination_path: Path | None = None,
    pass_marker: str | None = None,
    failure_marker: str | None = None,
) -> None:
    started = time.monotonic()
    deadline = started + timeout_seconds
    phase = "launch"
    phase_deadline = min(deadline, started + LAUNCH_TIMEOUT_SECONDS)
    progress = ProgressReporter(
        mode,
        prefix="rn_android_rf",
        heartbeat_seconds=HEARTBEAT_SECONDS,
    )
    progress.phase_started("launch")
    seen: set[str] = set()
    latest = "<none>"
    log = ""
    while time.monotonic() < min(deadline, phase_deadline):
        log = android_sdk.logcat(
            serial,
            cwd=PROJECT_ROOT,
            timeout=min(15, min(deadline, phase_deadline) - time.monotonic()),
        )
        now = time.monotonic()
        _advance_fixture_coordination(mode, log, coordination_path)
        markers = runtime_markers(log)
        if markers:
            latest = markers[-1]
        failed_line = runtime_failure(log)
        if failure_marker is None and failed_line is not None:
            log_path.write_text(log, encoding="utf-8")
            progress.failed(f"runtime:{failed_line}")
            raise AndroidRfError(f"Android RN RF failed: {failed_line}; see {log_path}")
        if failure_marker is not None and failure_marker in log:
            log_path.write_text(log, encoding="utf-8")
            progress.failed(f"runtime:{failure_marker}")
            raise AndroidRfError(
                f"Android RN RF failed: {failure_marker}; see {log_path}"
            )
        if latest not in seen and markers:
            progress.marker(
                latest,
                remaining=f"{max(0, int(deadline - now))}s",
            )
            seen.add(latest)
        if now >= min(deadline, phase_deadline):
            break
        if phase == "launch" and LAUNCH_MARKER in log:
            progress.phase_passed("launch")
            phase = "initialization"
            progress.phase_started(phase)
            phase_deadline = min(deadline, now + INITIALIZATION_TIMEOUT_SECONDS)
        if phase == "initialization" and INITIALIZATION_MARKER in log:
            progress.phase_passed("initialization")
            phase = "rf"
            progress.phase_started(phase)
            phase_deadline = deadline
        if phase == "rf" and (pass_marker or PASS_MARKERS[mode]) in log:
            progress.phase_passed("rf", marker=PASS_MARKERS[mode])
            log_path.write_text(log, encoding="utf-8")
            return
        if phase != "launch" and not android_sdk.runtime_alive(
            serial, cwd=PROJECT_ROOT
        ):
            log_path.write_text(log, encoding="utf-8")
            progress.failed("runtime-exited:com.blehubnitrohost")
            raise AndroidRfError(
                f"Android RN host exited before the RF pass marker; inspect {log_path}"
            )
        progress.waiting(
            f"phase:{phase}",
            remaining=f"{max(0, int(deadline - now))}s",
        )
        time.sleep(min(2, max(0, min(deadline, phase_deadline) - time.monotonic())))
    log_path.write_text(log, encoding="utf-8")
    progress.failed(f"timeout:{phase}")
    raise AndroidRfError(
        f"Android RN RF {phase} marker timed out; latest={latest}; see {log_path}"
    )


def _wait_for_recovery(
    serial: str,
    log_path: Path,
    coordination_path: Path,
    mode: str,
    cycles: int,
    timeout_seconds: float,
) -> None:
    """Drive the real fixture ACK protocol for Android recovery cycles."""
    prefix = mode
    deadline = time.monotonic() + timeout_seconds
    captured: list[str] = []
    seen_runtime_lines: set[str] = set()
    latest_log = ""

    def refresh() -> str:
        nonlocal latest_log
        snapshot = android_sdk.logcat(serial, cwd=PROJECT_ROOT)
        for line in snapshot.splitlines(keepends=True):
            if "BLEHUB_NITRO_RUNTIME" in line and line not in seen_runtime_lines:
                seen_runtime_lines.add(line)
                captured.append(line)
        latest_log = "".join(captured)
        log_path.write_text(latest_log, encoding="utf-8")
        failure = runtime_failure(latest_log)
        if failure is not None:
            raise AndroidRfError(
                f"Android RN RF recovery failed: {failure}; see {log_path}"
            )
        if not android_sdk.runtime_alive(serial, cwd=PROJECT_ROOT):
            raise AndroidRfError(
                f"Android RN host exited during RF recovery; see {log_path}"
            )
        return latest_log

    def wait_for_log(marker: str, label: str, timeout: float) -> None:
        limit = min(deadline, time.monotonic() + timeout)
        while time.monotonic() < limit:
            if marker in refresh():
                return
            time.sleep(1)
        raise AndroidRfError(
            f"Android RF recovery timed out waiting for {label}: {marker}; see {log_path}"
        )

    def wait_for_file(path: Path, label: str, timeout: float) -> None:
        limit = min(deadline, time.monotonic() + timeout)
        while time.monotonic() < limit:
            refresh()
            if path.is_file():
                return
            time.sleep(1)
        raise AndroidRfError(
            f"Android RF recovery timed out waiting for {label}: {path}; see {log_path}"
        )

    wait_for_log(
        f"BLEHUB_NITRO_RUNTIME {prefix}:listener:registered-before-initialize",
        "listener registration",
        30,
    )
    wait_for_log(
        f"BLEHUB_NITRO_RUNTIME {prefix}:initialize:passed:",
        "initialization",
        45,
    )
    for cycle in range(cycles):
        directory = coordination_path / f"cycle-{cycle}"
        wait_for_file(
            directory / "fixture-ready", f"cycle {cycle} fixture readiness", 90
        )
        wait_for_log(
            f"BLEHUB_NITRO_RUNTIME {prefix}:cycle:{cycle}:notification-enable:passed:",
            f"cycle {cycle} notification enable",
            240,
        )
        (directory / "subscription-enabled").write_text("ready\n", encoding="ascii")
        wait_for_file(
            directory / "value-applied", f"cycle {cycle} value application", 90
        )
        wait_for_log(
            f"BLEHUB_NITRO_RUNTIME {prefix}:cycle:{cycle}:notification-value:passed:",
            f"cycle {cycle} notification value",
            240,
        )
        (directory / "connected").write_text("ready\n", encoding="ascii")
        wait_for_file(directory / "fault-triggered", f"cycle {cycle} fault trigger", 90)
        wait_for_log(
            f"BLEHUB_NITRO_RUNTIME {prefix}:cycle:{cycle}:passive-disconnect:passed:",
            f"cycle {cycle} passive disconnect",
            240,
        )
        (directory / "passive-disconnected").write_text("ready\n", encoding="ascii")
        wait_for_file(
            directory / "recovery-ready", f"cycle {cycle} recovery readiness", 90
        )
        wait_for_log(
            f"BLEHUB_NITRO_RUNTIME {prefix}:cycle:{cycle}:cycle:passed:",
            f"cycle {cycle} completion",
            240,
        )
        (directory / "cycle-passed").write_text("ready\n", encoding="ascii")
    wait_for_log(
        f"BLEHUB_NITRO_RUNTIME {prefix}:close:passed:",
        "recovery close",
        60,
    )
    log_path.write_text(latest_log, encoding="utf-8")


def _checker_arguments(mode: str, log_path: Path, service_uuid: str) -> list[str]:
    flag = {
        "rf": "--rf-service-uuid",
        "rf-connect": "--rf-connect-service-uuid",
        "rf-gatt": "--rf-gatt-service-uuid",
        "rf-io": "--rf-io-service-uuid",
        "rf-rssi": "--rf-rssi-service-uuid",
        "rf-notification": "--rf-notification-service-uuid",
        "rf-indication": "--rf-indication-service-uuid",
        "rf-passive-disconnect": "--rf-passive-disconnect-service-uuid",
        "rf-cancellation": "--rf-cancellation-service-uuid",
        "rf-recovery": "--rf-recovery-service-uuid",
        "rf-recovery-ten": "--rf-recovery-ten-service-uuid",
    }[mode]
    return [
        sys.executable,
        "-m",
        "tools.react_native.check_runtime_log",
        str(log_path),
        flag,
        service_uuid,
    ]


def _wait_for_selected_runtime(
    serial: str,
    mode: str,
    log_path: Path,
    coordination_path: Path | None,
    timeout_seconds: float,
    suite_flow: str | None,
) -> None:
    if mode in RECOVERY_MODES:
        if coordination_path is None:
            raise AndroidRfError("recovery mode requires a coordination path")
        _wait_for_recovery(
            serial,
            log_path,
            coordination_path,
            mode,
            RECOVERY_MODES[mode][1],
            timeout_seconds,
        )
        return
    suite_marker = (
        f"BLEHUB_NITRO_RUNTIME rf-lab-suite:flow-passed:index=0:mode={suite_flow}"
        if suite_flow is not None
        else None
    )
    _wait_for_runtime(
        serial,
        mode,
        log_path,
        timeout_seconds,
        coordination_path,
        pass_marker=suite_marker,
        failure_marker=(
            f"BLEHUB_NITRO_RUNTIME rf-lab-suite:flow-failed:index=0:mode={suite_flow}:"
            if suite_flow is not None
            else None
        ),
    )


def _effective_timeout(mode: str, timeout_seconds: float) -> float:
    if mode not in RECOVERY_MODES or timeout_seconds != DEFAULT_TIMEOUT_SECONDS:
        return timeout_seconds
    return 900 if RECOVERY_MODES[mode][1] == 10 else 450


def run(
    serial: str,
    mode: str,
    service_uuid: str,
    timeout_seconds: float,
    *,
    fixture_device: str | None = None,
    runtime_mode: str | None = None,
    suite_flow: str | None = None,
) -> Path:
    if mode == "rf-cancellation" or (
        runtime_mode == "rf-lab-suite"
        and suite_flow in (None, "rf-cancellation", "rf-watchdog")
    ):
        from tests.peripherals.nrf52840.direct_fixture import require_v2_coordination

        require_v2_coordination("legacy-suite-runner")
    if mode not in SUPPORTED_MODES:
        raise AndroidRfError(f"unsupported Android RN RF mode: {mode}")
    if timeout_seconds <= 0:
        raise AndroidRfError("RF timeout must be positive")
    timeout_seconds = _effective_timeout(mode, timeout_seconds)
    log_path = (
        PROJECT_ROOT
        / "tests/consumers/react-native-internal/android/build"
        / f"{mode}-runtime.log"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.unlink(missing_ok=True)
    if fixture_device is None and provider.PID_FILE.exists():
        raise AndroidRfError(
            f"refusing to take over existing fixture PID file (even if stale): {provider.PID_FILE}"
        )
    fixture_attempted = False
    owned_identity: tuple[int, int] | None = None
    fixture_controller: multi_fixture.MultiFixtureController | None = None
    fixture_root: Path | None = None
    app_attempted = False
    coordination_path: Path | None = None
    try:
        print("android-rf:fixture-start", flush=True)
        coordination_mode = COORDINATION_MODES.get(mode)

        if fixture_device is not None:
            devices = multi_fixture.load_devices(names=(fixture_device,))
            expected_service_uuid = str(
                load_profile(devices[0].profile)["services"][0]["uuid"]
            ).casefold()
            if service_uuid.casefold() != expected_service_uuid:
                raise AndroidRfError(
                    f"service UUID {service_uuid} does not match {fixture_device} "
                    f"profile service {expected_service_uuid}"
                )
            fixture_root = (
                PROJECT_ROOT
                / "tests/consumers/react-native-internal/android/build"
                / f"{mode}-{fixture_device}-fixture-{time.time_ns()}"
            )
            coordination_root = fixture_root / "coordination"
            if coordination_mode is not None:
                coordination_path = coordination_root / fixture_device
            fixture_controller = multi_fixture.MultiFixtureController(
                devices,
                work_root=fixture_root,
                allow_single_device=True,
            )
            fixture_attempted = True
            fixture_controller.start(
                coordination_root=coordination_root if coordination_mode else None,
                coordination_mode=coordination_mode or "notification",
            )
        else:
            fixture_attempted = True
            if provider.PID_FILE.exists():
                raise AndroidRfError(
                    f"refusing to take over existing fixture PID file (even if stale): {provider.PID_FILE}"
                )
            fixture_command = [
                sys.executable,
                "-m",
                "tests.peripherals.nrf52840.provider",
                "direct-start",
            ]
            if coordination_mode is not None:
                coordination_path = (
                    PROJECT_ROOT
                    / "tests/consumers/react-native-internal/android/build"
                    / f"{mode}-coordination"
                )
                shutil.rmtree(coordination_path, ignore_errors=True)
                coordination_path.mkdir(parents=True, exist_ok=True)
                fixture_command.extend(
                    (
                        "--coordination-path",
                        str(coordination_path),
                        "--coordination-mode",
                        coordination_mode,
                    )
                )
            android_sdk.run_command(
                fixture_command,
                cwd=PROJECT_ROOT,
                timeout=provider.DEFAULT_READY_TIMEOUT + 15,
                check=True,
            )
            owned_identity = provider._process_identity()
            if owned_identity is None:
                raise AndroidRfError(
                    "direct-start returned without a fixture PID/PGID marker"
                )
        print("android-rf:fixture-ready", flush=True)
        android_sdk.run_command(
            android_sdk.adb_command(
                serial, "shell", "am", "force-stop", "com.blehubnitrohost"
            ),
            cwd=PROJECT_ROOT,
            timeout=15,
            check=True,
        )
        android_sdk.run_command(
            android_sdk.adb_command(serial, "logcat", "-c"),
            cwd=PROJECT_ROOT,
            timeout=15,
            check=True,
        )
        android_sdk.grant_bluetooth_permissions(serial, cwd=PROJECT_ROOT)
        app_attempted = True
        launch_arguments = [
            "shell",
            "am",
            "start",
            "-n",
            "com.blehubnitrohost/.MainActivity",
            "--es",
            "blehub.runtimeMode",
            runtime_mode or mode,
            "--es",
            "blehub.rfServiceUuid",
            service_uuid,
        ]
        if suite_flow is not None:
            launch_arguments.extend(["--es", "blehub.rfLabSuiteFlow", suite_flow])
        start = android_sdk.run_command(
            android_sdk.adb_command(serial, *launch_arguments),
            cwd=PROJECT_ROOT,
            timeout=20,
            check=True,
        )
        if re.search(r"(?:Error:|Exception|Activity not started)", start.stdout):
            raise AndroidRfError(
                f"unable to start a fresh RN host: {start.stdout[-1000:]}"
            )
        _wait_for_selected_runtime(
            serial,
            mode,
            log_path,
            coordination_path,
            timeout_seconds,
            suite_flow,
        )
        android_sdk.run_command(
            _checker_arguments(mode, log_path, service_uuid),
            cwd=PROJECT_ROOT,
            timeout=30,
            check=True,
        )
        return log_path
    except provider.Nrf52840Error as error:
        raise AndroidRfError(str(error)) from error
    except android_sdk.AndroidSdkError as error:
        raise AndroidRfError(str(error)) from error
    finally:
        primary_error = sys.exc_info()[1]
        cleanup_errors: list[Exception] = []
        if app_attempted:
            try:
                android_sdk.run_command(
                    android_sdk.adb_command(
                        serial, "shell", "am", "force-stop", "com.blehubnitrohost"
                    ),
                    cwd=PROJECT_ROOT,
                    timeout=15,
                    check=True,
                )
            except (
                AndroidRfError,
                android_sdk.AndroidSdkError,
                OSError,
                provider.Nrf52840Error,
                subprocess.SubprocessError,
            ) as error:
                cleanup_errors.append(error)
        if fixture_attempted:
            try:
                if fixture_controller is not None:
                    fixture_controller.stop()
                else:
                    # A failed/partial direct-start can leave its newly written PID marker.
                    if owned_identity is None:
                        owned_identity = provider._process_identity()
                    if owned_identity is not None:
                        if provider._process_identity() != owned_identity:
                            raise AndroidRfError(
                                "fixture identity changed; refusing to stop it"
                            )
                        pid, pgid = owned_identity
                        android_sdk.run_command(
                            [
                                sys.executable,
                                "-m",
                                "tests.peripherals.nrf52840.provider",
                                "stop-owned",
                                "--pid",
                                str(pid),
                                "--pgid",
                                str(pgid),
                            ],
                            cwd=PROJECT_ROOT,
                            timeout=35,
                            check=True,
                        )
            except (
                AndroidRfError,
                android_sdk.AndroidSdkError,
                OSError,
                provider.Nrf52840Error,
                multi_fixture.MultiFixtureError,
                subprocess.SubprocessError,
            ) as error:
                cleanup_errors.append(error)
        if fixture_device is None and coordination_path is not None:
            shutil.rmtree(coordination_path, ignore_errors=True)
        if fixture_root is not None:
            if primary_error is None and not cleanup_errors:
                shutil.rmtree(fixture_root, ignore_errors=True)
            else:
                print(
                    f"android-rf:diagnostics={fixture_root}",
                    file=sys.stderr,
                    flush=True,
                )
        for error in cleanup_errors:
            print(f"android-rf:cleanup-failed: {error}", file=sys.stderr, flush=True)
        if cleanup_errors and primary_error is None:
            raise AndroidRfError("Android RN RF cleanup failed") from cleanup_errors[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("serial", help="ADB serial of the Android Central device")
    _ = parser.add_argument("--mode", choices=sorted(SUPPORTED_MODES), default="rf")
    _ = parser.add_argument("--service-uuid", default=DEFAULT_SERVICE_UUID)
    _ = parser.add_argument(
        "--fixture-device",
        help="machine-local DK name from peripheral_nrf52840.devices; enables a profile-aware single-DK fixture",
    )
    _ = parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    arguments = parser.parse_args()
    try:
        log_path = run(
            arguments.serial,
            arguments.mode,
            arguments.service_uuid,
            arguments.timeout,
            fixture_device=arguments.fixture_device,
        )
    except AndroidRfError as error:
        print(f"android RF error: {error}", file=sys.stderr)
        return 2
    print(f"android-rf:passed log={log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
