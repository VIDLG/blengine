"""The one place this repository runs a bounded host command.

Every tool here needs the same three things: a canonical argv, a finite budget, and a
typed failure that says the process never produced an answer. Platform layers (adb, the
iOS tooling, host SDK tools such as aapt2) add their own argv shape, receipts and policy
on top of this; they do not re-implement process creation or termination.

A non-zero exit is the tool's answer and is returned as-is unless `check` was requested.
A process that cannot start is `HostCommandError`; a process that did not finish inside
its budget raises `subprocess.TimeoutExpired`, which keeps any output it produced.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class HostCommandError(RuntimeError):
    """Raised when a host command cannot start safely."""


def run(
    command: list[str],
    *,
    cwd: Path | None = PROJECT_ROOT,
    timeout: float,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one command with a finite budget and stderr merged into stdout."""
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except OSError as error:
        raise HostCommandError(
            f"command failed to start: {' '.join(command)}"
        ) from error
    if check and result.returncode:
        raise HostCommandError(
            f"command failed (exit={result.returncode}): {' '.join(command)}; "
            f"{(result.stdout or '')[-1500:]}"
        )
    return result


def start(
    command: list[str], *, cwd: Path | None = PROJECT_ROOT
) -> subprocess.Popen[bytes]:
    """Start a long-running command whose output the caller streams."""
    try:
        return subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
    except OSError as error:
        raise HostCommandError(
            f"command failed to start: {' '.join(command)}"
        ) from error


def stop(process: subprocess.Popen, *, grace: float = 5.0) -> None:
    """Stop a command this host started, boundedly: TERM, a grace period, KILL, a wait.

    A command started by `start` leads its own process group, so both signals also reach the
    children it spawned (an SDK tool starts helpers); any other process is signalled alone.
    A process that survives SIGKILL for another grace period is reported, never waited on
    forever.
    """
    if process.poll() is not None:
        return
    _signal(process, signal.SIGTERM)
    if _exits(process, grace):
        return
    _signal(process, signal.SIGKILL)
    # The one wait that reports: a process still alive after SIGKILL is a real failure.
    process.wait(timeout=grace)


def _leads_own_group(process: subprocess.Popen) -> bool:
    pid = process.pid
    if os.name != "posix" or type(pid) is not int or pid <= 0:
        return False
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        return False


def _signal(process: subprocess.Popen, number: int) -> None:
    try:
        if _leads_own_group(process):
            os.killpg(process.pid, number)
        elif number == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except (OSError, ProcessLookupError, ValueError):
        pass  # it left between the poll and the signal, which is the wanted outcome


def _exits(process: subprocess.Popen, seconds: float) -> bool:
    try:
        process.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


__all__ = ["PROJECT_ROOT", "HostCommandError", "run", "start", "stop"]
