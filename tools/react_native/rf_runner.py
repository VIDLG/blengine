"""Shared, platform-neutral helpers for React Native RF runners."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO, final

RUNTIME_PREFIX = "BLEHUB_NITRO_RUNTIME"
DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_MULTI_RF_TIMEOUT_SECONDS = 180.0
MULTI_RF_SOAK_SECONDS_PER_CYCLE = 30.0
_RUNTIME_MARKER = re.compile(rf"{re.escape(RUNTIME_PREFIX)} [^\r\n]*")


def multi_rf_timeout_seconds(
    timeout_seconds: float | None,
    *,
    runtime_mode: str,
    soak_cycles: int,
) -> float:
    """Return one bounded budget for a multi-RF runner invocation."""
    if runtime_mode not in {"rf-multi", "rf-multi-soak"}:
        raise ValueError(f"unsupported multi RF runtime mode: {runtime_mode}")
    if soak_cycles < 1 or soak_cycles > 20:
        raise ValueError("multi RF soak cycles must be between 1 and 20")
    if timeout_seconds is not None:
        if timeout_seconds <= 0:
            raise ValueError("multi RF timeout must be positive")
        return timeout_seconds
    if runtime_mode == "rf-multi-soak":
        return max(
            DEFAULT_MULTI_RF_TIMEOUT_SECONDS,
            MULTI_RF_SOAK_SECONDS_PER_CYCLE * soak_cycles,
        )
    return DEFAULT_MULTI_RF_TIMEOUT_SECONDS


def runtime_markers(content: str) -> list[str]:
    """Return runtime markers in log order, without forwarding unrelated logs."""
    return [match.group(0) for match in _RUNTIME_MARKER.finditer(content)]


def runtime_failure(content: str) -> str | None:
    """Return the first structured runtime failure or failure marker line."""
    for line in content.splitlines():
        _, prefix, payload = line.partition(f"{RUNTIME_PREFIX} ")
        if prefix and (":failed:" in payload or ":failure:" in payload):
            return line.strip()
    return None


def check_runtime_process(
    log_path: Path,
    runtime: subprocess.Popen[str],
    marker: str,
    progress: ProgressReporter | None,
    *,
    allow_zero_exit: bool = False,
) -> bool:
    """Check a runtime log and process without hiding a structured failure."""
    content = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
    failed_line = runtime_failure(content)
    if failed_line is not None:
        if progress is not None:
            progress.failed(f"runtime:{failed_line}")
        raise RuntimeError(
            f"RN RF gate failed before {marker!r}: {failed_line}; inspect {log_path}"
        )
    return_code = runtime.poll()
    if return_code is not None:
        if return_code == 0 and allow_zero_exit:
            return True
        if progress is not None:
            progress.failed(f"runtime-exit:{return_code}")
        raise subprocess.CalledProcessError(return_code, runtime.args)
    return False


def wait_for_runtime_log_marker(
    log_path: Path,
    marker: str,
    runtime: subprocess.Popen[str],
    timeout: float,
    occurrence: int = 1,
    *,
    progress: ProgressReporter | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for a runtime marker while failing fast on process/log failures."""
    phase = marker.removeprefix(f"{RUNTIME_PREFIX} ").strip(":")
    if progress is not None:
        progress.phase_started(phase)
    deadline = clock() + timeout
    while clock() < deadline:
        content = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
        failed_line = runtime_failure(content)
        if failed_line is not None:
            if progress is not None:
                progress.failed(f"runtime:{failed_line}")
            raise RuntimeError(
                f"RN RF gate failed before {marker!r}: {failed_line}; inspect {log_path}"
            )
        if content.count(marker) >= occurrence:
            if progress is not None:
                progress.observed(f"{marker}#{occurrence}")
                progress.phase_passed(phase, occurrence=occurrence)
            return
        _ = check_runtime_process(log_path, runtime, marker, progress)
        if progress is not None:
            progress.waiting(f"log:{marker}")
        sleeper(0.2)
    if progress is not None:
        progress.failed(f"timeout:{phase}")
    raise TimeoutError(f"timed out waiting for RN marker {marker!r} in {log_path}")


def wait_for_process_exit(
    runtime: subprocess.Popen[str],
    timeout: float,
    *,
    progress: ProgressReporter | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for a runtime controller without hiding a post-marker hang."""
    deadline = clock() + timeout
    while clock() < deadline:
        return_code = runtime.poll()
        if return_code is not None:
            if return_code != 0:
                if progress is not None:
                    progress.failed(f"runtime-exit:{return_code}")
                raise subprocess.CalledProcessError(return_code, runtime.args)
            return
        if progress is not None:
            progress.waiting("runtime-exit")
        sleeper(0.2)
    if progress is not None:
        progress.failed(f"timeout:runtime-exit:{timeout:g}s")
    raise subprocess.TimeoutExpired(runtime.args, timeout)


@final
class ProgressReporter:
    """Emit sparse RF progress without forwarding build or runtime logs."""

    def __init__(
        self,
        scenario: str,
        *,
        prefix: str = "rn_rf",
        stream: TextIO | None = None,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.scenario: str = scenario
        self.prefix: str = prefix
        self.stream: TextIO = stream or sys.stdout
        self.heartbeat_seconds: float = heartbeat_seconds
        self.clock: Callable[[], float] = clock
        self.started_at: float = clock()
        self.next_heartbeat: float = self.started_at + heartbeat_seconds
        self.last_marker: str = "none"
        self.phase_started_at: float = self.started_at

    def _emit(self, **fields: object) -> None:
        details = " ".join(f"{key}={value}" for key, value in fields.items())
        print(
            f"{self.prefix} scenario={self.scenario} {details}",
            file=self.stream,
            flush=True,
        )

    def phase_started(self, phase: str, **fields: object) -> None:
        self.phase_started_at = self.clock()
        self._emit(phase=phase, status="started", **fields)

    def phase_passed(self, phase: str, **fields: object) -> None:
        self._emit(
            phase=phase,
            status="passed",
            phase_elapsed=f"{self.clock() - self.phase_started_at:.0f}s",
            **fields,
        )

    def observed(self, marker: str) -> None:
        self.last_marker = marker

    def marker(self, marker: str, **fields: object) -> None:
        """Emit a newly observed runtime marker immediately."""
        self.observed(marker)
        self._emit(marker=marker, **fields)

    def waiting(self, marker: str, **fields: object) -> None:
        now = self.clock()
        if now < self.next_heartbeat:
            return
        self._emit(
            waiting=marker,
            elapsed=f"{now - self.started_at:.0f}s",
            phase_elapsed=f"{now - self.phase_started_at:.0f}s",
            last_marker=self.last_marker,
            **fields,
        )
        self.next_heartbeat = now + self.heartbeat_seconds

    def failed(self, reason: str) -> None:
        self._emit(status="failed", reason=reason, last_marker=self.last_marker)
