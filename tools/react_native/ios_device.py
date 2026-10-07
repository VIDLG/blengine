"""Shared CoreDevice preconditions for React Native iOS runners."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict, cast


class IosDevicePreconditionError(RuntimeError):
    """Raised when an iOS device cannot safely run a device gate."""


class _LockState(TypedDict):
    passcodeRequired: bool
    unlockedSinceBoot: bool


class _LockStateDocument(TypedDict):
    result: _LockState


def require_ready_unlocked_device(udid: str, *, cwd: Path) -> None:
    """Require a paired, available and currently unlocked physical device.

    ``devicectl list devices`` reports paired devices even while the user-facing
    SpringBoard is locked.  Device gates must query the explicit lock-state
    endpoint before installing or launching an app, otherwise a locked phone
    can consume the complete runtime timeout and leave a misleading result.
    """
    xcrun = shutil.which("xcrun")
    if xcrun is None:
        raise IosDevicePreconditionError("xcrun is required for the iOS device gate")

    listed = subprocess.run(
        [xcrun, "devicectl", "list", "devices"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if listed.returncode != 0:
        raise IosDevicePreconditionError(
            f"devicectl could not list iOS devices (exit {listed.returncode})"
        )
    device_lines = [line for line in listed.stdout.splitlines() if udid in line]
    if not any(
        state in line.split()
        for line in device_lines
        for state in ("available", "connected")
    ):
        raise IosDevicePreconditionError(
            f"iOS device {udid} is not available; connect, pair and unlock it"
        )

    lock_state = subprocess.run(
        [
            xcrun,
            "devicectl",
            "device",
            "info",
            "lockState",
            "--quiet",
            "--device",
            udid,
            "--json-output",
            "-",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if lock_state.returncode != 0:
        raise IosDevicePreconditionError(
            f"devicectl could not query iOS lock state (exit {lock_state.returncode})"
        )
    try:
        document = cast(_LockStateDocument, json.loads(lock_state.stdout))
        result = document["result"]
        passcode_required = result["passcodeRequired"]
        unlocked_since_boot = result["unlockedSinceBoot"]
    except (KeyError, TypeError, ValueError) as error:
        raise IosDevicePreconditionError(
            "devicectl returned an invalid iOS lock-state response"
        ) from error
    if passcode_required is not False or unlocked_since_boot is not True:
        raise IosDevicePreconditionError(
            f"iOS device {udid} is locked; unlock it before starting the RN gate"
        )
