"""Run a bounded React Native Android runtime/lifecycle gate."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

from tools.android_toolchain import adb_executable
from tools.react_native.rf_runner import (
    ProgressReporter,
    runtime_failure,
    runtime_markers,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = "com.blehubnitrohost"
ACTIVITY = f"{PACKAGE_NAME}/.MainActivity"
DEFAULT_TIMEOUT_SECONDS = 60.0
HEARTBEAT_SECONDS = 12.0


class AndroidRuntimeError(RuntimeError):
    """Raised when the bounded Android runtime controller cannot complete safely."""


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
        raise AndroidRuntimeError(
            f"command failed to start or finish: {' '.join(command)}"
        ) from error
    if check and result.returncode:
        raise AndroidRuntimeError(
            f"command failed (exit={result.returncode}): {' '.join(command)}; {result.stdout[-1500:]}"
        )
    return result


def _logcat(serial: str) -> str:
    return _run(
        _adb(serial, "logcat", "-d", "-v", "brief"), timeout=15, check=True
    ).stdout


def _mode_contract(mode: str) -> tuple[str, str, str | None]:
    if mode == "basic":
        return "close:resolved", "basic", None
    if mode == "stress":
        return "stress:complete:passed", "stress", "--stress"
    if mode == "reload":
        return "reload:ready", "reload", "--reload"
    raise AndroidRuntimeError(f"unsupported Android runtime mode: {mode}")


def _wait_for_runtime(
    serial: str,
    mode: str,
    log_path: Path,
    timeout_seconds: float,
) -> None:
    completion_marker, scenario, _ = _mode_contract(mode)
    progress = ProgressReporter(
        scenario,
        prefix="rn_android_runtime",
        heartbeat_seconds=HEARTBEAT_SECONDS,
    )
    started = time.monotonic()
    deadline = started + timeout_seconds
    phase = "launch"
    seen: set[str] = set()
    latest = "<none>"
    log = ""
    while time.monotonic() < deadline:
        log = _logcat(serial)
        _ = log_path.write_text(log, encoding="utf-8")
        markers = runtime_markers(log)
        if markers:
            latest = markers[-1]
            if latest not in seen:
                seen.add(latest)
                progress.marker(
                    latest, remaining=f"{max(0, int(deadline - time.monotonic()))}s"
                )
        failed_line = runtime_failure(log)
        if failed_line is not None:
            progress.failed(f"runtime:{failed_line}")
            raise AndroidRuntimeError(
                f"Android RN {mode} failed: {failed_line}; see {log_path}"
            )
        if phase == "launch" and markers:
            phase = "runtime"
            progress.phase_passed("launch")
            progress.phase_started(phase)
        if completion_marker in log:
            progress.phase_passed("runtime", marker=completion_marker)
            return
        progress.waiting(f"phase:{phase}")
        _ = time.sleep(1.0)
    _ = log_path.write_text(log, encoding="utf-8")
    progress.failed(f"timeout:{phase}")
    raise AndroidRuntimeError(
        f"Android RN {mode} marker timed out; latest={latest}; see {log_path}"
    )


def _wait_for_reload(
    serial: str,
    log_path: Path,
    timeout_seconds: float,
) -> None:
    """Trigger RN's native context recreation after generation one is ready."""
    progress = ProgressReporter(
        "reload",
        prefix="rn_android_runtime",
        heartbeat_seconds=HEARTBEAT_SECONDS,
    )
    started = time.monotonic()
    deadline = started + timeout_seconds
    triggered = False
    last_ready_count = 0
    log = ""
    while time.monotonic() < deadline:
        log = _logcat(serial)
        _ = log_path.write_text(log, encoding="utf-8")
        failed_line = runtime_failure(log)
        if failed_line is not None:
            progress.failed(f"runtime:{failed_line}")
            raise AndroidRuntimeError(
                f"Android RN reload failed: {failed_line}; see {log_path}"
            )
        ready_count = log.count("reload:ready")
        if ready_count != last_ready_count:
            last_ready_count = ready_count
            progress.marker(
                f"reload:ready:generation={ready_count}",
                remaining=f"{max(0, int(deadline - time.monotonic()))}s",
            )
        if ready_count >= 1 and not triggered:
            trigger = _run(
                _adb(
                    serial,
                    "shell",
                    "am",
                    "start",
                    "-n",
                    ACTIVITY,
                    "--ez",
                    "blehub.triggerReload",
                    "true",
                ),
                timeout=20,
                check=True,
            )
            if any(text in trigger.stdout for text in ("Error:", "Exception")):
                raise AndroidRuntimeError(
                    f"unable to trigger RN reload: {trigger.stdout[-1000:]}"
                )
            triggered = True
            progress.phase_passed("generation-1", marker="reload:ready")
            progress.phase_started("generation-2")
        cleanup_count = log.count("reload:cleanup-started")
        if triggered and ready_count >= 2 and cleanup_count >= 1:
            progress.phase_passed("generation-2", marker="reload:ready")
            return
        progress.waiting(f"phase:{'generation-2' if triggered else 'generation-1'}")
        _ = time.sleep(1.0)
    _ = log_path.write_text(log, encoding="utf-8")
    progress.failed("timeout:reload")
    raise AndroidRuntimeError(
        f"Android RN reload marker timed out; ready_count={last_ready_count}; see {log_path}"
    )


def run(
    serial: str,
    mode: str,
    log_path: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Path:
    """Launch one Android runtime mode, stop on terminal/failure, and clean up."""
    if not serial:
        raise AndroidRuntimeError("Android device serial must not be empty")
    if timeout_seconds <= 0:
        raise AndroidRuntimeError("Android runtime timeout must be positive")
    completion_marker, _, checker_flag = _mode_contract(mode)
    del completion_marker
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.unlink(missing_ok=True)
    app_started = False
    primary_error: BaseException | None = None
    try:
        _ = _run(_adb(serial, "wait-for-device"), timeout=20, check=True)
        _ = _run(
            _adb(serial, "shell", "am", "force-stop", PACKAGE_NAME),
            timeout=15,
            check=True,
        )
        _ = _run(_adb(serial, "logcat", "-c"), timeout=15, check=True)
        command = _adb(
            serial,
            "shell",
            "am",
            "start",
            "-n",
            ACTIVITY,
            "--es",
            "blehub.runtimeMode",
            mode,
        )
        started = _run(command, timeout=20, check=True)
        if any(
            text in started.stdout
            for text in ("Error:", "Exception", "Activity not started")
        ):
            raise AndroidRuntimeError(
                f"unable to start RN host: {started.stdout[-1000:]}"
            )
        app_started = True
        if mode == "reload":
            _wait_for_reload(serial, log_path, timeout_seconds)
        else:
            _wait_for_runtime(serial, mode, log_path, timeout_seconds)
        checker = [
            sys.executable,
            "-m",
            "tools.react_native.check_runtime_log",
            str(log_path),
        ]
        if checker_flag is not None:
            checker.append(checker_flag)
        _ = _run(checker, timeout=30, check=True)
        print(f"Android RN runtime log: {log_path}")
        return log_path
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if app_started:
            try:
                _ = _run(
                    _adb(serial, "shell", "am", "force-stop", PACKAGE_NAME),
                    timeout=15,
                    check=True,
                )
            except AndroidRuntimeError:
                if primary_error is None:
                    raise


def main() -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("serial")
    _ = parser.add_argument(
        "--mode", choices=("basic", "stress", "reload"), default="basic"
    )
    _ = parser.add_argument("--log", type=Path)
    _ = parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    serial = cast(str, args.serial)
    mode = cast(str, args.mode)
    requested_log = cast(Path | None, args.log)
    timeout = cast(float, args.timeout)
    log_path = requested_log or (
        PROJECT_ROOT
        / "tests/consumers/react-native-internal/android/build"
        / f"runtime-{mode}.log"
    )
    _ = run(serial, mode, log_path, timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
