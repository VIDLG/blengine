"""Test-only BTP controller for the isolated Zephyr ATT read-hold experiment.

No transport is created here: the caller owns the single serial BTP session.
Firmware statuses are synchronization evidence, not Central ATT results.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol

GATT_SERVICE = 0x02
ARM = 0x24
RELEASE = 0x25
ABORT = 0x26
STATUS = 0x27
_ARM_REQUEST = struct.Struct("<H")
_IDENTITY = struct.Struct("<IH")
_STATUS = struct.Struct("<BIH")


class HoldError(RuntimeError):
    """The diagnostic firmware response or hold lifecycle is invalid."""


class HoldState(IntEnum):
    """States emitted by the opt-in Tester firmware patch."""

    IDLE = 0
    ARMED = 1
    WAITING = 2
    RELEASE_REQUESTED = 3
    ABORT_REQUESTED = 4
    RELEASED = 5
    ABORTED = 6
    EXPIRED = 7
    PREPARING = 8


@dataclass(frozen=True)
class Identity:
    """Firmware-issued one-shot sequence and the targeted GATT value handle."""

    sequence: int
    handle: int

    def __post_init__(self) -> None:
        if not 0 < self.sequence <= 0xFFFFFFFF or not 0 < self.handle <= 0xFFFF:
            raise HoldError("invalid ATT hold sequence or handle")

    def encode(self) -> bytes:
        """Encode the exact identity required by RELEASE and ABORT."""
        return _IDENTITY.pack(self.sequence, self.handle)


@dataclass(frozen=True)
class Snapshot:
    """A single firmware status response, not proof of a Central ATT result."""

    state: HoldState
    identity: Identity | None

    @classmethod
    def decode(cls, payload: bytes) -> Snapshot:
        """Reject truncated, unknown, or internally inconsistent status responses."""
        if len(payload) != _STATUS.size:
            raise HoldError("ATT hold status response has the wrong length")
        state_byte, sequence, handle = _STATUS.unpack(payload)
        try:
            state = HoldState(state_byte)
        except ValueError as error:
            raise HoldError(f"unknown ATT hold state: {state_byte}") from error
        if state == HoldState.IDLE:
            if sequence or handle:
                raise HoldError("idle ATT hold status has a nonzero identity")
            return cls(state, None)
        return cls(state, Identity(sequence, handle))


class Frame(Protocol):
    """Response fields exposed by the provider-owned BTP session."""

    service: int
    opcode: int
    index: int
    payload: bytes


class Session(Protocol):
    """Single-owner synchronous BTP session; never issue parallel requests."""

    def request(
        self,
        *,
        service: int,
        opcode: int,
        index: int = 0,
        payload: bytes = b"",
        timeout: float = 1.0,
    ) -> Frame: ...


class ReadHold:
    """Enforce one-shot correlation of ARM, WAITING, and release/abort.

    A caller must separately verify the Central command and its terminal result.
    Only use this controller after verifying the exact diagnostic firmware image.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._identity: Identity | None = None
        self._state: HoldState | None = None

    def _request(
        self, opcode: int, payload: bytes = b"", timeout: float = 1.0
    ) -> bytes:
        response = self._session.request(
            service=GATT_SERVICE, opcode=opcode, payload=payload, timeout=timeout
        )
        if response.service != GATT_SERVICE or response.index != 0:
            raise HoldError("ATT hold response has the wrong service or index")
        if response.opcode == 0:
            raise HoldError("diagnostic firmware rejected ATT hold command")
        if response.opcode != opcode:
            raise HoldError("ATT hold response has the wrong opcode")
        return response.payload

    def arm(self, handle: int) -> Identity:
        """Arm one exact value handle; refuse to overwrite a live local hold."""
        if self._identity is not None and self._state not in (
            HoldState.RELEASED,
            HoldState.ABORTED,
            HoldState.EXPIRED,
        ):
            raise HoldError("ATT hold already armed or active")
        if not 0 < handle <= 0xFFFF:
            raise HoldError("ATT hold requires a nonzero 16-bit value handle")
        response = self._request(ARM, _ARM_REQUEST.pack(handle))
        if len(response) != _IDENTITY.size:
            raise HoldError("ATT hold ARM response has the wrong length")
        identity = Identity(*_IDENTITY.unpack(response))
        if identity.handle != handle:
            raise HoldError("firmware armed a different value handle")
        self._identity = identity
        self._state = HoldState.ARMED
        return identity

    def status(self) -> Snapshot:
        """Poll the held callback without allocating a second BTP serial owner."""
        snapshot = Snapshot.decode(self._request(STATUS))
        if self._identity is not None and snapshot.identity != self._identity:
            raise HoldError("ATT hold generation or handle changed unexpectedly")
        self._state = snapshot.state
        return snapshot

    def wait_until_held(self, deadline: float) -> Identity:
        """Require an exact WAITING status before claiming callback-entry evidence.

        The deadline is monotonic and must be shorter than the firmware hold
        timeout. Recheck STATUS immediately before any fault boundary; a status
        snapshot is not a promise that the hold lasts indefinitely.
        """
        identity = self._require_identity()
        while True:
            if time.monotonic() >= deadline:
                raise HoldError("ATT read did not enter the hold before the deadline")
            snapshot = self.status()
            if time.monotonic() >= deadline:
                raise HoldError("ATT hold status arrived after the deadline")
            if snapshot.state == HoldState.WAITING:
                return identity
            if snapshot.state != HoldState.ARMED:
                raise HoldError(
                    f"ATT hold left ARMED without WAITING: {snapshot.state.name}"
                )
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        """Request release only for the exact currently WAITING generation."""
        self._control(RELEASE, (HoldState.WAITING,))
        self._state = HoldState.RELEASE_REQUESTED

    def abort(self) -> None:
        """Cancel an unused arm or fail a held read; safe for bounded cleanup."""
        self._control(ABORT, (HoldState.ARMED, HoldState.WAITING))
        self._state = HoldState.ABORT_REQUESTED

    def _control(self, opcode: int, allowed: tuple[HoldState, ...]) -> None:
        identity = self._require_identity()
        if self._state not in allowed:
            raise HoldError("ATT hold is not in a controllable state")
        if self._request(opcode, identity.encode()):
            raise HoldError("ATT hold control response must be empty")

    def _require_identity(self) -> Identity:
        if self._identity is None:
            raise HoldError("ATT hold has not been armed")
        return self._identity
