"""The one place this repository builds and runs an adb command.

Android tooling needs a canonical argv, a bounded run with stderr merged into stdout, and
a typed failure that says the device command never produced an answer. `tools.host_command`
owns process mechanics; this module owns the adb shape (`-s <serial> ...`) and the Android
failure vocabulary on top of it.

Keeping that in one module means a test can replace the device boundary once
(`adb.run_command`) instead of faking several subprocess call sites, and a fix (deadline
handling, encoding, argv shape) lands everywhere at once.

This module owns mechanics only: no ownership, no receipts, no policy, and no knowledge of
what a command means. Callers keep their own budgets, evidence and refusal rules.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools import host_command
from tools.android_toolchain import adb_executable

PROJECT_ROOT = host_command.PROJECT_ROOT


class AdbError(host_command.HostCommandError):
    """Raised when an adb command cannot start safely."""


def adb_command(serial: str, *arguments: str) -> list[str]:
    """The canonical argv for one command against one explicit device."""
    return [str(adb_executable()), "-s", serial, *arguments]


def run_command(
    command: list[str],
    *,
    cwd: Path | None = PROJECT_ROOT,
    timeout: float,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one adb command with a finite budget and stderr merged into stdout.

    A command that cannot start becomes `AdbError`; a command that did not finish inside
    its budget raises `subprocess.TimeoutExpired` carrying whatever output it produced, so
    callers keep the real failure and its partial evidence.
    """
    try:
        return host_command.run(command, cwd=cwd, timeout=timeout, check=check)
    except host_command.HostCommandError as error:
        raise AdbError(str(error)) from error.__cause__


def start_command(
    command: list[str], *, cwd: Path | None = PROJECT_ROOT
) -> subprocess.Popen[bytes]:
    """Start a long-running adb command whose output the caller streams.

    Instrumentation runs for minutes, so it cannot use `run_command`; it still shares this
    module's argv shape, merged stderr and typed start failure.
    """
    try:
        return host_command.start(command, cwd=cwd)
    except host_command.HostCommandError as error:
        raise AdbError(str(error)) from error.__cause__


__all__ = [
    "PROJECT_ROOT",
    "AdbError",
    "adb_command",
    "run_command",
    "start_command",
]
