"""Pure codec/control for the revised, NOT RELEASED notification observation v1.

Wire authority: Zephyr tests/bluetooth/tester/src/btp/btp_notify_observation.h
and notification_observation.rst. No observation here is a delivery verdict.
The caller supplies the existing session and owns its lifecycle and timeout policy.
"""

from __future__ import annotations

import json
import struct
from dataclasses import asdict, dataclass, fields
from enum import IntEnum, IntFlag
from typing import TypeAlias

from .btp_session import BtpSession

SERVICE = INDEX = 0xFF
VERSION = 1
QUERY = 0x20
CONFIGURE = 0x21
READ = 0x22
QUERY_SIZE = 156
CONFIGURE_SIZE = 54
READ_COMMAND_SIZE = 37
RECORD_SIZE = 257
READ_REPLY_SIZE = 259
CAPACITY = TRACE_CAPACITY = TX_GROUP_CAPACITY = 8
VALUE_MAX = 20
COUNTER_COUNT = 23
TIME_COUNT = 12
ZERO_OWNER = bytes(16)
U32_MAX = (1 << 32) - 1


class ObservationWireError(ValueError):
    """Malformed wire data, replay document, or control argument."""


class Result(IntEnum):
    OK = 0
    INVALID = 1
    VERSION = 2
    NOT_READY = 3
    BOOT_MISMATCH = 4
    OWNER_MISMATCH = 5
    NOT_FOUND = 6
    BUSY = 7
    STALE_OWNER = 8
    OWNER_EXHAUSTED = 9


class ObservationCommandError(RuntimeError):
    """An exact firmware error reply; retain bytes for independent replay."""

    def __init__(
        self, result: Result, opcode: int, request: bytes, response: bytes
    ) -> None:
        self.result = result
        self.opcode = opcode
        self.request = request
        self.response = response
        super().__init__(f"notification observation 0x{opcode:02x}: {result.name}")

    def document(self) -> dict[str, object]:
        return _wire_document(self.opcode, self.request, self.response)


class Decision(IntEnum):
    PENDING = 0
    STORAGE_FAILED = 1
    NO_CCC_FLAG = 2
    NO_ASSOCIATION = 3
    CCC_ZERO = 4
    UNSUPPORTED_CCC = 5
    NOTIFY_SELECTED = 6
    SELECTION_FAILED = 7
    OTHER_MODE = 8


class CccFlags(IntFlag):
    ASSOCIATION = 1 << 0
    CCC_FLAG = 1 << 1
    MANAGED_VALID = 1 << 2
    PER_PEER_VALID = 1 << 3
    COUNT_VALID = 1 << 4
    SELECTED_PEER = 1 << 5


class RecordFlags(IntFlag):
    UPDATED = 1 << 0
    API_CALLED = 1 << 1
    API_RETURNED = 1 << 2
    PDU_SEEN = 1 << 3
    ATT_QUEUED = 1 << 4
    ATT_REJECTED = 1 << 5
    BUFFER_RELEASED = 1 << 6
    TX_CALLBACK = 1 << 7
    DISCONNECTED = 1 << 8
    TRUNCATED = 1 << 9
    INCOMPLETE = 1 << 10
    PDU_MISMATCH = 1 << 11
    SUBMIT_RETURNED = 1 << 12
    BOUND = 1 << 13
    BATCH_APPEND = 1 << 14
    TRACE_LOST = 1 << 15


class StateFlags(IntFlag):
    EVICTED = 1 << 0
    EXHAUSTED = 1 << 1
    HISTORY_INCOMPLETE = 1 << 2
    COUNTER_SATURATED = 1 << 3
    TRACE_LOSS = 1 << 4
    PDU_EXHAUSTED = 1 << 5


class BatchFlags(IntFlag):
    COMPILED = 1 << 0
    AUTO_BATCHING = 1 << 1
    LIVE_PEER = 1 << 2
    PEER_BATCHING = 1 << 3


class TraceLoss(IntFlag):
    VECTOR = 1
    GROUP = 2
    TOKEN = 4


class Profile(IntEnum):
    FIXED_ATT = 1
    AUTOMATIC_BATCHING = 2


@dataclass(frozen=True)
class ObservationCounters:
    """Boot-sticky per-update observations, not unique PDUs or resource counts."""

    records: int
    updates: int
    update_only: int
    calls: int
    api_ok: int
    api_errors: int
    pdus: int
    queued: int
    att_errors: int
    submit_attempts: int
    submit_ok: int
    submit_errors: int
    tx_ok: int
    tx_errors: int
    releases: int
    disconnects: int
    evictions: int
    unmatched: int
    omitted: int
    queue_retries: int
    bindings: int
    trace_losses: int
    owner_rotations: int


@dataclass(frozen=True)
class ObservationTimes:
    """Device uptime milliseconds; None is unobserved, zero can be observed.

    No total ordering is imposed: callbacks/releases may precede API/submit
    returns, batching may defer PDU send, and repeated events retain latest times.
    """

    begin: int
    update: int | None
    call: int | None
    returned: int | None
    pdu: int | None
    queue: int | None
    submit: int | None
    tx: int | None
    release: int | None
    disconnect: int | None
    retry: int | None
    bind: int | None


@dataclass(frozen=True)
class ObservationQuery:
    boot: bytes
    owner: bytes
    owner_generation: int
    enabled: bool
    capacity: int
    value_max: int
    profile: Profile
    batch_flags: BatchFlags
    flush_ms: int
    trace_capacity: int
    tx_group_capacity: int
    trace_loss: TraceLoss
    state_flags: StateFlags
    last_sequence: int
    oldest_sequence: int
    newest_sequence: int
    count: int
    counter_count: int
    counters: ObservationCounters
    opcode: int
    request: bytes
    response: bytes

    def document(self) -> dict[str, object]:
        return document(self)


@dataclass(frozen=True)
class ObservationRecord:
    boot: bytes
    owner: bytes
    owner_generation: int
    sequence: int
    connection_epoch: int
    bearer_epoch: int
    database_epoch: int
    live_local_id: int
    live_peer_present: bool
    live_peer: bytes | None
    value_handle: int
    value_length: int
    copied_length: int
    value: bytes
    ccc_flags: CccFlags
    cached_ccc: int | None
    aggregate_ccc: int | None
    per_peer_ccc: int | None
    subscriber_count: int | None
    selected_local_id: int | None
    selected_peer: bytes | None
    decision: Decision
    flags: RecordFlags
    selection_errno: int | None
    api_errno: int | None
    att_errno: int | None
    queue_retries: int
    queue_errno: int | None
    submit_errno: int | None
    buffer_errno: int | None
    tx_errno: int | None
    submit_attempts: int
    submit_admitted: int
    tx_callbacks: int
    entry_index: int | None
    pdu_sequence: int | None
    pdu_entries: int | None
    pdu_tracked: int | None
    pdu_opcode: int | None
    trace_loss: TraceLoss
    times: ObservationTimes
    request: bytes
    response: bytes

    def document(self) -> dict[str, object]:
        return document(self)


Observation: TypeAlias = ObservationQuery | ObservationRecord


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ObservationWireError(message)


def _nonce(value: bytes, name: str, *, zero: bool = False) -> bytes:
    _require(type(value) is bytes and len(value) == 16, f"{name}: expected 16 bytes")
    _require(zero or any(value), f"{name}: zero nonce")
    return value


def _u32(value: int, name: str, *, nonzero: bool = False) -> int:
    _require(
        type(value) is int and int(nonzero) <= value <= U32_MAX,
        f"{name}: u32 bounds/type",
    )
    return value


def _owner_generation(owner: bytes, generation: int) -> None:
    _nonce(owner, "expected_owner", zero=True)
    _u32(generation, "owner_generation")
    _require(
        (owner == ZERO_OWNER) == (generation == 0), "owner/generation inconsistent"
    )


def encode_query() -> bytes:
    return bytes([VERSION])


def encode_configure(
    *,
    expected_boot: bytes,
    expected_owner: bytes,
    expected_owner_generation: int,
    new_owner: bytes,
    enable: bool,
) -> bytes:
    _nonce(expected_boot, "expected_boot")
    _owner_generation(expected_owner, expected_owner_generation)
    _nonce(new_owner, "new_owner")
    _require(type(enable) is bool, "enable must be bool")
    return (
        bytes([VERSION])
        + expected_boot
        + expected_owner
        + struct.pack("<I", expected_owner_generation)
        + new_owner
        + bytes([enable])
    )


def encode_read(*, expected_boot: bytes, record_owner: bytes, sequence: int) -> bytes:
    return (
        bytes([VERSION])
        + _nonce(expected_boot, "expected_boot")
        + _nonce(record_owner, "record_owner")
        + struct.pack("<I", _u32(sequence, "sequence", nonzero=True))
    )


def _request(opcode: int, payload: bytes) -> None:
    _require(
        type(opcode) is int and opcode in (QUERY, CONFIGURE, READ), "invalid opcode"
    )
    _require(type(payload) is bytes, "request must be bytes")
    size = {QUERY: 1, CONFIGURE: CONFIGURE_SIZE, READ: READ_COMMAND_SIZE}[opcode]
    _require(len(payload) == size and payload[0] == VERSION, "request length/version")
    if opcode == CONFIGURE:
        _require(payload[-1] in (0, 1), "invalid enable")
        encode_configure(
            expected_boot=payload[1:17],
            expected_owner=payload[17:33],
            expected_owner_generation=int.from_bytes(payload[33:37], "little"),
            new_owner=payload[37:53],
            enable=bool(payload[-1]),
        )
    elif opcode == READ:
        encode_read(
            expected_boot=payload[1:17],
            record_owner=payload[17:33],
            sequence=int.from_bytes(payload[33:37], "little"),
        )


def _reply(payload: bytes, size: int, opcode: int, request: bytes) -> None:
    _request(opcode, request)
    _require(type(payload) is bytes and len(payload) >= 2, "response header/type")
    _require(payload[0] == VERSION, "response version")
    _require(payload[1] in Result._value2member_map_, "unknown result")
    result = Result(payload[1])
    if result != Result.OK:
        _require(len(payload) == 2, "error response must be exactly two bytes")
        raise ObservationCommandError(result, opcode, request, payload)
    _require(len(payload) == size, f"response length must be {size}")


class _Reader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0

    def take(self, size: int) -> bytes:
        value = self.payload[self.offset : self.offset + size]
        self.offset += size
        return value

    def number(self, code: str) -> int:
        return struct.unpack("<" + code, self.take(struct.calcsize("<" + code)))[0]

    def u8(self) -> int:
        return self.number("B")

    def u16(self) -> int:
        return self.number("H")

    def u32(self) -> int:
        return self.number("I")

    def i32(self) -> int:
        return self.number("i")


def decode_query(
    payload: bytes,
    *,
    request: bytes = b"\x01",
    opcode: int = QUERY,
    expected_boot: bytes | None = None,
) -> ObservationQuery:
    _require(opcode in (QUERY, CONFIGURE), "not a query/configure opcode")
    if expected_boot is not None:
        _nonce(expected_boot, "expected_boot")
    _reply(payload, QUERY_SIZE, opcode, request)
    r = _Reader(payload[2:])
    boot, owner = r.take(16), r.take(16)
    _nonce(boot, "boot")
    generation = r.u32()
    _owner_generation(owner, generation)
    enabled, capacity, value_max, profile, batching = (r.u8() for _ in range(5))
    flush_ms = r.u16()
    trace_capacity, group_capacity, loss = (r.u8() for _ in range(3))
    state = r.u16()
    last, oldest, newest = (r.u32() for _ in range(3))
    count, counter_count = r.u8(), r.u8()
    counters = ObservationCounters(*(r.u32() for _ in range(COUNTER_COUNT)))
    _require(
        enabled in (0, 1) and (not enabled or generation), "enabled/owner inconsistent"
    )
    _require(
        (capacity, value_max, trace_capacity, group_capacity, counter_count)
        == (CAPACITY, VALUE_MAX, TRACE_CAPACITY, TX_GROUP_CAPACITY, COUNTER_COUNT),
        "unsupported capacities/count",
    )
    _require(profile in (1, 2) and batching & ~0x0F == 0, "profile/batch reserved bits")
    b = BatchFlags(batching)
    automatic = bool(b & BatchFlags.COMPILED) and flush_ms != 0
    _require(
        bool(b & BatchFlags.AUTO_BATCHING) == automatic
        and profile == (2 if automatic else 1),
        "batch configuration/profile inconsistent",
    )
    _require(
        bool(b & BatchFlags.COMPILED) or flush_ms == 0,
        "flush without compiled batching",
    )
    _require(
        not b & BatchFlags.PEER_BATCHING or b & BatchFlags.LIVE_PEER and automatic,
        "peer batching without live peer/automatic configuration",
    )
    _require(loss & ~7 == 0 and state & ~0x3F == 0, "loss/state reserved bits")
    s = StateFlags(state)
    _require(
        bool(loss) == bool(s & StateFlags.TRACE_LOSS), "trace loss/state inconsistent"
    )
    _require(
        bool(loss & TraceLoss.TOKEN) == bool(s & StateFlags.PDU_EXHAUSTED),
        "token exhaustion/state inconsistent",
    )
    sticky = s & ~StateFlags.HISTORY_INCOMPLETE
    _require(
        not sticky or s & StateFlags.HISTORY_INCOMPLETE,
        "sticky state without incomplete",
    )
    _require(
        not s & StateFlags.EXHAUSTED or last == U32_MAX,
        "cookie exhaustion without max cookie",
    )
    _require(
        (last, oldest, newest, count)
        == (
            last,
            max(1, last - CAPACITY + 1) if last else 0,
            last,
            min(last, CAPACITY),
        ),
        "ring sequence/count inconsistent",
    )
    _require(not last or generation, "receipts without owner")
    _require(expected_boot is None or boot == expected_boot, "boot mismatch")
    if opcode == CONFIGURE:
        expected_generation = int.from_bytes(request[33:37], "little")
        expected_generation += request[17:33] != request[37:53]
        _require(
            boot == request[1:17]
            and owner == request[37:53]
            and generation == expected_generation
            and enabled == request[-1],
            "configure acknowledgement identity/state mismatch",
        )
    return ObservationQuery(
        boot,
        owner,
        generation,
        bool(enabled),
        capacity,
        value_max,
        Profile(profile),
        b,
        flush_ms,
        trace_capacity,
        group_capacity,
        TraceLoss(loss),
        s,
        last,
        oldest,
        newest,
        count,
        counter_count,
        counters,
        opcode,
        request,
        payload,
    )


def _observed(value: int, valid: object, name: str) -> int | None:
    _require(valid or value == 0, f"unobserved {name} must be zero")
    return value if valid else None


def decode_record(
    payload: bytes, *, expected_boot: bytes, record_owner: bytes, sequence: int
) -> ObservationRecord:
    request = encode_read(
        expected_boot=expected_boot, record_owner=record_owner, sequence=sequence
    )
    _reply(payload, READ_REPLY_SIZE, READ, request)
    r = _Reader(payload[2:])
    boot, owner = r.take(16), r.take(16)
    generation, cookie, connection, bearer, database = (r.u32() for _ in range(5))
    _require(
        (boot, owner, cookie) == (expected_boot, record_owner, sequence),
        "record boot/owner/cookie mismatch",
    )
    _u32(generation, "record owner generation", nonzero=True)
    local_id, peer_present, peer = r.u8(), r.u8(), r.take(7)
    _require(peer_present in (0, 1), "live_peer_present must be boolean")
    _require(
        peer_present or (peer == bytes(7) and local_id == 0), "unobserved live peer"
    )
    handle, length, copied, value = r.u16(), r.u16(), r.u8(), r.take(VALUE_MAX)
    _require(handle != 0, "zero value handle")
    _require(
        copied == min(length, VALUE_MAX) and not any(value[copied:]),
        "value prefix/padding",
    )
    ccc, cached, aggregate, per_peer = r.u8(), r.u8(), r.u16(), r.u16()
    subscribers, selected_local, selected_peer = r.u8(), r.u8(), r.take(7)
    decision, flags = r.u8(), r.u32()
    _require(ccc & ~0x3F == 0 and flags & ~0xFFFF == 0, "record/CCC reserved bits")
    _require(decision in Decision._value2member_map_, "unknown decision")
    c, f, d = CccFlags(ccc), RecordFlags(flags), Decision(decision)
    selection_error, api_error, att_error = r.i32(), r.i32(), r.i32()
    retries, queue_error = r.u32(), r.i32()
    submit_error, buffer_error, tx_error = r.i32(), r.i32(), r.i32()
    attempts, admitted, callbacks = r.u32(), r.u32(), r.u32()
    entry, pdu, entries, tracked, opcode, loss = (
        r.u16(),
        r.u32(),
        r.u16(),
        r.u8(),
        r.u8(),
        r.u8(),
    )
    raw_times = tuple(r.number("Q") for _ in range(TIME_COUNT))
    _require(
        bool(f & RecordFlags.TRUNCATED) == (length > VALUE_MAX),
        "truncation inconsistent",
    )
    _require(
        loss & ~7 == 0 and bool(loss) == bool(f & RecordFlags.TRACE_LOST),
        "trace loss/flag inconsistent",
    )
    _require(
        not f
        & (RecordFlags.TRUNCATED | RecordFlags.PDU_MISMATCH | RecordFlags.TRACE_LOST)
        or f & RecordFlags.INCOMPLETE,
        "incomplete flag missing",
    )
    for dependent, prerequisite in (
        (RecordFlags.API_RETURNED, RecordFlags.API_CALLED),
        (RecordFlags.API_CALLED, RecordFlags.UPDATED),
        (RecordFlags.BOUND, RecordFlags.API_CALLED),
        (RecordFlags.BATCH_APPEND | RecordFlags.PDU_MISMATCH, RecordFlags.BOUND),
        (RecordFlags.PDU_SEEN, RecordFlags.BOUND),
        (RecordFlags.ATT_QUEUED | RecordFlags.ATT_REJECTED, RecordFlags.PDU_SEEN),
    ):
        _require(
            not f & dependent or f & prerequisite, "record flag prerequisite missing"
        )
    _require(
        not c & CccFlags.MANAGED_VALID or c & CccFlags.ASSOCIATION,
        "managed CCC without association",
    )
    _require(
        not c & CccFlags.SELECTED_PEER or c & CccFlags.COUNT_VALID,
        "selected peer without count",
    )
    _require(
        not c & CccFlags.PER_PEER_VALID
        or c & CccFlags.SELECTED_PEER
        and c & CccFlags.MANAGED_VALID,
        "per-peer CCC validity",
    )
    _require(
        c & CccFlags.SELECTED_PEER
        or (not selected_local and selected_peer == bytes(7)),
        "unobserved selected peer",
    )
    _require(
        bool(c & CccFlags.COUNT_VALID)
        == (d in (Decision.NOTIFY_SELECTED, Decision.SELECTION_FAILED)),
        "selection decision/count validity",
    )
    _require(
        not c & CccFlags.COUNT_VALID
        or bool(selection_error) == (d == Decision.SELECTION_FAILED),
        "selection error/decision",
    )
    _require(
        not c & CccFlags.SELECTED_PEER or selection_error == 0,
        "selected peer on selection failure",
    )
    selection_incomplete = (
        d == Decision.NOTIFY_SELECTED
        and not c & CccFlags.SELECTED_PEER
        or c & CccFlags.SELECTED_PEER
        and (not peer_present or local_id != selected_local or peer != selected_peer)
    )
    _require(
        not selection_incomplete or f & RecordFlags.INCOMPLETE,
        "selection identity mismatch without incomplete",
    )
    _require(
        d in (Decision.PENDING, Decision.STORAGE_FAILED) or f & RecordFlags.UPDATED,
        "decision without local update",
    )
    _require(
        not f & RecordFlags.API_CALLED or d == Decision.NOTIFY_SELECTED,
        "API without selection",
    )
    _require(
        not f & RecordFlags.UPDATED
        or d not in (Decision.PENDING, Decision.STORAGE_FAILED),
        "updated/storage decision inconsistent",
    )
    if d == Decision.NO_CCC_FLAG:
        _require(not c & CccFlags.CCC_FLAG, "NO_CCC_FLAG contradicts CCC flags")
    elif d == Decision.NO_ASSOCIATION:
        _require(
            c & CccFlags.CCC_FLAG and not c & CccFlags.ASSOCIATION,
            "NO_ASSOCIATION contradicts CCC flags",
        )
    elif d == Decision.CCC_ZERO:
        _require(
            c & CccFlags.CCC_FLAG and c & CccFlags.ASSOCIATION and cached == 0,
            "CCC_ZERO contradicts cached CCC",
        )
    _require(
        admitted <= attempts and (not admitted or f & RecordFlags.SUBMIT_RETURNED),
        "submit admission/attempts inconsistent",
    )
    _require(
        not f & RecordFlags.SUBMIT_RETURNED or attempts, "submit result without attempt"
    )
    _require(
        bool(callbacks) == bool(f & RecordFlags.TX_CALLBACK),
        "TX flag/count inconsistent",
    )
    _require(
        callbacks <= 1 or f & RecordFlags.INCOMPLETE,
        "repeated callback without incomplete",
    )
    _require(
        not (attempts or retries or callbacks) or f & RecordFlags.PDU_SEEN,
        "transport observations without PDU",
    )
    _require(not f & RecordFlags.BOUND or entry != 0, "zero bound entry ordinal")
    _observed(entry, f & RecordFlags.BOUND, "entry_index")
    if f & RecordFlags.PDU_SEEN:
        _require(
            entries >= 1 and 1 <= tracked <= min(TRACE_CAPACITY, entries),
            "PDU entries/tracked bounds",
        )
        _require(
            opcode in (0x1B, 0x23) and (opcode != 0x1B or entries == 1),
            "PDU opcode/entries",
        )
        _require(entry <= entries, "bound entry exceeds PDU entries")
        _require(
            pdu != 0 or loss & TraceLoss.TOKEN, "zero PDU token without TOKEN loss"
        )
    else:
        _require(
            (pdu, entries, tracked, opcode) == (0, 0, 0, 0), "unobserved PDU fields"
        )
    # Each validity source is independent; do not infer ordering or return success.
    valid_times = (
        True,
        f & RecordFlags.UPDATED,
        f & RecordFlags.API_CALLED,
        f & RecordFlags.API_RETURNED,
        f & RecordFlags.PDU_SEEN,
        f & RecordFlags.ATT_QUEUED,
        attempts,
        f & RecordFlags.TX_CALLBACK,
        f & RecordFlags.BUFFER_RELEASED,
        f & RecordFlags.DISCONNECTED,
        retries,
        f & RecordFlags.BOUND,
    )
    times = ObservationTimes(
        *(
            _observed(v, valid, field.name)
            for v, valid, field in zip(
                raw_times, valid_times, fields(ObservationTimes), strict=True
            )
        )
    )
    return ObservationRecord(
        boot,
        owner,
        generation,
        cookie,
        connection,
        bearer,
        database,
        local_id,
        bool(peer_present),
        peer if peer_present else None,
        handle,
        length,
        copied,
        value[:copied],
        c,
        _observed(cached, c & CccFlags.ASSOCIATION, "cached_ccc"),
        _observed(aggregate, c & CccFlags.MANAGED_VALID, "aggregate_ccc"),
        _observed(per_peer, c & CccFlags.PER_PEER_VALID, "per_peer_ccc"),
        _observed(subscribers, c & CccFlags.COUNT_VALID, "subscriber_count"),
        selected_local if c & CccFlags.SELECTED_PEER else None,
        selected_peer if c & CccFlags.SELECTED_PEER else None,
        d,
        f,
        _observed(selection_error, c & CccFlags.COUNT_VALID, "selection_errno"),
        _observed(api_error, f & RecordFlags.API_RETURNED, "api_errno"),
        _observed(att_error, f & RecordFlags.ATT_REJECTED, "att_errno"),
        retries,
        _observed(queue_error, retries, "queue_errno"),
        _observed(submit_error, f & RecordFlags.SUBMIT_RETURNED, "submit_errno"),
        _observed(buffer_error, f & RecordFlags.BUFFER_RELEASED, "buffer_errno"),
        _observed(tx_error, f & RecordFlags.TX_CALLBACK, "tx_errno"),
        attempts,
        admitted,
        callbacks,
        entry if f & RecordFlags.BOUND else None,
        pdu if f & RecordFlags.PDU_SEEN else None,
        entries if f & RecordFlags.PDU_SEEN else None,
        tracked if f & RecordFlags.PDU_SEEN else None,
        opcode if f & RecordFlags.PDU_SEEN else None,
        TraceLoss(loss),
        times,
        request,
        payload,
    )


def _wire_document(opcode: int, request: bytes, response: bytes) -> dict[str, object]:
    return {
        "schema": "notification-observation-v1",
        "service": SERVICE,
        "index": INDEX,
        "opcode": opcode,
        "request_hex": request.hex(),
        "response_hex": response.hex(),
    }


def _json_value(value: object) -> object:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (IntEnum, IntFlag)):
        return int(value)
    if isinstance(value, dict):
        return {k: _json_value(v) for k, v in value.items()}
    return value


def document(observation: Observation) -> dict[str, object]:
    """JSON-ready typed facts plus exact payload hex (BTP header routing separate)."""
    opcode = observation.opcode if isinstance(observation, ObservationQuery) else READ
    result = _wire_document(opcode, observation.request, observation.response)
    decoded = asdict(observation)
    for key in ("opcode", "request", "response"):
        decoded.pop(key, None)
    result["decoded"] = _json_value(decoded)
    return result


def _from_document(value: object, *, record: bool) -> Observation:
    _require(type(value) is dict, "document must be an object")
    _require(value.get("schema") == "notification-observation-v1", "document schema")
    _require(
        type(value.get("service")) is int
        and value["service"] == SERVICE
        and type(value.get("index")) is int
        and value["index"] == INDEX,
        "document routing",
    )
    opcode = value.get("opcode")
    _require(
        type(opcode) is int and opcode in ((READ,) if record else (QUERY, CONFIGURE)),
        "document opcode",
    )
    raw: list[bytes] = []
    for key in ("request_hex", "response_hex"):
        text = value.get(key)
        _require(type(text) is str, f"{key} must be hex text")
        maximum = (
            {QUERY: 1, CONFIGURE: CONFIGURE_SIZE, READ: READ_COMMAND_SIZE}[opcode]
            if key == "request_hex"
            else READ_REPLY_SIZE
            if record
            else QUERY_SIZE
        )
        _require(len(text) <= 2 * maximum, f"{key} exceeds wire bound")
        try:
            data = bytes.fromhex(text)
        except ValueError as error:
            raise ObservationWireError(f"invalid {key}") from error
        _require(text == data.hex(), f"{key} must be canonical lowercase hex")
        raw.append(data)
    request, response = raw
    _request(opcode, request)
    if record:
        parsed = decode_record(
            response,
            expected_boot=request[1:17],
            record_owner=request[17:33],
            sequence=int.from_bytes(request[33:37], "little"),
        )
    else:
        parsed = decode_query(response, request=request, opcode=opcode)
    try:
        _require(
            json.dumps(value, sort_keys=True, allow_nan=False)
            == json.dumps(document(parsed), sort_keys=True, allow_nan=False),
            "document facts differ from raw wire",
        )
    except (TypeError, ValueError) as error:
        raise ObservationWireError("document facts differ from raw wire") from error
    return parsed


class ObserverController:
    """Synchronous commands on an existing session; no ownership or retry policy."""

    def __init__(self, session: BtpSession) -> None:
        self._session = session

    def _exchange(self, opcode: int, request: bytes) -> bytes:
        response = self._session.request(
            service=SERVICE, opcode=opcode, index=INDEX, payload=request
        )
        _require(
            (response.service, response.opcode, response.index)
            == (SERVICE, opcode, INDEX),
            "unexpected BTP response routing/status",
        )
        return response.payload

    def query(self, *, expected_boot: bytes | None = None) -> ObservationQuery:
        if expected_boot is not None:
            _nonce(expected_boot, "expected_boot")
        request = encode_query()
        return decode_query(self._exchange(QUERY, request), expected_boot=expected_boot)

    def configure(
        self,
        *,
        expected_boot: bytes,
        expected_owner: bytes,
        expected_owner_generation: int,
        new_owner: bytes,
        enable: bool,
    ) -> ObservationQuery:
        request = encode_configure(
            expected_boot=expected_boot,
            expected_owner=expected_owner,
            expected_owner_generation=expected_owner_generation,
            new_owner=new_owner,
            enable=enable,
        )
        return decode_query(
            self._exchange(CONFIGURE, request), request=request, opcode=CONFIGURE
        )

    def read(
        self, *, expected_boot: bytes, record_owner: bytes, sequence: int
    ) -> ObservationRecord:
        request = encode_read(
            expected_boot=expected_boot, record_owner=record_owner, sequence=sequence
        )
        return decode_record(
            self._exchange(READ, request),
            expected_boot=expected_boot,
            record_owner=record_owner,
            sequence=sequence,
        )

    def rotate(
        self,
        *,
        expected_boot: bytes,
        expected_owner: bytes,
        expected_owner_generation: int,
        new_owner: bytes,
    ) -> tuple[ObservationQuery, ObservationQuery, ObservationQuery]:
        """Explicit off -> rotate(off) -> enable. Failure stops; no reset/rollback.

        Historical records and in-flight observations keep their original owner,
        generation and entry epochs. The caller must retain every returned document.
        """
        encode_configure(
            expected_boot=expected_boot,
            expected_owner=expected_owner,
            expected_owner_generation=expected_owner_generation,
            new_owner=new_owner,
            enable=False,
        )
        _require(
            expected_owner != ZERO_OWNER and expected_owner != new_owner,
            "rotation requires distinct initialized owners",
        )
        _require(expected_owner_generation < U32_MAX, "owner generation exhausted")
        off = self.configure(
            expected_boot=expected_boot,
            expected_owner=expected_owner,
            expected_owner_generation=expected_owner_generation,
            new_owner=expected_owner,
            enable=False,
        )
        rotated = self.configure(
            expected_boot=expected_boot,
            expected_owner=expected_owner,
            expected_owner_generation=expected_owner_generation,
            new_owner=new_owner,
            enable=False,
        )
        enabled = self.configure(
            expected_boot=expected_boot,
            expected_owner=new_owner,
            expected_owner_generation=expected_owner_generation + 1,
            new_owner=new_owner,
            enable=True,
        )
        return off, rotated, enabled

    @staticmethod
    def query_from_document(value: object) -> ObservationQuery:
        parsed = _from_document(value, record=False)
        assert isinstance(parsed, ObservationQuery)
        return parsed

    @staticmethod
    def record_from_document(value: object) -> ObservationRecord:
        parsed = _from_document(value, record=True)
        assert isinstance(parsed, ObservationRecord)
        return parsed
