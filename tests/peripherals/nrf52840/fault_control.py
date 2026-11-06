"""Forward-only ATT fault v2 wire codec and dispatch controller.

Wire authority: the att_fault.inc/att_fault.rst in the attested Zephyr source.
No transport, lease, firmware attestation or RF authorization is created here.
Control acknowledgements report firmware admission, never Central delivery or
remote commit. STATUS is a copied receipt, not a deadline renewal.
"""

from __future__ import annotations

import math
import struct
import uuid
from dataclasses import dataclass
from enum import IntEnum, IntFlag

from .btp_commands import INDEX_NONE, VENDOR, BtpCommandError, VendorCommands
from .btp_session import BtpSession

VERSION = 2
TICKET_LENGTH = 55
COMMON_LENGTH = 62
LIMITS_LENGTH = 61
ACK_LENGTH = 58
STATUS_MAX_LENGTH = 162
VALUE_MAX = 20
ARM_MAX_MS = 10_000
HOLD_MAX_MS = 20_000
RESOLUTION_MAX_MS = 2_000
RETIREMENT_MAX_MS = 5_000


class Opcode(IntEnum):
    GET_LIMITS = 0x10
    ARM_WRITE = 0x11
    ARM_CCC = 0x12
    RESOLVE = 0x13
    ABORT = 0x14
    STATUS = 0x15
    ARM_READ = 0x16


class Operation(IntEnum):
    RESPONSE_WRITE = 1
    CCC_WRITE = 2
    READ = 3


class SupportedOperations(IntFlag):
    RESPONSE_WRITE = 1
    CCC_WRITE = 2
    READ = 4


class BearerPolicy(IntEnum):
    FIXED_ONLY = 0


class EngineFlags(IntFlag):
    QUARANTINED = 1
    BUSY = 4


class Result(IntEnum):
    OK = 0
    BUSY = 1
    BAD_STATE = 2
    STALE_TICKET = 3
    MISMATCH = 4
    RESOURCE_EXHAUSTED = 5
    INVALID_ARGUMENT = 6
    NOT_READY = 7
    UNSUPPORTED_PROTOCOL = 8
    TOO_LATE = 9


class Phase(IntEnum):
    IDLE = 0
    ARMED = 1
    HELD = 2
    RESOLUTION_QUEUED = 3
    COMMITTING = 4
    RETIRING = 5
    RETIRED = 6
    QUARANTINED = 7


class Decision(IntEnum):
    COMMIT = 1
    REJECT = 2


class Winner(IntEnum):
    NONE = 0
    COMMIT = 1
    REJECT = 2
    ABORT = 3
    ARM_EXPIRE = 4
    HOLD_EXPIRE = 5
    DISCONNECT = 6
    PROTOCOL_FAILURE = 7
    RESOURCE_FAILURE = 8


class StatusFlags(IntFlag):
    REQUEST_CAPTURED = 1 << 0
    CALLBACK_INVOKED = 1 << 1
    COMMIT_OBSERVED = 1 << 2
    RESPONSE_QUEUED = 1 << 3
    TX_COMPLETED = 1 << 4
    TX_FAILED = 1 << 5
    INFRASTRUCTURE_FAILED = 1 << 6
    REARMABLE = 1 << 7
    NORMAL_PATH_EVALUATED = 1 << 8
    ANSWER_ORDERED = 1 << 9


class Outcome(IntEnum):
    PENDING = 0
    ARM_CANCELLED = 1
    ARM_EXPIRED = 2
    INJECTED_REJECT = 3
    HOLD_EXPIRED_UNCOMMITTED = 4
    DISCONNECTED_UNCOMMITTED = 5
    NORMAL_VALIDATION_FAILED = 6
    COMMITTED = 7
    NORMAL_NO_COMMIT = 8
    PROTOCOL_FAILURE = 9
    RESOURCE_FAILURE = 10


class InfrastructureError(IntEnum):
    NONE = 0
    CONFLICTING_TRAFFIC = 1
    PRECONDITION_CHANGED = 2
    RESPONSE_ADMISSION_TIMEOUT = 3
    RESPONSE_SEND_FAILED = 4
    RETIREMENT_TIMEOUT = 5
    EVIDENCE_OVERFLOW = 6
    OWNERSHIP_INVARIANT = 7


class FaultWireError(ValueError):
    """Malformed or noncanonical fault wire data; never silently truncated."""


class FaultCommandError(BtpCommandError):
    """A well-formed non-success firmware result, with no invented receipt."""

    def __init__(self, result: Result) -> None:
        self.result = result
        super().__init__(f"ATT fault control: {result.name}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise FaultWireError(message)


def _uint(value: int, bits: int, name: str, *, nonzero: bool = False) -> None:
    _require(
        type(value) is int and int(nonzero) <= value < 1 << bits,
        f"invalid {name}: expected {'nonzero ' if nonzero else ''}u{bits}",
    )


def _nonce(value: bytes) -> None:
    _require(type(value) is bytes and len(value) == 16 and any(value), "invalid nonce")


def owner_nonce(run_uuid: str) -> bytes:
    """Canonical UUID text to canonical bytes, not bytes_le or UUID text bytes."""
    try:
        parsed = uuid.UUID(run_uuid)
    except (ValueError, TypeError, AttributeError) as error:
        raise FaultWireError("owner must be a canonical UUID") from error
    _require(str(parsed) == run_uuid, "owner must be a canonical UUID")
    _nonce(parsed.bytes)
    return parsed.bytes


def _peer(value: bytes) -> None:
    _require(type(value) is bytes and len(value) == 7, "peer must be seven wire bytes")
    _require(value[0] in (0, 1, 2, 3), "unknown peer address type")


def _value(value: bytes) -> None:
    _require(type(value) is bytes and len(value) <= VALUE_MAX, "value exceeds 20 bytes")


@dataclass(frozen=True)
class Ticket:
    boot_nonce: bytes
    owner_nonce: bytes
    sequence: int
    connection_epoch: int
    bearer_epoch: int
    database_epoch: int
    operation: Operation
    handle: int

    def encode(self) -> bytes:
        _nonce(self.boot_nonce)
        _nonce(self.owner_nonce)
        _uint(self.sequence, 64, "sequence", nonzero=True)
        for epoch in (self.connection_epoch, self.bearer_epoch, self.database_epoch):
            _uint(epoch, 32, "epoch", nonzero=True)
        _uint(self.handle, 16, "handle", nonzero=True)
        _require(type(self.operation) is Operation, "unknown operation")
        return (
            self.boot_nonce
            + self.owner_nonce
            + struct.pack(
                "<QIIIBH",
                self.sequence,
                self.connection_epoch,
                self.bearer_epoch,
                self.database_epoch,
                self.operation,
                self.handle,
            )
        )

    @classmethod
    def decode(cls, payload: bytes) -> Ticket:
        _require(len(payload) == TICKET_LENGTH, "ticket must be exactly 55 bytes")
        seq, conn, bearer, database, operation, handle = struct.unpack(
            "<QIIIBH", payload[32:]
        )
        try:
            ticket = cls(
                payload[:16],
                payload[16:32],
                seq,
                conn,
                bearer,
                database,
                Operation(operation),
                handle,
            )
        except ValueError as error:
            raise FaultWireError("unknown ticket operation") from error
        ticket.encode()
        return ticket


@dataclass(frozen=True)
class ArmCommon:
    boot_nonce: bytes
    owner_nonce: bytes
    connection_epoch: int
    bearer_epoch: int
    database_epoch: int
    peer: bytes
    handle: int
    arm_budget_ms: int
    hold_budget_ms: int

    def encode(self) -> bytes:
        _nonce(self.boot_nonce)
        _nonce(self.owner_nonce)
        for epoch in (self.connection_epoch, self.bearer_epoch, self.database_epoch):
            _uint(epoch, 32, "epoch", nonzero=True)
        _peer(self.peer)
        _uint(self.handle, 16, "handle", nonzero=True)
        for value, maximum in (
            (self.arm_budget_ms, ARM_MAX_MS),
            (self.hold_budget_ms, HOLD_MAX_MS),
        ):
            _uint(value, 32, "budget")
            _require(100 <= value <= maximum, "budget outside firmware deadline bounds")
        return (
            bytes([VERSION])
            + self.boot_nonce
            + self.owner_nonce
            + struct.pack(
                "<III7sHII",
                self.connection_epoch,
                self.bearer_epoch,
                self.database_epoch,
                self.peer,
                self.handle,
                self.arm_budget_ms,
                self.hold_budget_ms,
            )
        )

    @classmethod
    def decode(cls, payload: bytes) -> ArmCommon:
        _require(
            len(payload) == COMMON_LENGTH and payload[0] == VERSION,
            "invalid ARM common header",
        )
        common = cls(
            payload[1:17], payload[17:33], *struct.unpack("<III7sHII", payload[33:])
        )
        common.encode()
        return common


@dataclass(frozen=True)
class ArmRequest:
    operation: Operation
    common: ArmCommon
    before: bytes
    requested: bytes = b""

    @property
    def opcode(self) -> Opcode:
        return {
            Operation.RESPONSE_WRITE: Opcode.ARM_WRITE,
            Operation.CCC_WRITE: Opcode.ARM_CCC,
            Operation.READ: Opcode.ARM_READ,
        }[self.operation]

    def encode(self) -> bytes:
        _require(type(self.operation) is Operation, "unknown ARM operation")
        _value(self.before)
        _value(self.requested)
        if self.operation is Operation.CCC_WRITE:
            _require(
                len(self.before) == len(self.requested) == 2, "CCC values must be u16"
            )
            # Exact wire values; COMMIT's normal GATT callback decides CCC semantics.
            body = self.before + self.requested
        elif self.operation is Operation.READ:
            _require(not self.requested, "read has no requested value")
            body = bytes([len(self.before)]) + self.before
        else:
            _require(
                len(self.requested) <= len(self.before), "write exceeds before length"
            )
            body = (
                bytes([len(self.before)])
                + self.before
                + bytes([len(self.requested)])
                + self.requested
            )
        return self.common.encode() + body

    @classmethod
    def decode(cls, opcode: Opcode, payload: bytes) -> ArmRequest:
        operations = {
            Opcode.ARM_WRITE: Operation.RESPONSE_WRITE,
            Opcode.ARM_CCC: Operation.CCC_WRITE,
            Opcode.ARM_READ: Operation.READ,
        }
        _require(type(opcode) is Opcode and opcode in operations, "not an ARM opcode")
        common = ArmCommon.decode(payload[:COMMON_LENGTH])
        reader = _Reader(payload[COMMON_LENGTH:])
        if opcode == Opcode.ARM_CCC:
            before, requested = reader.take(2), reader.take(2)
        else:
            before = reader.value()
            requested = reader.value() if opcode == Opcode.ARM_WRITE else b""
        reader.end()
        request = cls(operations[opcode], common, before, requested)
        request.encode()
        return request


class _Reader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0

    def take(self, length: int) -> bytes:
        end = self.offset + length
        _require(end <= len(self.payload), "truncated fault payload")
        value = self.payload[self.offset : end]
        self.offset = end
        return value

    def unpack(self, fmt: str) -> tuple[int, ...]:
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))

    def byte(self) -> int:
        return self.take(1)[0]

    def value(self) -> bytes:
        length = self.byte()
        _require(length <= VALUE_MAX, "value exceeds 20 bytes")
        return self.take(length)

    def end(self) -> None:
        _require(self.offset == len(self.payload), "trailing fault data")


def _reply(payload: bytes, maximum: int) -> _Reader:
    _require(2 <= len(payload) <= maximum, "invalid fault reply length")
    reader = _Reader(payload)
    _require(reader.byte() == VERSION, "unsupported fault version")
    try:
        result = Result(reader.byte())
    except ValueError as error:
        raise FaultWireError("unknown fault result") from error
    if result is not Result.OK:
        reader.end()
        raise FaultCommandError(result)
    return reader


@dataclass(frozen=True)
class Limits:
    boot_nonce: bytes
    supported_operations: SupportedOperations
    bearer_policy: BearerPolicy
    max_connections: int
    max_live_slots: int
    max_value_length: int
    engine_flags: EngineFlags
    arm_max_ms: int
    hold_max_ms: int
    resolution_max_ms: int
    retirement_max_ms: int
    peer_present: bool
    peer: bytes
    connection_epoch: int
    bearer_epoch: int
    database_epoch: int


def decode_limits(payload: bytes) -> Limits:
    reader = _reply(payload, LIMITS_LENGTH)
    boot = reader.take(16)
    supported, policy, connections, slots, maximum, flags = reader.unpack("<BBBBBH")
    deadlines = reader.unpack("<IIII")
    present = reader.byte()
    peer = reader.take(7)
    epochs = reader.unpack("<III")
    reader.end()
    _nonce(boot)
    _require(supported == 7, "unsupported operation mask (expected 7)")
    _require(flags & ~5 == 0, "reserved engine flags")
    _require(
        (policy, connections, slots, maximum) == (0, 1, 1, VALUE_MAX),
        "unsupported resource/bearer limits",
    )
    _require(
        deadlines == (ARM_MAX_MS, HOLD_MAX_MS, RESOLUTION_MAX_MS, RETIREMENT_MAX_MS),
        "unsupported deadline limits",
    )
    _require(present in (0, 1), "invalid peer_present")
    _peer(peer)
    _require(
        all(epochs) if present else peer == bytes(7) and epochs == (0, 0, 0),
        "invalid peer/epoch presence",
    )
    return Limits(
        boot,
        SupportedOperations(supported),
        BearerPolicy(policy),
        connections,
        slots,
        maximum,
        EngineFlags(flags),
        *deadlines,
        bool(present),
        peer,
        *epochs,
    )


@dataclass(frozen=True)
class Ack:
    ticket: Ticket
    phase: Phase


def decode_ack(payload: bytes) -> Ack:
    reader = _reply(payload, ACK_LENGTH)
    ticket = Ticket.decode(reader.take(TICKET_LENGTH))
    try:
        phase = Phase(reader.byte())
    except ValueError as error:
        raise FaultWireError("unknown ACK phase") from error
    reader.end()
    return Ack(ticket, phase)


@dataclass(frozen=True)
class RefCounters:
    conn_refs: int
    bearer_pins: int
    response_buffers: int
    resolver_work: int
    tx_pending: int
    evidence_pending: int


@dataclass(frozen=True)
class Remaining:
    arm_ms: int
    hold_ms: int
    resolution_ms: int
    retirement_ms: int


@dataclass(frozen=True)
class Status:
    ticket: Ticket
    phase: Phase
    winner: Winner
    flags: StatusFlags
    request_opcode: int
    derived_write_flags: int
    offset: int
    peer: bytes
    normal_att_result: int
    callback_result: int
    observed: bytes
    before: bytes
    after: bytes
    refs: RefCounters
    remaining: Remaining
    outcome: Outcome
    infrastructure_error: InfrastructureError


def decode_status(payload: bytes) -> Status:
    reader = _reply(payload, STATUS_MAX_LENGTH)
    ticket = Ticket.decode(reader.take(TICKET_LENGTH))
    try:
        phase, winner = Phase(reader.byte()), Winner(reader.byte())
        flags, opcode, derived, offset = reader.unpack("<HBBH")
        peer = reader.take(7)
        normal, callback = reader.unpack("<Bh")
        observed, before, after = reader.value(), reader.value(), reader.value()
        counters = reader.unpack("<BBBBBB")
        remaining = reader.unpack("<IIII")
        outcome = Outcome(reader.byte())
        infrastructure = InfrastructureError(reader.byte())
    except ValueError as error:
        raise FaultWireError("invalid status enum or field") from error
    reader.end()
    _require(flags & ~0x3FF == 0, "reserved status flags")
    _require(derived == 0 and offset == 0, "unsupported derived flags/offset")
    _peer(peer)
    expected_opcode = 0x0A if ticket.operation is Operation.READ else 0x12
    captured = bool(flags & StatusFlags.REQUEST_CAPTURED)
    _require(
        opcode == (expected_opcode if captured else 0),
        "request opcode does not match target operation",
    )
    if ticket.operation is Operation.CCC_WRITE:
        _require(len(before) == len(after) == 2, "CCC snapshots must be u16")
        _require(len(observed) == (2 if captured else 0), "CCC request must be u16")

    if ticket.operation is Operation.READ:
        _require(
            not observed
            and not flags & StatusFlags.COMMIT_OBSERVED
            and outcome is not Outcome.COMMITTED,
            "read cannot report a write commit",
        )
    # Pending evidence includes one UART sender outside the eight-entry queue.
    _require(
        all(value <= 1 for value in counters[:5]) and counters[5] <= 9,
        "invalid bounded owner counters",
    )
    for value, maximum in zip(
        remaining,
        (ARM_MAX_MS, HOLD_MAX_MS, RESOLUTION_MAX_MS, RETIREMENT_MAX_MS),
        strict=True,
    ):
        _require(value <= maximum, "remaining deadline exceeds budget")
    for value, active in zip(
        remaining,
        (Phase.ARMED, Phase.HELD, Phase.RESOLUTION_QUEUED, Phase.RETIRING),
        strict=True,
    ):
        _require(value == 0 or phase is active, "deadline for inactive phase")
    return Status(
        ticket,
        phase,
        winner,
        StatusFlags(flags),
        opcode,
        derived,
        offset,
        peer,
        normal,
        callback,
        observed,
        before,
        after,
        RefCounters(*counters),
        Remaining(*remaining),
        outcome,
        infrastructure,
    )


def encode_status(status: Status) -> bytes:
    """Lossless copied receipt for host evidence; validate with the same strict decoder."""
    values = (status.observed, status.before, status.after)
    for value in values:
        _value(value)
    encoded = (
        bytes([VERSION, Result.OK])
        + status.ticket.encode()
        + struct.pack(
            "<BBHBBH7sBh",
            status.phase,
            status.winner,
            status.flags,
            status.request_opcode,
            status.derived_write_flags,
            status.offset,
            status.peer,
            status.normal_att_result,
            status.callback_result,
        )
        + b"".join(bytes([len(value)]) + value for value in values)
        + struct.pack(
            "<BBBBBB",
            status.refs.conn_refs,
            status.refs.bearer_pins,
            status.refs.response_buffers,
            status.refs.resolver_work,
            status.refs.tx_pending,
            status.refs.evidence_pending,
        )
        + struct.pack(
            "<IIII",
            status.remaining.arm_ms,
            status.remaining.hold_ms,
            status.remaining.resolution_ms,
            status.remaining.retirement_ms,
        )
        + bytes([status.outcome, status.infrastructure_error])
    )
    _require(decode_status(encoded) == status, "noncanonical status evidence")
    return encoded


def cleanup_ready(status: Status) -> bool:
    """Retirement is not cleanup: all six reported owners must drain and rearm be allowed."""
    return (
        status.phase is Phase.RETIRED
        and bool(status.flags & StatusFlags.REARMABLE)
        and status.refs == RefCounters(0, 0, 0, 0, 0, 0)
        and status.infrastructure_error is InfrastructureError.NONE
        and not status.flags & StatusFlags.INFRASTRUCTURE_FAILED
    )


def read_completed(status: Status) -> bool:
    """Normal read success with ordered local response, not a write commit or peer-delivery claim."""
    required = (
        StatusFlags.REQUEST_CAPTURED
        | StatusFlags.NORMAL_PATH_EVALUATED
        | StatusFlags.ANSWER_ORDERED
    )
    return (
        cleanup_ready(status)
        and status.ticket.operation is Operation.READ
        and status.winner is Winner.COMMIT
        and status.outcome is Outcome.NORMAL_NO_COMMIT
        and status.flags & required == required
        and status.normal_att_result == 0
        and not status.flags & (StatusFlags.COMMIT_OBSERVED | StatusFlags.TX_FAILED)
    )


def ticket_document(ticket: Ticket) -> dict[str, int | str]:
    """Exact portable ticket; sequence/handle alone cannot identify a v2 owner."""
    return {
        "wire_hex": ticket.encode().hex(),
        "sequence": ticket.sequence,
        "handle": ticket.handle,
    }


def ticket_from_document(document: dict[str, object]) -> Ticket:
    _require(
        type(document) is dict
        and set(document) == {"wire_hex", "sequence", "handle"}
        and type(document["sequence"]) is int
        and type(document["handle"]) is int,
        "invalid ticket document schema/types",
    )
    try:
        raw = document["wire_hex"]
        _require(type(raw) is str, "ticket wire_hex must be text")
        ticket = Ticket.decode(bytes.fromhex(raw))
    except (KeyError, ValueError, TypeError) as error:
        raise FaultWireError("invalid ticket document") from error
    _require(ticket_document(ticket) == document, "noncanonical ticket document")
    return ticket


def status_document(status: Status) -> dict[str, object]:
    """Persist the complete canonical receipt, never sequence/handle alone."""
    return {
        "protocol_version": VERSION,
        "ticket": ticket_document(status.ticket),
        "status_wire": encode_status(status).hex(),
    }


def status_from_document(document: dict[str, object]) -> Status:
    _require(
        type(document) is dict
        and set(document) == {"protocol_version", "ticket", "status_wire"}
        and type(document["protocol_version"]) is int
        and document["protocol_version"] == VERSION,
        "invalid v2 status document",
    )
    ticket = ticket_from_document(document["ticket"])
    raw = document["status_wire"]
    _require(type(raw) is str, "status wire must be canonical hex")
    status = decode_status(bytes.fromhex(raw))
    _require(
        status.ticket == ticket and encode_status(status).hex() == raw,
        "status document ticket/wire drift",
    )
    return status


def encode_ticket_command(
    opcode: Opcode, ticket: Ticket, decision: Decision | None = None
) -> bytes:
    _require(
        type(opcode) is Opcode
        and opcode in (Opcode.RESOLVE, Opcode.ABORT, Opcode.STATUS),
        "not a ticket command",
    )
    if opcode is Opcode.RESOLVE:
        _require(type(decision) is Decision, "RESOLVE requires COMMIT or REJECT")
    else:
        _require(decision is None, "unexpected resolution decision")
    return (
        bytes([VERSION])
        + ticket.encode()
        + (bytes([decision]) if decision is not None else b"")
    )


def decode_ticket_command(
    opcode: Opcode, payload: bytes
) -> tuple[Ticket, Decision | None]:
    _require(
        type(opcode) is Opcode
        and opcode in (Opcode.RESOLVE, Opcode.ABORT, Opcode.STATUS),
        "not a ticket command",
    )
    reader = _Reader(payload)
    _require(reader.byte() == VERSION, "unsupported fault version")
    ticket = Ticket.decode(reader.take(TICKET_LENGTH))
    try:
        decision = Decision(reader.byte()) if opcode is Opcode.RESOLVE else None
    except ValueError as error:
        raise FaultWireError("unknown resolution decision") from error
    reader.end()
    return ticket, decision


class FaultController(VendorCommands):
    """Use the existing session owner; no polling, retries or local ticket history.

    Caller supplies a live-attested Limits/target and owns orchestration/deadlines.
    Mutations and STATUS always send the exact ticket to firmware. In particular,
    no current-connection epoch check hides the retained receipt after disconnect.
    """

    def __init__(self, session: BtpSession, run_uuid: str) -> None:
        super().__init__(session)
        self.owner_nonce = owner_nonce(run_uuid)

    def _call(self, opcode: Opcode, payload: bytes, timeout: float) -> bytes:
        _require(
            math.isfinite(timeout) and timeout > 0,
            "timeout must be positive and finite",
        )
        response = self._session.request(
            service=VENDOR,
            opcode=int(opcode),
            index=INDEX_NONE,
            payload=payload,
            timeout=timeout,
        )
        if response.opcode == 0:
            raise BtpCommandError(f"ATT fault BTP failure: {response.payload.hex()}")
        _require(
            (response.service, response.opcode, response.index)
            == (VENDOR, opcode, INDEX_NONE),
            "mismatched BTP response",
        )
        return response.payload

    def get_limits(self, *, timeout: float = 1.0) -> Limits:
        return decode_limits(self._call(Opcode.GET_LIMITS, bytes([VERSION]), timeout))

    def require_idle(self, *, timeout: float = 1.0) -> Limits:
        """No ticket exists to query/adopt before ARM; inspect the engine, not a fake IDLE STATUS."""
        limits = self.get_limits(timeout=timeout)
        _require(
            not limits.engine_flags,
            "fault engine busy/quarantined; foreign resources remain",
        )
        return limits

    def arm_read(
        self, handle: int, before: bytes, peer: bytes, *, timeout: float = 1.0
    ) -> Ack:
        """Bind a read to live firmware epochs and the caller's observed GAP peer.

        Caller must attest the target/before value under its lease first. No local
        read callback is used to fabricate a captured remote read.
        """
        limits = self.require_idle(timeout=timeout)
        _require(
            limits.peer_present and limits.peer == peer,
            "ARM_READ requires the exact live peer",
        )
        common = ArmCommon(
            limits.boot_nonce,
            self.owner_nonce,
            limits.connection_epoch,
            limits.bearer_epoch,
            limits.database_epoch,
            peer,
            handle,
            limits.arm_max_ms,
            limits.hold_max_ms,
        )
        return self.arm(ArmRequest(Operation.READ, common, before), timeout=timeout)

    def arm(self, request: ArmRequest, *, timeout: float = 1.0) -> Ack:
        payload = request.encode()
        _require(
            request.common.owner_nonce == self.owner_nonce, "foreign ARM owner nonce"
        )
        ack = decode_ack(self._call(request.opcode, payload, timeout))
        common, ticket = request.common, ack.ticket
        _require(
            (
                ticket.boot_nonce,
                ticket.owner_nonce,
                ticket.connection_epoch,
                ticket.bearer_epoch,
                ticket.database_epoch,
                ticket.operation,
                ticket.handle,
            )
            == (
                common.boot_nonce,
                common.owner_nonce,
                common.connection_epoch,
                common.bearer_epoch,
                common.database_epoch,
                request.operation,
                common.handle,
            ),
            "ARM ticket target mismatch",
        )
        _require(ack.phase is Phase.ARMED, "ARM acknowledgement is not ARMED")
        return ack

    def _ticket_payload(
        self, opcode: Opcode, ticket: Ticket, decision: Decision | None = None
    ) -> bytes:
        _require(ticket.owner_nonce == self.owner_nonce, "foreign ticket owner nonce")
        return encode_ticket_command(opcode, ticket, decision)

    def resolve(
        self, ticket: Ticket, decision: Decision, *, timeout: float = 1.0
    ) -> Ack:
        payload = self._ticket_payload(Opcode.RESOLVE, ticket, decision)
        ack = decode_ack(self._call(Opcode.RESOLVE, payload, timeout))
        _require(
            ack.ticket == ticket and ack.phase is Phase.RESOLUTION_QUEUED,
            "RESOLVE acknowledgement mismatch",
        )
        return ack

    def abort(self, ticket: Ticket, *, timeout: float = 1.0) -> Ack:
        payload = self._ticket_payload(Opcode.ABORT, ticket)
        ack = decode_ack(self._call(Opcode.ABORT, payload, timeout))
        _require(
            ack.ticket == ticket
            and ack.phase in (Phase.RESOLUTION_QUEUED, Phase.RETIRING),
            "ABORT acknowledgement mismatch",
        )
        return ack

    def status(self, ticket: Ticket, *, timeout: float = 1.0) -> Status:
        payload = self._ticket_payload(Opcode.STATUS, ticket)
        status = decode_status(self._call(Opcode.STATUS, payload, timeout))
        _require(status.ticket == ticket, "STATUS ticket mismatch")
        return status
