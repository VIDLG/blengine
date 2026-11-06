"""Live provider relay for the v2 write/CCC system-loss protocol.

The host owns phone/UI mutation. This process owns only the leased DK BTP
session, FaultController, GAP observation and retained disconnect receipt.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from .direct_fixture import DirectFixture
from .fixture_session import FixtureSession
from .system_loss import (
    BINDING_NAME,
    CONTROL_NAME,
    PHYSICAL_NAME,
    RECEIPT_NAME,
    SystemLossCoordinator,
)

BASE_NAME = "system-loss-base-witness.json"
PENDING_NAME = "system-loss-pending-witness.json"
OFF_REQUEST_NAME = "system-loss-off-request.json"
FINISH_NAME = "system-loss-finish.json"
MAX_WAIT_SECONDS = 300.0


def _read_document(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"system-loss document is not an object: {path}")
    return value


def _wait(path: Path, *, timeout: float = MAX_WAIT_SECONDS) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = _read_document(path)
        if value is not None:
            return value
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for system-loss document: {path.name}")


def _wait_state(root: Path, state: str) -> dict[str, Any]:
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        value = _read_document(root / PHYSICAL_NAME)
        if value is not None and value.get("state") == state:
            return value
        time.sleep(0.1)
    raise TimeoutError(f"provider did not publish system-loss state {state}")


def run(
    fixture: DirectFixture,
    coordination_path: Path,
    handles: dict[str, int],
    *,
    on_ready: Callable[[], None],
) -> None:
    """Run one terminal provider attempt and retain the same fixture for recovery."""
    coordination_path.mkdir(parents=True, exist_ok=True)
    # DirectFixture.start() has already completed before this provider mode is
    # entered, so the Peripheral is advertising and safe for discovery. The
    # readiness marker must describe that live advertising boundary; it must not
    # wait for base/control, because the App needs RF discovery before it can
    # publish those documents. ARM remains a separate, later proof.
    on_ready()
    base = _wait(coordination_path / BASE_NAME)
    control = _wait(coordination_path / CONTROL_NAME)
    coordinator = SystemLossCoordinator(fixture, coordination_path, handles)
    try:
        coordinator.arm(base)
        pending = _wait(coordination_path / PENDING_NAME)
        coordinator.observe(pending)
        off_request = _wait(coordination_path / OFF_REQUEST_NAME)
        coordinator.authorize_off(off_request, pending)
        while coordinator.document["state"] != "disconnected-retired":
            coordinator.poll_disconnect(pending)
            time.sleep(0.05)
        _wait(coordination_path / FINISH_NAME)
    except BaseException as error:
        coordinator.finish_failure(error)
        raise
