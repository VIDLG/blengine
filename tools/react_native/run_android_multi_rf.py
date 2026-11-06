"""Run the bounded Android RN two-nRF52840-DK RF gate."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import TextIO, cast, final

from tests.peripherals.nrf52840 import multi_fixture, provider
from tests.peripherals.nrf52840.profile import load_profile
from tools.react_native import android_sdk
from tools.react_native.rf_runner import (
    ProgressReporter,
    multi_rf_timeout_seconds,
    runtime_failure,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HEARTBEAT_SECONDS = 12
PASS_MARKER = "BLEHUB_NITRO_RUNTIME rf-multi:passed:"
LISTENER_MARKER = "BLEHUB_NITRO_RUNTIME rf-multi:listener:registered-before-initialize"


class AndroidMultiRfError(RuntimeError):
    """Raised when the bounded Android multi-DK gate cannot complete safely."""


@final
class _LogcatCapture:
    """Keep the RN runtime ledger out of Android's small system log ring buffer."""

    def __init__(self, serial: str) -> None:
        self._process: subprocess.Popen[str] = subprocess.Popen(
            android_sdk.adb_command(
                serial,
                "logcat",
                "-v",
                "brief",
                "-s",
                "ReactNativeJS:I",
                "*:S",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stdout = cast(TextIO | None, self._process.stdout)
        if stdout is None:
            return
        with stdout:
            for line in stdout:
                if "BLEHUB_NITRO_RUNTIME" not in line:
                    continue
                with self._lock:
                    self._lines.append(line.rstrip("\r\n"))

    def snapshot(self) -> str:
        with self._lock:
            return "\n".join(self._lines)

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                _ = self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                _ = self._process.wait(timeout=5)
        self._reader.join(timeout=5)


def _advance_fixture_coordination(
    log: str,
    coordination_paths: dict[str, Path],
    subscription_mode: str,
    runtime_mode: str = "rf-multi",
) -> None:
    subscription_label = (
        "indication" if subscription_mode == "indication" else "notification"
    )
    cycle = 1
    for line in log.splitlines():
        if (
            runtime_mode == "rf-multi-soak"
            and "rf-multi-soak:cycle:" in line
            and ":started" in line
        ):
            cycle = int(line.split("rf-multi-soak:cycle:", 1)[1].split(":", 1)[0])
        for service_uuid, path in coordination_paths.items():
            target = (
                path / f"cycle-{cycle}" if runtime_mode == "rf-multi-soak" else path
            )
            if (
                f"rf-multi:step:{subscription_label}-enable:passed:" in line
                and f"serviceUuid={service_uuid}" in line
            ):
                target.mkdir(parents=True, exist_ok=True)
                (target / "subscription-enabled").touch()
            if (
                f"rf-multi:step:{subscription_label}-disable:passed:" in line
                and f"serviceUuid={service_uuid}" in line
            ):
                target.mkdir(parents=True, exist_ok=True)
                (target / "subscription-disabled").touch()


def _wait_for_runtime(
    serial: str,
    log_path: Path,
    service_uuids: str,
    timeout_seconds: float,
    coordination_paths: dict[str, Path],
    subscription_mode: str,
    runtime_mode: str = "rf-multi",
    capture: _LogcatCapture | None = None,
    controller: multi_fixture.MultiFixtureController | None = None,
) -> None:
    started = time.monotonic()
    deadline = started + timeout_seconds
    phase = "launch"
    progress = ProgressReporter(
        runtime_mode,
        prefix="rn_android_rf_multi",
        heartbeat_seconds=HEARTBEAT_SECONDS,
    )
    progress.phase_started(phase)
    log = ""
    seen_cycle_markers: set[str] = set()
    pass_marker = f"BLEHUB_NITRO_RUNTIME {runtime_mode}:passed:"
    while time.monotonic() < deadline:
        if controller is not None:
            try:
                controller.check_health()
            except multi_fixture.MultiFixtureError as error:
                progress.failed("fixture-exited")
                message = f"nRF52840 fixture failed during Android RN multi RF: {error}; see {log_path}"
                raise AndroidMultiRfError(message) from error
        log = capture.snapshot() if capture is not None else android_sdk.logcat(serial)
        _ = log_path.write_text(log, encoding="utf-8")
        for line in log.splitlines():
            if "BLEHUB_NITRO_RUNTIME rf-multi-soak:cycle:" not in line:
                continue
            marker = line.split("BLEHUB_NITRO_RUNTIME ", 1)[-1]
            if marker not in seen_cycle_markers:
                seen_cycle_markers.add(marker)
                progress.marker(marker)
        _advance_fixture_coordination(
            log, coordination_paths, subscription_mode, runtime_mode
        )
        failure = runtime_failure(log)
        if failure is not None:
            progress.failed(f"runtime:{failure}")
            raise AndroidMultiRfError(
                f"Android RN multi RF failed: {failure}; see {log_path}"
            )
        if phase == "launch" and LISTENER_MARKER in log:
            progress.phase_passed("launch")
            phase = "multi-rf"
            progress.phase_started(phase)
        if pass_marker in log:
            progress.phase_passed("multi-rf", marker=PASS_MARKER)
            return
        if phase != "launch" and not android_sdk.runtime_alive(serial):
            progress.failed("runtime-exited:com.blehubnitrohost")
            raise AndroidMultiRfError(
                f"Android RN multi RF host exited before the pass marker; see {log_path}"
            )
        progress.waiting(
            f"phase:{phase}",
            remaining=f"{max(0, int(deadline - time.monotonic()))}s",
        )
        _ = time.sleep(1.0)
    _ = log_path.write_text(log, encoding="utf-8")
    progress.failed(f"timeout:{phase}")
    raise AndroidMultiRfError(
        f"Android RN multi RF marker timed out; serviceUuids={service_uuids}; see {log_path}"
    )


def run(
    serial: str,
    timeout_seconds: float | None = None,
    notification_mode: str = "both",
    subscription_mode: str = "notification",
    *,
    log_suffix: str = "",
    runtime_mode: str = "rf-multi",
    soak_cycles: int = 10,
) -> Path:
    if not serial.strip():
        raise AndroidMultiRfError("an Android ADB serial is required")
    if notification_mode not in {"both", "only-dk-a", "only-dk-b", "reverse"}:
        raise AndroidMultiRfError(f"unsupported route mode: {notification_mode}")
    if subscription_mode not in {"notification", "indication"}:
        raise AndroidMultiRfError(f"unsupported subscription mode: {subscription_mode}")
    if runtime_mode not in {"rf-multi", "rf-multi-soak"}:
        raise AndroidMultiRfError(f"unsupported runtime mode: {runtime_mode}")
    try:
        effective_timeout = multi_rf_timeout_seconds(
            timeout_seconds,
            runtime_mode=runtime_mode,
            soak_cycles=soak_cycles,
        )
    except ValueError as error:
        raise AndroidMultiRfError(str(error)) from error
    if provider.PID_FILE.exists():
        raise AndroidMultiRfError(
            f"refusing to run multi-DK gate while single-DK provider marker exists: {provider.PID_FILE}"
        )

    devices = multi_fixture.load_devices()
    service_uuids = [load_profile(device.profile).service_uuid for device in devices]
    service_uuid_argument = ",".join(service_uuids)
    build_root = PROJECT_ROOT / "tests/consumers/react-native-internal/android/build"
    log_path = build_root / f"{runtime_mode}-runtime{log_suffix}.log"
    fixture_root = build_root / f"rf-multi-fixture-{uuid.uuid4().hex[:12]}"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.unlink(missing_ok=True)
    coordination_root = fixture_root / "coordination"
    coordination_paths = {
        service_uuid: coordination_root / device.name
        for service_uuid, device in zip(service_uuids, devices, strict=True)
    }
    controller = multi_fixture.MultiFixtureController(devices, work_root=fixture_root)
    capture: _LogcatCapture | None = None
    app_attempted = False
    primary_error: Exception | None = None
    try:
        print("android-rf-multi:fixture-start", flush=True)
        controller.start(
            coordination_root=coordination_root,
            coordination_mode=(
                f"soak-{subscription_mode}"
                if runtime_mode == "rf-multi-soak"
                else subscription_mode
            ),
            coordination_cycles=soak_cycles if runtime_mode == "rf-multi-soak" else 1,
        )
        print("android-rf-multi:fixture-ready", flush=True)
        _ = android_sdk.run_command(
            android_sdk.adb_command(
                serial, "shell", "am", "force-stop", "com.blehubnitrohost"
            ),
            timeout=15,
            check=True,
        )
        _ = android_sdk.run_command(
            android_sdk.adb_command(serial, "logcat", "-c"), timeout=15, check=True
        )
        capture = _LogcatCapture(serial)
        android_sdk.grant_bluetooth_permissions(serial)
        app_attempted = True
        start = android_sdk.run_command(
            android_sdk.adb_command(
                serial,
                "shell",
                "am",
                "start",
                "-n",
                "com.blehubnitrohost/.MainActivity",
                "--es",
                "blehub.runtimeMode",
                runtime_mode,
                "--es",
                "blehub.rfServiceUuid",
                service_uuid_argument,
                "--es",
                "blehub.rfMultiNotificationMode",
                notification_mode,
                "--es",
                "blehub.rfMultiSubscriptionMode",
                subscription_mode,
                "--es",
                "blehub.rfMultiCycles",
                str(soak_cycles),
            ),
            timeout=20,
            check=True,
        )
        if any(
            value in start.stdout
            for value in ("Error:", "Exception", "Activity not started")
        ):
            raise AndroidMultiRfError(
                f"unable to start RN multi host: {start.stdout[-1000:]}"
            )
        _wait_for_runtime(
            serial,
            log_path,
            service_uuid_argument,
            effective_timeout,
            coordination_paths,
            subscription_mode,
            runtime_mode,
            capture,
            controller,
        )
        checker = [
            sys.executable,
            "-m",
            "tools.react_native.check_runtime_log",
            str(log_path),
            "--rf-multi-notification-mode",
            notification_mode,
            "--rf-multi-subscription-mode",
            subscription_mode,
        ]
        if runtime_mode == "rf-multi-soak":
            checker.extend(
                [
                    "--rf-multi-soak-service-uuids",
                    service_uuid_argument,
                    "--rf-multi-soak-cycles",
                    str(soak_cycles),
                ]
            )
        else:
            checker.extend(["--rf-multi-service-uuids", service_uuid_argument])
        _ = android_sdk.run_command(checker, timeout=30, check=True)
        return log_path
    except Exception as error:
        primary_error = error
        if isinstance(error, multi_fixture.MultiFixtureError):
            raise AndroidMultiRfError(str(error)) from error
        raise
    finally:
        cleanup_errors: list[Exception] = []
        if capture is not None:
            capture.close()
        if app_attempted:
            try:
                _ = android_sdk.run_command(
                    android_sdk.adb_command(
                        serial, "shell", "am", "force-stop", "com.blehubnitrohost"
                    ),
                    timeout=15,
                    check=True,
                )
            except (android_sdk.AndroidSdkError, OSError) as error:
                cleanup_errors.append(error)
        try:
            controller.stop()
        except (multi_fixture.MultiFixtureError, OSError) as error:
            cleanup_errors.append(error)
        if primary_error is None and not cleanup_errors:
            shutil.rmtree(fixture_root, ignore_errors=True)
        else:
            print(
                f"android-rf-multi:diagnostics={fixture_root}",
                file=sys.stderr,
                flush=True,
            )
        for error in cleanup_errors:
            print(
                f"android-rf-multi:cleanup-failed:{error}", file=sys.stderr, flush=True
            )
        if cleanup_errors and primary_error is None:
            raise AndroidMultiRfError(
                "Android RN multi-DK cleanup failed"
            ) from cleanup_errors[0]


def run_cycles(
    serial: str,
    cycles: int,
    timeout_seconds: float | None = None,
    notification_mode: str = "both",
    subscription_mode: str = "notification",
) -> Path:
    """Run bounded fresh-probe multi-DK cycles, stopping on the first failure."""
    if cycles < 1 or cycles > 20:
        raise AndroidMultiRfError("multi RF cycles must be between 1 and 20")
    last_log: Path | None = None
    for cycle in range(1, cycles + 1):
        print(f"android-rf-multi:cycle={cycle}/{cycles}:started", flush=True)
        last_log = run(
            serial,
            timeout_seconds,
            notification_mode,
            subscription_mode,
            log_suffix=f"-cycle-{cycle}",
            runtime_mode="rf-multi",
            soak_cycles=1,
        )
        print(f"android-rf-multi:cycle={cycle}/{cycles}:passed", flush=True)
    assert last_log is not None
    return last_log


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("serial", help="ADB serial of the Android Central device")
    _ = parser.add_argument("--timeout", type=float, default=None)
    _ = parser.add_argument(
        "--notification-mode",
        choices=("both", "only-dk-a", "only-dk-b", "reverse"),
        default="both",
    )
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
                cast(str, arguments.serial),
                cast(float | None, arguments.timeout),
                cast(str, arguments.notification_mode),
                cast(str, arguments.subscription_mode),
                runtime_mode="rf-multi-soak",
                soak_cycles=cast(int, arguments.soak_cycles),
            )
        else:
            log_path = run_cycles(
                cast(str, arguments.serial),
                cast(int, arguments.cycles),
                cast(float | None, arguments.timeout),
                cast(str, arguments.notification_mode),
                cast(str, arguments.subscription_mode),
            )
    except AndroidMultiRfError as error:
        print(f"android multi RF error: {error}", file=sys.stderr)
        return 2
    print(f"android-rf-multi:passed log={log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
