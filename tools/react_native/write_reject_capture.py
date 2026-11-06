"""Named response-write REJECT adapter over the bounded single-use transport."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tests.peripherals.nrf52840 import write_reject as protocol
from tools.react_native import write_reject_ledger
from tools.react_native.reject_capture import RejectCapture, capture_reject

PREFIX = protocol.PREFIX
MARKER_PREFIXES = ("rf-write-reject:", "rf-control:", "rf-host:")


class WriteRejectCapture(RejectCapture):
    """Response-write protocol and ledger remain owned by their named modules."""

    protocol = protocol
    scenario = "write-reject"
    android_control_property = "blehub.rfWriteRejectControl"

    def _new_relay(self) -> protocol.WriteRejectRelay:
        return protocol.WriteRejectRelay(self.root)

    def _before_control(self, phase: str) -> None:
        if phase == "close-allowed":
            protocol.require(
                self.facts is not None
                and "write-rejected" in self.facts["steps"]
                and "unchanged-read" in self.facts["steps"]
                and "recovery-read" in self.facts["steps"],
                "close control lacks native rejection/recovery",
            )

    def _verify_ledger(self) -> None:
        write_reject_ledger.verify(
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
    """Retain raw evidence and ordered SDK/native/JS ACKs; return text and ()."""
    return capture_reject(
        WriteRejectCapture,
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
