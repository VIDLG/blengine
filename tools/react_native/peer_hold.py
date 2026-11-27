"""One ATT fault the peer (fixture) applies, owned by the host.

A hold is three facts, and each of them is a wire command: *arm* the firmware for one
operation on one characteristic, *prove* the firmware is holding the request this scene
submitted, then *resolve* it — commit it or refuse it. Keeping those three apart is what
makes a fault scene deterministic: the scene can only observe the App's own publications
while the peer is provably holding something.

Both in-process scene drivers use this seam (the iOS XCTest lane as one phase,
host actions), so it lives here rather than inside either driver. The scene decides what
a hold means; this class only performs it, re-running the caller's ownership guard around
every fixture command so a scene that lost fixture ownership stops before the next wire
command instead of acting on a lease it can no longer prove.
"""

from __future__ import annotations

from collections.abc import Callable

from tests.peripherals.nrf52840.direct_fixture import DirectFixture
from tests.peripherals.nrf52840.fault_control import ArmedHold, Decision, Operation


class PeerHold:
    """Arm, confirm and resolve one operation-level fault on the fixture."""

    def __init__(
        self,
        *,
        fixture: DirectFixture,
        role: str,
        operation: Operation,
        requested: bytes = b"",
        before: bytes | None = None,
        guard: Callable[[], None],
    ) -> None:
        if type(operation) is not Operation:
            raise TypeError("operation must be a fault_control.Operation")
        self._fixture = fixture
        self._role = role
        self._operation = operation
        self._requested = requested
        self._before = before
        self._guard = guard
        self._armed: ArmedHold | None = None
        self._held = False
        self._resolved = False
        self.receipt = None

    def arm(self) -> None:
        if self._armed is not None:
            raise RuntimeError("a peer hold is already armed for this scene")
        self._guard()
        self._armed = self._fixture.arm_hold(
            self._operation, self._role, self._requested, before=self._before
        )
        self._guard()

    def require_held(self) -> None:
        if self._armed is None or self._held or self._resolved:
            raise RuntimeError("require_held() needs the armed, unresolved hold")
        self._guard()
        self._fixture.require_hold_held(self._armed)
        self._guard()
        self._held = True

    def release(self) -> None:
        self._resolve(Decision.COMMIT)

    def reject(self) -> None:
        self._resolve(Decision.REJECT)

    def _resolve(self, decision: Decision) -> None:
        if self._armed is None or not self._held or self._resolved:
            raise RuntimeError("resolve() needs a held, unresolved request")
        self._guard()
        self.receipt = self._fixture.release_hold(self._armed, decision)
        self._guard()
        self._resolved = True


__all__ = ["PeerHold"]
