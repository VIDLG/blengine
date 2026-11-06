"""Shared CoreDevice preconditions for React Native iOS runners."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import TypedDict, cast

from tools.react_native.ios_sdk import devicectl_list_devices, devicectl_lock_state


class IosDevicePreconditionError(RuntimeError):
    """Raised when an iOS device cannot safely run a device gate."""


class _LockState(TypedDict):
    passcodeRequired: bool
    unlockedSinceBoot: bool


class _LockStateDocument(TypedDict):
    result: _LockState


def canonical_physical_udid(device: str) -> str:
    """Normalize physical UDID case before evidence, SDK calls or lease hashing."""
    if type(device) is str:
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{16}", device):
            return device.upper()
        if re.fullmatch(r"[0-9a-fA-F]{40}", device):
            return device.lower()
    raise ValueError("an explicit physical iOS UDID is required")


def require_ready_unlocked_device(
    udid: str, *, cwd: Path, timeout: float = 60.0
) -> None:
    """Require a paired, available and currently unlocked physical device.

    ``devicectl list devices`` reports paired devices even while the user-facing
    SpringBoard is locked.  Device gates must query the explicit lock-state
    endpoint before installing or launching an app, otherwise a locked phone
    can consume the complete runtime timeout and leave a misleading result.
    Both queries share a host-monotonic ``timeout`` budget, with at most 30
    seconds per subprocess; even successful responses must arrive in budget.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise IosDevicePreconditionError(
            "iOS device timeout must be finite and positive"
        )
    started = time.monotonic()

    def remaining_timeout() -> float:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise IosDevicePreconditionError(
                "iOS device readiness exceeded its overall timeout budget"
            )
        return min(30.0, remaining)

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        command_timeout = remaining_timeout()
        try:
            return subprocess.run(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=command_timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise IosDevicePreconditionError(
                "devicectl timed out checking iOS device readiness"
            ) from error

    xcrun = shutil.which("xcrun")
    if xcrun is None:
        raise IosDevicePreconditionError("xcrun is required for the iOS device gate")

    listed = run(devicectl_list_devices())
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

    lock_state = run(devicectl_lock_state(udid))
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
    remaining_timeout()
