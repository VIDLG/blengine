"""Run the RN iPhone RF scan with an RN-local fixture ownership guard."""

from __future__ import annotations

import argparse
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType
from typing import Literal, cast

from tests.peripherals.nrf52840 import provider
from tools.check_peripheral_profiles import NRF_MULTIFUNCTION_A_PATH, load_profile
from tools.react_native.host_contract import APP_NAME
from tools.react_native.rf_runner import (
    DEFAULT_HEARTBEAT_SECONDS,
    ProgressReporter,
    check_runtime_process,
    runtime_failure,
    wait_for_process_exit,
    wait_for_runtime_log_marker,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOST_BUILD = PROJECT_ROOT / "tests/consumers/react-native-internal/ios/build"
APP = HOST_BUILD / "device-derived/Build/Products/Debug-iphoneos" / APP_NAME
SERVICE_UUID = str(
    load_profile(NRF_MULTIFUNCTION_A_PATH)["services"][0]["uuid"]
).casefold()
LOCK = provider.WORK_ROOT / "rn-ios-scan.lock"
# Kept as a module-level compatibility alias for existing runner tests/tools.
HEARTBEAT_SECONDS = DEFAULT_HEARTBEAT_SECONDS
__all__ = ["ProgressReporter", "runtime_failure"]


def fixture_pid() -> int | None:
    return provider._pid()


def stop_owned_fixture(pid: int) -> None:
    if fixture_pid() != pid:
        raise RuntimeError("fixture PID changed since RN start; refusing to stop it")
    # provider.stop() can wait up to 15 seconds before killing the process.
    subprocess.run(
        [sys.executable, "-m", "tests.peripherals.nrf52840.provider", "stop"],
        cwd=PROJECT_ROOT,
        check=True,
        timeout=25,
    )


def interrupt_on_sigterm(_signum: int, _frame: FrameType | None) -> None:
    raise InterruptedError("RN iOS RF scan interrupted by SIGTERM")


COORDINATION_EXIT_GRACE_SECONDS = 5.0


def check_runtime_wait(
    log_path: Path,
    runtime: subprocess.Popen[str],
    marker: str,
    progress: ProgressReporter | None,
    *,
    allow_zero_exit: bool = False,
) -> bool:
    """Compatibility wrapper retaining the iOS runner's patchable API."""
    return check_runtime_process(
        log_path,
        runtime,
        marker,
        progress,
        allow_zero_exit=allow_zero_exit,
    )


def wait_for_log_marker(
    log_path: Path,
    marker: str,
    runtime: subprocess.Popen[str],
    timeout: float,
    occurrence: int = 1,
    *,
    progress: ProgressReporter | None = None,
) -> None:
    """Compatibility wrapper retaining local clock control for tests."""
    wait_for_runtime_log_marker(
        log_path,
        marker,
        runtime,
        timeout,
        occurrence,
        progress=progress,
        clock=time.monotonic,
        sleeper=time.sleep,
    )


def wait_for_coordination_marker(
    path: Path,
    timeout: float,
    *,
    runtime: subprocess.Popen[str],
    log_path: Path,
    progress: ProgressReporter | None = None,
) -> None:
    phase = f"coordination:{path.name}"
    if progress is not None:
        progress.phase_started(phase)
    deadline = time.monotonic() + timeout
    exited_at: float | None = None
    while time.monotonic() < deadline:
        runtime_exited = check_runtime_wait(
            log_path, runtime, phase, progress, allow_zero_exit=True
        )
        if path.is_file():
            if progress is not None:
                progress.observed(phase)
                progress.phase_passed(phase)
            return
        if runtime_exited:
            if exited_at is None:
                exited_at = time.monotonic()
            elif time.monotonic() - exited_at >= COORDINATION_EXIT_GRACE_SECONDS:
                if progress is not None:
                    progress.failed(f"runtime-exit-before:{phase}")
                raise RuntimeError(
                    f"runtime exited before coordination marker {path}; inspect {log_path}"
                )
        if progress is not None:
            progress.waiting(phase)
        time.sleep(0.2)
    if progress is not None:
        progress.failed(f"timeout:{phase}")
    raise TimeoutError(f"timed out waiting for nRF coordination marker {path}")


def wait_for_runtime_exit(
    runtime: subprocess.Popen[str],
    timeout: float,
    *,
    progress: ProgressReporter | None = None,
) -> None:
    """Compatibility wrapper retaining local clock control for tests."""
    wait_for_process_exit(
        runtime,
        timeout,
        progress=progress,
        clock=time.monotonic,
        sleeper=time.sleep,
    )


def run(
    udid: str,
    *,
    connect: bool = False,
    gatt: bool = False,
    io: bool = False,
    rssi: bool = False,
    subscription: str | None = None,
    passive_disconnect: bool = False,
    cancellation: bool = False,
    recovery: bool = False,
    rf_reload: bool = False,
    suite_flow: str | None = None,
) -> None:
    if cancellation or suite_flow in ("rf-cancellation", "rf-watchdog"):
        from tests.peripherals.nrf52840.direct_fixture import require_v2_coordination

        require_v2_coordination("legacy-suite-runner")
    provider.WORK_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        LOCK.mkdir()  # Atomic among RN gates; other Apple gates do not use this lock.
    except FileExistsError as error:
        raise RuntimeError(f"RN iOS RF scan is already running ({LOCK})") from error
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    try:
        if rssi and (
            gatt
            or io
            or subscription is not None
            or passive_disconnect
            or cancellation
            or recovery
            or rf_reload
        ):
            raise ValueError("connected RSSI is a separate RF gate")
        recovery_prefix = "rf-recovery"
        if subscription not in (None, "notification", "indication"):
            raise ValueError(f"unsupported RN RF subscription mode: {subscription}")
        if suite_flow is not None and suite_flow not in {
            "rf",
            "rf-connect",
            "rf-gatt",
            "rf-io",
            "rf-rssi",
            "rf-notification",
            "rf-indication",
            "rf-passive-disconnect",
            "rf-cancellation",
        }:
            raise ValueError(f"unsupported RF Lab suite flow: {suite_flow}")
        if rf_reload and (
            passive_disconnect
            or cancellation
            or recovery
            or io
            or subscription is not None
        ):
            raise ValueError("RF reload is a separate Notification gate")
        if passive_disconnect and (cancellation or recovery):
            raise ValueError(
                "passive disconnect, cancellation, and recovery are separate gates"
            )
        if cancellation and (
            passive_disconnect or recovery or io or subscription is not None
        ):
            raise ValueError("cancellation is a separate GATT gate")
        if (passive_disconnect or recovery) and (
            io or subscription not in (None, "notification")
        ):
            raise ValueError(
                "passive disconnect/recovery uses the notification subscription flow"
            )
        if passive_disconnect or recovery or rf_reload:
            subscription = "notification"
            connect = True
            gatt = True
        elif cancellation or io or subscription is not None:
            connect = True
            gatt = True
        elif rssi:
            connect = True
        signal.signal(signal.SIGTERM, interrupt_on_sigterm)
        if provider.PID_FILE.exists():
            raise RuntimeError(
                f"nRF fixture PID already exists ({provider.PID_FILE}); "
                "do not stop it here, even if stale. Inspect and clean up manually"
            )

        attempted = False
        owned_pid = None
        coordination_path: Path | None = None
        runtime_process: subprocess.Popen[str] | None = None
        try:
            attempted = True
            stem = (
                "device-rf-reload"
                if rf_reload
                else f"device-{recovery_prefix}"
                if recovery
                else "device-rf-passive-disconnect"
                if passive_disconnect
                else "device-rf-cancellation"
                if cancellation
                else f"device-rf-{subscription}"
                if subscription is not None
                else "device-rf-io"
                if io
                else "device-rf-rssi"
                if rssi
                else "device-rf-gatt"
                if gatt
                else "device-rf-connect"
                if connect
                else "device-rf-scan"
            )
            if subscription is not None or passive_disconnect or rf_reload:
                coordination_path = HOST_BUILD / f"{stem}-coordination"
                shutil.rmtree(coordination_path, ignore_errors=True)
            progress = ProgressReporter(stem, prefix="rn_ios_rf")
            # A short-lived controller must start the detached fixture. Keeping
            # its Popen parent alive here can leave an exited child in this process
            # group while a separate stop controller waits for it to disappear.
            start_command = [
                sys.executable,
                "-m",
                "tests.peripherals.nrf52840.provider",
                "direct-start",
            ]
            if coordination_path is not None:
                start_command.extend(
                    [
                        "--coordination-path",
                        str(coordination_path),
                        "--coordination-mode",
                        "host-close"
                        if rf_reload
                        else "recovery"
                        if recovery
                        else "passive-disconnect"
                        if passive_disconnect
                        else cast(str, subscription),
                    ]
                )
            progress.phase_started("fixture-start")
            subprocess.run(
                start_command,
                cwd=PROJECT_ROOT,
                check=True,
                timeout=provider.DEFAULT_READY_TIMEOUT + 15,
            )
            progress.phase_passed("fixture-start")
            owned_pid = fixture_pid()
            if owned_pid is None:
                raise RuntimeError(f"direct-start returned without {provider.PID_FILE}")
            progress.phase_started("install")
            subprocess.run(
                [
                    "pixi",
                    "run",
                    "-e",
                    "apple-ci",
                    "xcrun",
                    "devicectl",
                    "device",
                    "install",
                    "app",
                    "--device",
                    udid,
                    "--json-output",
                    str(HOST_BUILD / "device-rf-install.json"),
                    str(APP),
                ],
                cwd=PROJECT_ROOT,
                check=True,
                timeout=120,
            )
            progress.phase_passed("install")
            log = HOST_BUILD / f"{stem}.log"
            attempt_log = HOST_BUILD / f"{stem}.attempt.log"
            attempt_log.unlink(missing_ok=True)
            runtime_command = [
                sys.executable,
                "-m",
                "tools.react_native.run_ios_device_runtime",
                udid,
                str(attempt_log),
                "--seconds",
                "450" if recovery or rf_reload else "210",
                "--rf-lab-suite-service-uuid"
                if suite_flow is not None
                else "--rf-reload-service-uuid"
                if rf_reload
                else "--rf-recovery-service-uuid"
                if recovery
                else "--rf-passive-disconnect-service-uuid"
                if passive_disconnect
                else "--rf-cancellation-service-uuid"
                if cancellation
                else "--rf-indication-service-uuid"
                if subscription == "indication"
                else "--rf-notification-service-uuid"
                if subscription == "notification"
                else "--rf-io-service-uuid"
                if io
                else "--rf-rssi-service-uuid"
                if rssi
                else "--rf-gatt-service-uuid"
                if gatt
                else "--rf-connect-service-uuid"
                if connect
                else "--rf-service-uuid",
                SERVICE_UUID,
            ]
            if suite_flow is not None:
                runtime_command.extend(["--rf-lab-suite-flow", suite_flow])
            if subscription is not None or recovery:
                progress.phase_started("runtime-launch")
                runtime_process = subprocess.Popen(
                    runtime_command,
                    cwd=PROJECT_ROOT,
                    text=True,
                )
                progress.phase_passed("runtime-launch")
                assert coordination_path is not None
                if rf_reload:
                    first = coordination_path / "generation-0"
                    second = coordination_path / "generation-1"
                    wait_for_log_marker(
                        attempt_log,
                        "rf-reload:notification-enable:passed:",
                        runtime_process,
                        300,
                        progress=progress,
                    )
                    first.mkdir(parents=True, exist_ok=True)
                    (first / "subscription-enabled").write_text(
                        "ready\n", encoding="ascii"
                    )
                    wait_for_coordination_marker(
                        first / "value-applied",
                        90,
                        runtime=runtime_process,
                        log_path=attempt_log,
                        progress=progress,
                    )
                    wait_for_log_marker(
                        attempt_log,
                        "rf-reload:active:",
                        runtime_process,
                        300,
                        progress=progress,
                    )
                    (first / "active").write_text("ready\n", encoding="ascii")
                    wait_for_coordination_marker(
                        first / "armed",
                        90,
                        runtime=runtime_process,
                        log_path=attempt_log,
                        progress=progress,
                    )
                    wait_for_log_marker(
                        attempt_log,
                        "rf-reload:native-triggered",
                        runtime_process,
                        90,
                        progress=progress,
                    )
                    wait_for_log_marker(
                        attempt_log,
                        "rf-reload:cleanup-started",
                        runtime_process,
                        90,
                        progress=progress,
                    )

                    wait_for_coordination_marker(
                        first / "host-disconnected",
                        90,
                        runtime=runtime_process,
                        log_path=attempt_log,
                        progress=progress,
                    )
                    wait_for_log_marker(
                        attempt_log,
                        "rf-reload:notification-enable:passed:",
                        runtime_process,
                        300,
                        occurrence=2,
                        progress=progress,
                    )
                    second.mkdir(parents=True, exist_ok=True)
                    (second / "subscription-enabled").write_text(
                        "ready\n", encoding="ascii"
                    )
                    wait_for_coordination_marker(
                        second / "value-applied",
                        90,
                        runtime=runtime_process,
                        log_path=attempt_log,
                        progress=progress,
                    )
                    wait_for_log_marker(
                        attempt_log,
                        "rf-reload:active:",
                        runtime_process,
                        300,
                        occurrence=2,
                        progress=progress,
                    )
                elif recovery:
                    for cycle in range(3):
                        directory = coordination_path / f"cycle-{cycle}"
                        wait_for_coordination_marker(
                            directory / "fixture-ready",
                            90,
                            runtime=runtime_process,
                            log_path=attempt_log,
                            progress=progress,
                        )
                        wait_for_log_marker(
                            attempt_log,
                            f"{recovery_prefix}:cycle:{cycle}:notification-enable:passed:",
                            runtime_process,
                            450,
                            progress=progress,
                        )
                        (directory / "subscription-enabled").write_text(
                            "ready\n", encoding="ascii"
                        )
                        wait_for_coordination_marker(
                            directory / "value-applied",
                            90,
                            runtime=runtime_process,
                            log_path=attempt_log,
                            progress=progress,
                        )
                        wait_for_log_marker(
                            attempt_log,
                            f"{recovery_prefix}:cycle:{cycle}:notification-value:passed:",
                            runtime_process,
                            450,
                            progress=progress,
                        )
                        (directory / "connected").write_text(
                            "ready\n", encoding="ascii"
                        )
                        wait_for_coordination_marker(
                            directory / "fault-triggered",
                            90,
                            runtime=runtime_process,
                            log_path=attempt_log,
                            progress=progress,
                        )
                        wait_for_log_marker(
                            attempt_log,
                            f"{recovery_prefix}:cycle:{cycle}:passive-disconnect:passed:",
                            runtime_process,
                            450,
                            progress=progress,
                        )
                        (directory / "passive-disconnected").write_text(
                            "ready\n", encoding="ascii"
                        )
                        wait_for_coordination_marker(
                            directory / "recovery-ready",
                            90,
                            runtime=runtime_process,
                            log_path=attempt_log,
                            progress=progress,
                        )
                        wait_for_log_marker(
                            attempt_log,
                            f"{recovery_prefix}:cycle:{cycle}:cycle:passed:",
                            runtime_process,
                            450,
                            progress=progress,
                        )
                        (directory / "cycle-passed").write_text(
                            "ready\n", encoding="ascii"
                        )
                elif passive_disconnect:
                    wait_for_log_marker(
                        attempt_log,
                        "rf:step:notification-enable:passed:",
                        runtime_process,
                        210,
                        progress=progress,
                    )
                    (coordination_path / "subscription-enabled").write_text(
                        "ready\n", encoding="ascii"
                    )
                    wait_for_coordination_marker(
                        coordination_path / "value-applied",
                        60,
                        runtime=runtime_process,
                        log_path=attempt_log,
                        progress=progress,
                    )
                    wait_for_log_marker(
                        attempt_log,
                        "rf:step:notification-value:passed:",
                        runtime_process,
                        210,
                        progress=progress,
                    )
                    (coordination_path / "connected").write_text(
                        "ready\n", encoding="ascii"
                    )
                    wait_for_coordination_marker(
                        coordination_path / "fault-triggered",
                        60,
                        runtime=runtime_process,
                        log_path=attempt_log,
                        progress=progress,
                    )
                    wait_for_log_marker(
                        attempt_log,
                        "rf:step:disconnected:passed:",
                        runtime_process,
                        210,
                        progress=progress,
                    )
                    (coordination_path / "passive-disconnected").write_text(
                        "ready\n", encoding="ascii"
                    )
                    wait_for_coordination_marker(
                        coordination_path / "recovery-ready",
                        60,
                        runtime=runtime_process,
                        log_path=attempt_log,
                        progress=progress,
                    )
                else:
                    wait_for_log_marker(
                        attempt_log,
                        f"rf:step:{subscription}-enable:passed:",
                        runtime_process,
                        210,
                        progress=progress,
                    )
                    (coordination_path / "subscription-enabled").write_text(
                        "ready\n", encoding="ascii"
                    )
                    wait_for_coordination_marker(
                        coordination_path / "value-applied",
                        60,
                        runtime=runtime_process,
                        log_path=attempt_log,
                        progress=progress,
                    )
                    wait_for_log_marker(
                        attempt_log,
                        f"rf:step:{subscription}-disable:passed:",
                        runtime_process,
                        210,
                        progress=progress,
                    )
                    (coordination_path / "subscription-disabled").write_text(
                        "ready\n", encoding="ascii"
                    )
                    wait_for_coordination_marker(
                        coordination_path / "post-disable-triggered",
                        60,
                        runtime=runtime_process,
                        log_path=attempt_log,
                        progress=progress,
                    )
                progress.phase_started("runtime-exit")
                wait_for_runtime_exit(
                    runtime_process,
                    475 if recovery or rf_reload else 235,
                    progress=progress,
                )
                runtime_process = None
                progress.phase_passed("runtime-exit")

                if rf_reload:
                    # Only merge host ACKs after the console writer exits. The
                    # fixture disconnected before the runner enabled generation 1;
                    # do not invent an earlier ordering at generation 0 cleanup.
                    lines = attempt_log.read_text(encoding="utf-8").splitlines(
                        keepends=True
                    )
                    enables = [
                        index
                        for index, line in enumerate(lines)
                        if "BLEHUB_NITRO_RUNTIME rf-reload:notification-enable:passed:"
                        in line
                    ]
                    if len(enables) != 2:
                        raise RuntimeError(
                            "RF reload notification-enable markers missing or duplicated"
                        )
                    lines.insert(
                        enables[1],
                        "BLEHUB_NITRO_RUNTIME rf-reload:fixture-host-disconnected\n",
                    )
                    attempt_log.write_text(
                        "".join(lines)
                        + "BLEHUB_NITRO_RUNTIME rf-reload:host-complete:passed\n",
                        encoding="utf-8",
                    )
            else:
                progress.phase_started("runtime")
                subprocess.run(
                    runtime_command,
                    cwd=PROJECT_ROOT,
                    check=True,
                    timeout=235,
                )
                progress.phase_passed("runtime")
            progress.phase_started("result-check")
            subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "tools/react_native/check_runtime_log.py"),
                    str(attempt_log),
                    "--rf-reload-service-uuid"
                    if rf_reload
                    else "--rf-recovery-service-uuid"
                    if recovery
                    else "--rf-passive-disconnect-service-uuid"
                    if passive_disconnect
                    else "--rf-cancellation-service-uuid"
                    if cancellation
                    else "--rf-indication-service-uuid"
                    if subscription == "indication"
                    else "--rf-notification-service-uuid"
                    if subscription == "notification"
                    else "--rf-io-service-uuid"
                    if io
                    else "--rf-rssi-service-uuid"
                    if rssi
                    else "--rf-gatt-service-uuid"
                    if gatt
                    else "--rf-connect-service-uuid"
                    if connect
                    else "--rf-service-uuid",
                    SERVICE_UUID,
                ],
                cwd=PROJECT_ROOT,
                check=True,
                timeout=30,
            )
            progress.phase_passed("result-check")
            attempt_log.replace(log)
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if runtime_process is not None and runtime_process.poll() is None:
                runtime_process.terminate()
                try:
                    runtime_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    runtime_process.kill()
                    _ = runtime_process.wait()
            if coordination_path is not None:
                shutil.rmtree(coordination_path, ignore_errors=True)
            primary_error = sys.exc_info()[1]
            if attempted and provider.PID_FILE.exists():
                try:
                    # direct_start may fail after writing the PID but before readiness.
                    if owned_pid is None:
                        owned_pid = fixture_pid()
                    if owned_pid is not None:
                        stop_owned_fixture(owned_pid)
                except Exception as error:
                    print(
                        f"RN fixture cleanup failed: {error}; inspect "
                        f"{provider.PID_FILE} and {provider.LOG_FILE} manually",
                        file=sys.stderr,
                    )
                    if primary_error is None:
                        raise
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            LOCK.rmdir()
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("udid")
    _ = parser.add_argument("--connect", action="store_true")
    _ = parser.add_argument("--gatt", action="store_true")
    _ = parser.add_argument("--io", action="store_true")
    _ = parser.add_argument("--rssi", action="store_true")
    _ = parser.add_argument("--passive-disconnect", action="store_true")
    _ = parser.add_argument("--cancellation", action="store_true")
    _ = parser.add_argument("--recovery", action="store_true")

    _ = parser.add_argument("--rf-reload", action="store_true")
    subscription = parser.add_mutually_exclusive_group()
    _ = subscription.add_argument(
        "--notification",
        action="store_const",
        const="notification",
        dest="subscription",
    )
    _ = subscription.add_argument(
        "--indication", action="store_const", const="indication", dest="subscription"
    )
    args = parser.parse_args()
    udid = cast(str, args.udid)
    connect = cast(bool, args.connect)
    gatt = cast(bool, args.gatt)
    io = cast(bool, args.io)
    rssi = cast(bool, args.rssi)
    passive_disconnect = cast(bool, args.passive_disconnect)
    cancellation = cast(bool, args.cancellation)
    recovery = cast(bool, args.recovery)
    rf_reload = cast(bool, args.rf_reload)
    subscription_mode = cast(
        Literal["notification", "indication"] | None, args.subscription
    )
    if (
        gatt
        or io
        or rssi
        or subscription_mode is not None
        or passive_disconnect
        or cancellation
        or recovery
    ) and not connect:
        connect = True
    run(
        udid,
        connect=connect,
        gatt=gatt,
        io=io,
        rssi=rssi,
        subscription=subscription_mode,
        passive_disconnect=passive_disconnect,
        cancellation=cancellation,
        recovery=recovery,
        rf_reload=rf_reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
