"""Synthetic fixed-bearer v2 firmware; no transport discovery, USB or RF."""

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
from ..gap_commands import GAP_DEVICE_CONNECTED

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
    """Synthetic firmware responses travel through the real BtpSession dispatcher."""

    def __init__(self, clock: Clock, fault: str) -> None:
        self.clock = clock
        self.fault = fault
        self.frames: deque[BtpFrame] = deque()
        self.commands: list[BtpFrame] = []
        self.sequence = 0
        self.phase = Phase.IDLE
        self.ticket = None
        self.connected = False
        self.epoch = 0
        self.resolving = False
        self.aborting = False
        self.captured = False
        self.winner = Winner.NONE
        self.arm_deadline = self.hold_deadline = self.resolution_deadline = 0.0
        self.hold_budget_ms = 20000
        self.handle = 0
        self.remote_value = b"\x00"
        self.remote_reads: list[tuple[int, int, bytes]] = []

    def capture_read(self) -> None:
        if self.phase is not Phase.ARMED or not self.connected:
            raise AssertionError("remote read requires the armed connected target")
        self.phase = Phase.HELD
        self.captured = True
        self.hold_deadline = self.clock.now + self.hold_budget_ms / 1000

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
        elif frame.opcode == ARM:
            request = ArmRequest.decode(Opcode.ARM_READ, frame.payload)
            if (
                not self.connected
                or request.common.peer != PEER
                or request.before != self.remote_value
            ):
                raise AssertionError("ARM did not bind the live peer/before value")
            self.sequence += 1
            if self.fault == "sequence-reuse" and self.sequence == 2:
                self.sequence = 1
            self.handle = request.common.handle
            self.ticket = Ticket(
                request.common.boot_nonce,
                request.common.owner_nonce,
                self.sequence,
                self.epoch,
                self.epoch,
                1,
                Operation.READ,
                self.handle,
            )
            self.phase = Phase.ARMED
            self.arm_deadline = self.clock.now + request.common.arm_budget_ms / 1000
            self.hold_budget_ms = request.common.hold_budget_ms
            self.resolving = self.aborting = self.captured = False
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
                        self.remote_reads.append(
                            (self.sequence, self.handle, self.remote_value)
                        )
                self.captured |= self.phase is Phase.HELD
                captured = self.captured
                retired = self.phase is Phase.RETIRED
                flags = StatusFlags.REQUEST_CAPTURED if captured else StatusFlags(0)
                if retired:
                    flags |= StatusFlags.REARMABLE
                if retired and self.resolving:
                    flags |= (
                        StatusFlags.CALLBACK_INVOKED
                        | StatusFlags.NORMAL_PATH_EVALUATED
                        | StatusFlags.RESPONSE_QUEUED
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
                    0x0A if captured else 0,
                    0,
                    0,
                    PEER,
                    0,
                    1 if self.resolving else 0,
                    b"",
                    self.remote_value,
                    self.remote_value,
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
                    if retired and self.resolving
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
