"""Synthetic fixed-bearer v2 firmware; no transport discovery, USB or RF.

One slot, one engine: a read, a characteristic write and a CCC write are the same state
machine with a different ARM opcode, exactly as the firmware implements them.
"""

from __future__ import annotations

import struct
from collections import deque
from dataclasses import replace

from .. import fault_control as fault
from ..btp_protocol import BtpFrame
from ..fault_control import (
    ArmRequest,
    InfrastructureError,
    Opcode,
    Operation,
    Outcome,
    Phase,
    RefCounters,
    Remaining,
    Status,
    StatusFlags,
    Ticket,
    Winner,
    decode_ticket_command,
    encode_status,
)
from ..gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED, GAP_DISCONNECT

PEER = bytes.fromhex("01665544332211")
TARGET = 51
ARM, RELEASE, ABORT, STATUS = (
    Opcode.ARM_READ,
    Opcode.RESOLVE,
    Opcode.ABORT,
    Opcode.STATUS,
)


def read_receipt(
    ticket: Ticket, phase: Phase, winner: Winner = Winner.NONE, *, peer: bytes = PEER
) -> Status:
    """Typed synthetic read receipt for offline ledger mutation tests, never live evidence."""
    captured = phase is not Phase.ARMED
    retired = phase is Phase.RETIRED
    flags = StatusFlags.REQUEST_CAPTURED if captured else StatusFlags(0)
    if retired:
        flags |= StatusFlags.REARMABLE
        if winner is Winner.COMMIT:
            flags |= (
                StatusFlags.NORMAL_PATH_EVALUATED
                | StatusFlags.CALLBACK_INVOKED
                | StatusFlags.RESPONSE_QUEUED
                | StatusFlags.ANSWER_ORDERED
                | StatusFlags.TX_COMPLETED
            )
    return Status(
        ticket,
        phase,
        winner,
        flags,
        0x0A if captured else 0,
        0,
        0,
        peer,
        0,
        1 if winner is Winner.COMMIT else 0,
        b"",
        b"\x00",
        b"\x00",
        RefCounters(0, 0, 0, 0, 0, 0) if retired else RefCounters(1, 1, 1, 1, 0, 0),
        Remaining(
            10000 if phase is Phase.ARMED else 0,
            20000 if phase is Phase.HELD else 0,
            0,
            0,
        ),
        Outcome.PENDING
        if not retired
        else Outcome.NORMAL_NO_COMMIT
        if winner is Winner.COMMIT
        else Outcome.DISCONNECTED_UNCOMMITTED,
        InfrastructureError.NONE,
    )


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Wire:
    """Synthetic firmware responses travel through the real BtpSession dispatcher.

    One slot, one engine: the same state machine serves a read, a characteristic write and
    a CCC write, because that is what the firmware is. Which of them a hold is comes from
    the ARM opcode and is recorded on the ticket, never inferred from the reply.
    """

    def __init__(self, clock: Clock, fault: str) -> None:
        self.clock = clock
        self.fault = fault
        self.frames: deque[BtpFrame] = deque()
        self.commands: list[BtpFrame] = []
        self.sequence = 0
        self.phase = Phase.IDLE
        self.ticket = None
        self.operation = Operation.READ
        self.connected = False
        self.epoch = 0
        self.resolving = False
        self.aborting = False
        self.rejecting = False
        self.captured = False
        self.winner = Winner.NONE
        self.arm_deadline = self.hold_deadline = self.resolution_deadline = 0.0
        self.hold_budget_ms = 20000
        self.handle = 0
        self.requested = b""
        self.before_snapshot = b"\x00"
        self.remote_value = b"\x00"
        self.ccc_value = bytes(2)
        # What a read of the managed descriptor answers, when it differs from the state the
        # central enabled: the Tester value getter reports the descriptor's stored default.
        # `None` means the getter agrees with the managed state.
        self.ccc_getter: bytes | None = None
        self.remote_reads: list[tuple[int, int, bytes]] = []
        # Commits the synthetic peer applied, as (handle, value): what a caller must be
        # able to tell apart from a request it merely forwarded.
        self.commits: list[tuple[int, bytes]] = []
        # Peer addresses this synthetic controller was asked to drop, in wire form.
        self.drops: list[bytes] = []

    @property
    def stored_value(self) -> bytes:
        """The value the target handle holds; a CCC write targets the managed u16."""
        return (
            self.ccc_value
            if self.operation is Operation.CCC_WRITE
            else self.remote_value
        )

    def capture(self) -> None:
        """Park the exact request the central dispatched, as the firmware does."""
        if self.phase is not Phase.ARMED or not self.connected:
            raise AssertionError("remote operation requires the armed connected target")
        self.phase = Phase.HELD
        self.captured = True
        self.hold_deadline = self.clock.now + self.hold_budget_ms / 1000

    def capture_read(self) -> None:
        self.operation = Operation.READ
        self.capture()

    def capture_write(self, payload: bytes) -> None:
        """Capture the write that matches the binding; anything else takes the normal path."""
        if self.operation is Operation.READ:
            raise AssertionError("this hold is a read, not a write")
        if payload != self.requested:
            raise AssertionError("captured write differs from the armed request")
        self.capture()

    def remaining(self, deadline: float) -> int:
        return max(0, int((deadline - self.clock.now) * 1000))

    def gap(self, opcode: int, payload: bytes = PEER) -> None:
        if opcode == GAP_DEVICE_CONNECTED:
            self.connected = True
            self.epoch += 1
            payload += b"\x00" * 6
        else:
            self.connected = False
            if self.ticket is not None and self.phase in (
                Phase.ARMED,
                Phase.HELD,
                Phase.RESOLUTION_QUEUED,
            ):
                self.captured |= self.phase is Phase.HELD
                self.phase = Phase.RETIRED
                self.resolving = self.aborting = False
                self.winner = Winner.DISCONNECT
        self.frames.append(BtpFrame(1, opcode, 0, payload))

    def send(self, frame: BtpFrame) -> None:
        self.commands.append(frame)
        if frame.service == 1 and frame.opcode == GAP_DISCONNECT:
            # The peer ends its own link: the controller reports the detach as an event and
            # acknowledges the command separately, so a host can wait for the agreement.
            self.drops.append(frame.payload)
            self.gap(GAP_DEVICE_DISCONNECTED)
            self.frames.append(BtpFrame(1, frame.opcode, 0, b""))
            return
        payload = b""
        if frame.service != 255 or frame.index != 255:
            raise AssertionError("fault commands must use vendor/index ff")
        if frame.opcode == Opcode.GET_LIMITS:
            payload = (
                b"\x02\x00"
                + bytes(range(1, 17))
                + struct.pack(
                    "<BBBBBHIIIIB7sIII",
                    7,
                    0,
                    1,
                    1,
                    20,
                    4
                    if self.phase in (Phase.ARMED, Phase.HELD, Phase.RESOLUTION_QUEUED)
                    else 0,
                    10000,
                    20000,
                    2000,
                    5000,
                    int(self.connected),
                    PEER if self.connected else bytes(7),
                    self.epoch if self.connected else 0,
                    self.epoch if self.connected else 0,
                    1 if self.connected else 0,
                )
            )
        elif frame.opcode in (Opcode.ARM_READ, Opcode.ARM_WRITE, Opcode.ARM_CCC):
            arm_opcode = Opcode(frame.opcode)
            request = ArmRequest.decode(arm_opcode, frame.payload)
            if not self.connected or request.common.peer != PEER:
                raise AssertionError("ARM did not bind the live peer")
            if request.operation is not Operation.READ:
                # A write binding names the descriptor or value it targets and the exact
                # request; the peer re-checks the expected before-state at capture.
                if request.before != (
                    self.ccc_value
                    if request.operation is Operation.CCC_WRITE
                    else self.remote_value
                ):
                    raise AssertionError("ARM did not bind the current before value")
            elif request.before != self.remote_value:
                raise AssertionError("ARM did not bind the live before value")
            if (
                request.before == request.requested
                and request.operation is Operation.CCC_WRITE
            ):
                raise AssertionError("CCC binding is a no-op on the current value")
            self.sequence += 1
            if self.fault == "sequence-reuse" and self.sequence == 2:
                self.sequence = 1
            self.handle = request.common.handle
            self.operation = request.operation
            self.requested = request.requested
            self.before_snapshot = request.before
            self.ticket = Ticket(
                request.common.boot_nonce,
                request.common.owner_nonce,
                self.sequence,
                self.epoch,
                self.epoch,
                1,
                request.operation,
                self.handle,
            )
            self.phase = Phase.ARMED
            self.arm_deadline = self.clock.now + request.common.arm_budget_ms / 1000
            self.hold_budget_ms = request.common.hold_budget_ms
            self.resolving = self.aborting = self.rejecting = False
            self.captured = False
            self.winner = Winner.NONE
            payload = b"\x02\x00" + self.ticket.encode() + bytes([self.phase])
        elif frame.opcode == STATUS:
            ticket, _ = decode_ticket_command(Opcode.STATUS, frame.payload)
            if ticket != self.ticket:
                payload = b"\x02\x03"
            else:
                if (
                    self.phase is Phase.RESOLUTION_QUEUED
                    and self.fault != "release-timeout"
                ):
                    self.phase = Phase.RETIRED
                    if self.resolving:
                        # The normal path applied the request: a read answers with the
                        # stored value, a write stores it. Both are peer-side facts.
                        if self.operation is Operation.READ:
                            self.remote_reads.append(
                                (self.sequence, self.handle, self.remote_value)
                            )
                        elif self.operation is Operation.CCC_WRITE:
                            self.ccc_value = self.requested
                            self.commits.append((self.handle, self.requested))
                        else:
                            self.remote_value = self.requested
                            self.commits.append((self.handle, self.requested))
                self.captured |= self.phase is Phase.HELD
                captured = self.captured
                retired = self.phase is Phase.RETIRED
                flags = StatusFlags.REQUEST_CAPTURED if captured else StatusFlags(0)
                if retired:
                    flags |= StatusFlags.REARMABLE
                committed = retired and self.resolving
                if committed:
                    flags |= (
                        StatusFlags.CALLBACK_INVOKED | StatusFlags.NORMAL_PATH_EVALUATED
                    )
                if committed and self.operation is not Operation.READ:
                    flags |= StatusFlags.COMMIT_OBSERVED
                if committed or (retired and self.rejecting):
                    flags |= (
                        StatusFlags.RESPONSE_QUEUED
                        | StatusFlags.ANSWER_ORDERED
                        | StatusFlags.TX_COMPLETED
                    )
                actual_ticket = ticket
                if self.fault == "status-drift" and self.phase is Phase.HELD:
                    from dataclasses import replace

                    actual_ticket = replace(ticket, sequence=ticket.sequence + 1)
                status = Status(
                    actual_ticket,
                    self.phase,
                    self.winner,
                    flags,
                    (
                        0x0A
                        if captured and self.operation is Operation.READ
                        else 0x12
                        if captured
                        else 0
                    ),
                    0,
                    0,
                    PEER,
                    0,
                    1 if committed and self.operation is Operation.READ else 0,
                    self.requested if captured else b"",
                    self.before_snapshot,
                    self.stored_value,
                    RefCounters(0, 0, 0, 0, 0, 0)
                    if retired
                    else RefCounters(1, 1, 1, 1, 0, 0),
                    Remaining(
                        self.remaining(self.arm_deadline)
                        if self.phase is Phase.ARMED
                        else 0,
                        self.remaining(self.hold_deadline)
                        if self.phase is Phase.HELD
                        else 0,
                        self.remaining(self.resolution_deadline)
                        if self.phase is Phase.RESOLUTION_QUEUED
                        else 0,
                        0,
                    ),
                    Outcome.NORMAL_NO_COMMIT
                    if committed and self.operation is Operation.READ
                    else Outcome.COMMITTED
                    if committed
                    else Outcome.ARM_CANCELLED
                    if retired and self.aborting and not captured
                    else Outcome.INJECTED_REJECT
                    if retired and self.winner in (Winner.ABORT, Winner.REJECT)
                    else Outcome.DISCONNECTED_UNCOMMITTED
                    if retired
                    else Outcome.PENDING,
                    InfrastructureError.NONE,
                )
                payload = encode_status(status)
        elif frame.opcode in (RELEASE, ABORT):
            ticket, decision = decode_ticket_command(
                Opcode(frame.opcode), frame.payload
            )
            if ticket != self.ticket or not self.connected:
                payload = b"\x02\x03"
            elif self.phase not in (
                (Phase.ARMED, Phase.HELD) if frame.opcode == ABORT else (Phase.HELD,)
            ):
                payload = b"\x02\x02"
            elif self.clock.now >= (
                self.arm_deadline if self.phase is Phase.ARMED else self.hold_deadline
            ):
                payload = b"\x02\x09"
            else:
                if frame.opcode == ABORT and self.fault == "cleanup-failure":
                    raise RuntimeError("injected abort failure")
                self.captured |= self.phase is Phase.HELD
                self.winner = Winner(decision) if decision is not None else Winner.ABORT
                self.resolving = self.winner is Winner.COMMIT
                self.aborting = self.winner is Winner.ABORT
                self.rejecting = self.winner is Winner.REJECT
                self.phase = Phase.RESOLUTION_QUEUED
                self.resolution_deadline = self.clock.now + 2
                payload = b"\x02\x00" + ticket.encode() + bytes([self.phase])
        else:
            raise AssertionError(f"unexpected wire command {frame}")
        self.frames.append(BtpFrame(frame.service, frame.opcode, frame.index, payload))

    def receive(self, timeout: float) -> BtpFrame:
        self.clock.now += 0.001
        if not self.frames:
            self.clock.now += timeout
            raise TimeoutError("synthetic idle wire")
        return self.frames.popleft()

    def close(self) -> None:
        pass


class WatchdogWire(Wire):
    def send(self, frame: BtpFrame) -> None:
        super().send(frame)
        if frame.opcode == fault.Opcode.STATUS and self.phase is fault.Phase.RETIRED:
            response = self.frames.pop()
            if self.fault == "stale-receipt":
                response = BtpFrame(255, frame.opcode, 255, b"\x02\x03")
            elif self.fault == "pinned-evidence":
                status = fault.decode_status(response.payload)
                response = BtpFrame(
                    255,
                    frame.opcode,
                    255,
                    fault.encode_status(
                        replace(status, refs=replace(status.refs, evidence_pending=1))
                    ),
                )
            self.frames.append(response)
