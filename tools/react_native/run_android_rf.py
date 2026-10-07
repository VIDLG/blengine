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
from tools.android.run_instrumentation import has_root
from tools.android_toolchain import adb_executable
from tools.check_peripheral_profiles import load_profile
from tools.react_native.rf_runner import (
    ProgressReporter,
    runtime_failure,
    runtime_markers,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SERVICE_UUID = "fd000000-0000-4000-8000-000000000000"
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
}
COORDINATION_MODES = {
    "rf-notification": "notification",
    "rf-indication": "indication",
    "rf-passive-disconnect": "passive-disconnect",
}


class AndroidRfError(RuntimeError):
    """Raised when the bounded Android RF controller cannot complete safely."""


def _adb(serial: str, *arguments: str) -> list[str]:
    return [str(adb_executable()), "-s", serial, *arguments]


def _run(
    command: list[str], *, timeout: float, check: bool = False
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AndroidRfError(
            f"command failed to start or finish: {' '.join(command)}"
        ) from error
    if check and result.returncode:
        raise AndroidRfError(
            f"command failed (exit={result.returncode}): {' '.join(command)}; "
            f"{result.stdout[-1500:]}"
        )
    return result


def _grant_bluetooth_permissions(serial: str) -> None:
    sdk = _run(
        _adb(serial, "shell", "getprop", "ro.build.version.sdk"),
        timeout=15,
        check=True,
    ).stdout.strip()
    if not sdk.isdecimal() or int(sdk) <= 0:
        raise AndroidRfError(f"invalid Android device API level: {sdk!r}")
    api = int(sdk)
    # Match the host's neverForLocation scan and maxSdkVersion=30 location grant.
    permissions = (
        ("android.permission.BLUETOOTH_SCAN", "android.permission.BLUETOOTH_CONNECT")
        if api >= 31
        else ("android.permission.ACCESS_FINE_LOCATION",)
        if api >= 23
        else ()
    )
    if not permissions:
        return
    try:
        use_root = has_root(adb_executable(), serial)
    except (OSError, subprocess.SubprocessError) as error:
        raise AndroidRfError(
            "unable to determine Android permission grant root access"
        ) from error
    for permission in permissions:
        arguments = (
            ("shell", "su", "-c", f"pm grant com.blehubnitrohost {permission}")
            if use_root
            else ("shell", "pm", "grant", "com.blehubnitrohost", permission)
        )
        _run(_adb(serial, *arguments), timeout=15, check=True)


def _logcat(serial: str, *, timeout: float = 15) -> str:
    return _run(
        _adb(serial, "logcat", "-d", "-v", "brief"), timeout=timeout, check=True
    ).stdout


def runtime_alive(serial: str) -> bool:
    """Return whether the RN host process is still alive on the selected device."""
    result = _run(
        _adb(serial, "shell", "pidof", "com.blehubnitrohost"),
        timeout=5,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def adb_command(serial: str, *arguments: str) -> list[str]:
    return _adb(serial, *arguments)


def run_command(
    command: list[str], *, timeout: float, check: bool = False
) -> subprocess.CompletedProcess[str]:
    return _run(command, timeout=timeout, check=check)


def grant_bluetooth_permissions(serial: str) -> None:
    _grant_bluetooth_permissions(serial)


def logcat(serial: str, *, timeout: float = 15) -> str:
    return _logcat(serial, timeout=timeout)


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
        log = _logcat(
            serial, timeout=min(15, min(deadline, phase_deadline) - time.monotonic())
        )
        now = time.monotonic()
        _advance_fixture_coordination(mode, log, coordination_path)
        markers = runtime_markers(log)
        if markers:
            latest = markers[-1]
        failed_line = runtime_failure(log)
        if failed_line is not None:
            log_path.write_text(log, encoding="utf-8")
            progress.failed(f"runtime:{failed_line}")
            raise AndroidRfError(f"Android RN RF failed: {failed_line}; see {log_path}")
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
        if phase == "rf" and PASS_MARKERS[mode] in log:
            progress.phase_passed("rf", marker=PASS_MARKERS[mode])
            log_path.write_text(log, encoding="utf-8")
            return
        if phase != "launch" and not runtime_alive(serial):
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
    }[mode]
    return [
        sys.executable,
        "-m",
        "tools.react_native.check_runtime_log",
        str(log_path),
        flag,
        service_uuid,
    ]


def run(
    serial: str,
    mode: str,
    service_uuid: str,
    timeout_seconds: float,
    *,
    fixture_device: str | None = None,
) -> Path:
    if mode not in PASS_MARKERS:
        raise AndroidRfError(f"unsupported Android RN RF mode: {mode}")
    if timeout_seconds <= 0:
        raise AndroidRfError("RF timeout must be positive")
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
            _run(
                fixture_command,
                timeout=provider.DEFAULT_READY_TIMEOUT + 15,
                check=True,
            )
            owned_identity = provider._process_identity()
            if owned_identity is None:
                raise AndroidRfError(
                    "direct-start returned without a fixture PID/PGID marker"
                )
        print("android-rf:fixture-ready", flush=True)
        _run(
            _adb(serial, "shell", "am", "force-stop", "com.blehubnitrohost"),
            timeout=15,
            check=True,
        )
        _run(_adb(serial, "logcat", "-c"), timeout=15, check=True)
        _grant_bluetooth_permissions(serial)
        app_attempted = True
        start = _run(
            _adb(
                serial,
                "shell",
                "am",
                "start",
                "-n",
                "com.blehubnitrohost/.MainActivity",
                "--es",
                "blehub.runtimeMode",
                mode,
                "--es",
                "blehub.rfServiceUuid",
                service_uuid,
            ),
            timeout=20,
            check=True,
        )
        if re.search(r"(?:Error:|Exception|Activity not started)", start.stdout):
            raise AndroidRfError(
                f"unable to start a fresh RN host: {start.stdout[-1000:]}"
            )
        _wait_for_runtime(serial, mode, log_path, timeout_seconds, coordination_path)
        _run(_checker_arguments(mode, log_path, service_uuid), timeout=30, check=True)
        return log_path
    except provider.Nrf52840Error as error:
        raise AndroidRfError(str(error)) from error
    finally:
        primary_error = sys.exc_info()[1]
        cleanup_errors: list[Exception] = []
        if app_attempted:
            try:
                _run(
                    _adb(serial, "shell", "am", "force-stop", "com.blehubnitrohost"),
                    timeout=15,
                    check=True,
                )
            except (
                AndroidRfError,
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
                        _run(
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
                            timeout=35,
                            check=True,
                        )
            except (
                AndroidRfError,
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
    _ = parser.add_argument("--mode", choices=sorted(PASS_MARKERS), default="rf")
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
