"""One declared link-level fault for the multi-link scene, driven from the host's fixture.

The App cannot see the host's fault engine, so "this link was refused, the other was not" can
only be proven by the host refusing a write the App actually made. The hold is armed as soon as
the link is up — before discovery, hence before the first write — and resolved as soon as the
firmware reports that it captured the exact request. The receipt is fixture evidence: it says
what the peer's engine did, never what the App received.
"""

from __future__ import annotations

from typing import Any

from tests.peripherals.nrf52840.direct_fixture import DirectFixture
from tests.peripherals.nrf52840.fault_control import (
    Decision,
    Operation,
    Phase,
    injected_reject,
)

from .bleng_multi_link_contract import FAULT_WRITE_REJECT, READ_WRITE_ROLE


class MultiLinkFaultError(RuntimeError):
    """Raised when a declared fault cannot be armed, captured or resolved as declared."""


class WriteRejectFault:
    """A write the host refuses on one peer, with the peer's own receipt for it.

    `arm()` is called once that peer's link is up; `service()` is called from the scene's own
    poll and resolves the hold exactly once, the first time the firmware reports it captured.
    """

    def __init__(self, fixture: DirectFixture, *, payload: bytes) -> None:
        if type(payload) is not bytes or not payload:
            raise ValueError("a refused write needs the payload the App will attempt")
        self._fixture = fixture
        self._payload = payload
        self._hold = None
        self._resolved = False
        self._refused = False
        self.receipt: Any = None

    def arm(self) -> None:
        if self._hold is not None:
            raise MultiLinkFaultError("this peer's fault is already armed")
        self._hold = self._fixture.arm_hold(
            Operation.RESPONSE_WRITE, READ_WRITE_ROLE, self._payload
        )

    def service(self) -> None:
        """Reject the held write once, as soon as the firmware reports it captured."""
        if self._hold is None or self._resolved:
            return
        phase = self._fixture.hold_phase(self._hold)
        if phase is Phase.ARMED:
            return
        if phase is not Phase.HELD:
            raise MultiLinkFaultError(
                f"the armed write left ARMED without being held: {phase.name}"
            )
        self.receipt = self._fixture.release_hold(self._hold, Decision.REJECT)
        self._refused = injected_reject(self.receipt)
        self._resolved = True
        if not self._refused:
            raise MultiLinkFaultError(
                "the peer did not produce a refused receipt for the held write"
            )

    @property
    def resolved(self) -> bool:
        return self._resolved

    def evidence(self) -> dict[str, Any]:
        """What the peer's engine reported about the refusal, never an App observation."""
        status = self.receipt
        if status is None:
            return {"kind": FAULT_WRITE_REJECT, "resolved": False}
        return {
            "kind": FAULT_WRITE_REJECT,
            "resolved": True,
            "refused": self._refused,
            "winner": getattr(getattr(status, "winner", None), "name", None),
            "outcome": getattr(getattr(status, "outcome", None), "name", None),
        }
