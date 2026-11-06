"""Named CCC disable REJECT adapter; provider owns all eight protocol exchanges."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tests.peripherals.nrf52840 import ccc_disable_reject as protocol
from tools.react_native import ccc_disable_reject_ledger
from tools.react_native.reject_capture import RejectCapture, capture_reject

PREFIX = protocol.PREFIX
MARKER_PREFIXES = ("rf-ccc-disable-reject:", "rf-control:", "rf-host:")


class CccDisableRejectCapture(RejectCapture):
    """Flow 12 transport; provider validates the baseline without a fault ticket."""

    protocol = protocol
    scenario = "ccc-disable-reject"
    android_control_property = "blehub.rfCccDisableRejectControl"

    def _new_relay(self) -> protocol.CccDisableRejectRelay:
        return protocol.CccDisableRejectRelay(self.root)

    def _verify_ledger(self) -> None:
        ccc_disable_reject_ledger.verify(
            self.text(),
            platform=self.platform,
            run_id=self.run_id,
            service_uuid=self.service,
            physical_path=self.root / protocol.PHYSICAL_NAME,
            fixture_request=self.plan.request_path,
            fixture_identity=self.fixture_identity,
        )


def capture(
    platform: str,
    device: str,
    log: Path,
    coordination: Path,
    run_id: str,
    service_uuid: str,
    expected_local_name: str,
    *,
    timeout: float,
    guard: Callable[[], None],
    install_json: Path | None = None,
    fixture_identity: tuple[int, int],
) -> tuple[str, tuple[int, ...]]:
    """Return normalized CCC/control/host text and (), preserving original receipts."""
    return capture_reject(
        CccDisableRejectCapture,
        platform,
        device,
        log,
        coordination,
        run_id,
        service_uuid,
        expected_local_name,
        timeout=timeout,
        guard=guard,
        install_json=install_json,
        fixture_identity=fixture_identity,
    )
