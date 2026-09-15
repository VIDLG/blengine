"""Isolated AutoPTS process-boundary spike.

This is deliberately only lifecycle/configuration glue. It does not import
AutoPTS or nrftest and is not an active nRF provider.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self


class AutoPtsBoundaryError(RuntimeError):
    """Raised when the external AutoPTS boundary is unsafe to use."""


@dataclass(frozen=True)
class AutoPtsConfig:
    checkout: Path
    command: tuple[str, ...]
    ready_marker: str = "BTP_READY"
    stop_timeout: float = 5.0

    def validate(self) -> None:
        if not self.checkout.is_dir():
            raise AutoPtsBoundaryError(
                f"AutoPTS checkout is not a directory: {self.checkout}"
            )
        if not self.command:
            raise AutoPtsBoundaryError("AutoPTS command must not be empty")
        if not self.ready_marker:
            raise AutoPtsBoundaryError("AutoPTS ready marker must not be empty")
        if self.stop_timeout <= 0:
            raise AutoPtsBoundaryError("AutoPTS stop timeout must be positive")


class Readiness:
    """Consume process output without treating process existence as readiness."""

    def __init__(self, marker: str) -> None:
        if not marker:
            raise AutoPtsBoundaryError("readiness marker must not be empty")
        self.marker = marker
        self.ready = False
        self.failure: str | None = None

    def feed(self, line: str) -> None:
        if self.ready or self.failure is not None:
            return
        if line.startswith("BTP_FAILURE "):
            self.failure = (
                line.removeprefix("BTP_FAILURE ").strip() or "unknown failure"
            )
        elif line.startswith(self.marker):
            self.ready = True

    def require_ready(self) -> None:
        if self.failure is not None:
            raise AutoPtsBoundaryError(
                f"AutoPTS failed before readiness: {self.failure}"
            )
        if not self.ready:
            raise AutoPtsBoundaryError("AutoPTS has not reached BTP/profile readiness")


class DeviceLock:
    """Small exclusive lock for one physical nRF/AutoPTS resource."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._owned = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise AutoPtsBoundaryError(
                f"resource is already locked: {self.path}"
            ) from error
        os.close(descriptor)
        self._owned = True

    def release(self) -> None:
        if self._owned:
            self.path.unlink(missing_ok=True)
            self._owned = False

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class ProcessLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class AutoPtsProcess:
    """Own one external AutoPTS process group with bounded shutdown."""

    def __init__(self, config: AutoPtsConfig) -> None:
        config.validate()
        self.config = config
        self.process: ProcessLike | None = None
        self.closed = False

    def start(self) -> ProcessLike:
        if self.closed:
            raise AutoPtsBoundaryError("AutoPTS boundary is closed")
        if self.process is not None and self.process.poll() is None:
            raise AutoPtsBoundaryError("AutoPTS process is already running")
        self.process = subprocess.Popen(
            list(self.config.command),
            cwd=self.config.checkout,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return self.process

    def stop(self) -> None:
        if self.closed:
            return
        self.closed = True
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=self.config.stop_timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=self.config.stop_timeout)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
