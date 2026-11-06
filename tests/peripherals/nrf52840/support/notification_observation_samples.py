"""Owner-local synthetic wire samples, not a firmware emulator or RF evidence.

Offsets are expressed independently as C wire field widths. Production code must
never import these samples. Real C-generated fixtures, when available, are a
separate crossing check rather than a claim made by these byte builders.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from typing import TypeAlias

from ..btp_protocol import BtpFrame

BOOT = bytes(range(1, 17))
OWNER = bytes(range(17, 33))
NEXT_OWNER = bytes(range(33, 49))
PEER = bytes.fromhex("01aabbccddeeff")

WireValue: TypeAlias = int | bytes | tuple[int, ...]

QUERY_FIELDS = (
    ("version", "B"),
    ("result", "B"),
    ("boot", "16s"),
    ("owner", "16s"),
    ("owner_generation", "I"),
    ("enabled", "B"),
    ("capacity", "B"),
    ("value_max", "B"),
    ("profile", "B"),
    ("batch_flags", "B"),
    ("flush_ms", "H"),
    ("trace_capacity", "B"),
    ("tx_group_capacity", "B"),
    ("trace_loss", "B"),
    ("state_flags", "H"),
    ("last_sequence", "I"),
    ("oldest_sequence", "I"),
    ("newest_sequence", "I"),
    ("count", "B"),
    ("counter_count", "B"),
    ("counters", "23I"),
)
RECORD_FIELDS = (
    ("version", "B"),
    ("result", "B"),
    ("boot", "16s"),
    ("owner", "16s"),
    ("owner_generation", "I"),
    ("sequence", "I"),
    ("connection_epoch", "I"),
    ("bearer_epoch", "I"),
    ("database_epoch", "I"),
    ("live_local_id", "B"),
    ("live_peer_present", "B"),
    ("live_peer", "7s"),
    ("value_handle", "H"),
    ("value_length", "H"),
    ("copied_length", "B"),
    ("value", "20s"),
    ("ccc_flags", "B"),
    ("cached_ccc", "B"),
    ("aggregate_ccc", "H"),
    ("per_peer_ccc", "H"),
    ("subscriber_count", "B"),
    ("selected_local_id", "B"),
    ("selected_peer", "7s"),
    ("decision", "B"),
    ("flags", "I"),
    ("selection_errno", "i"),
    ("api_errno", "i"),
    ("att_errno", "i"),
    ("queue_retries", "I"),
    ("queue_errno", "i"),
    ("submit_errno", "i"),
    ("buffer_errno", "i"),
    ("tx_errno", "i"),
    ("submit_attempts", "I"),
    ("submit_admitted", "I"),
    ("tx_callbacks", "I"),
    ("entry_index", "H"),
    ("pdu_sequence", "I"),
    ("pdu_entries", "H"),
    ("pdu_tracked", "B"),
    ("pdu_opcode", "B"),
    ("trace_loss", "B"),
    ("times", "12Q"),
)


def _pack(layout: tuple[tuple[str, str], ...], values: dict[str, WireValue]) -> bytes:
    parts: list[bytes] = []
    for name, code in layout:
        value = values.get(name, bytes(int(code[:-1])) if code.endswith("s") else 0)
        args = value if isinstance(value, tuple) else (value,)
        parts.append(struct.pack("<" + code, *args))
    return b"".join(parts)


def query_bytes(**changes: WireValue) -> bytes:
    """Initialized, enabled profile-2 peer; counters remain caller-supplied facts."""
    values: dict[str, WireValue] = {
        "version": 1,
        "boot": BOOT,
        "owner": OWNER,
        "owner_generation": 7,
        "enabled": 1,
        "capacity": 8,
        "value_max": 20,
        "profile": 2,
        "batch_flags": 15,
        "flush_ms": 1,
        "trace_capacity": 8,
        "tx_group_capacity": 8,
        "counter_count": 23,
        "counters": (0,) * 23,
    }
    values.update(changes)
    return _pack(QUERY_FIELDS, values)


def record_bytes(**changes: WireValue) -> bytes:
    """A selected, bound batch entry with API admission, before deferred flush."""
    values: dict[str, WireValue] = {
        "version": 1,
        "boot": BOOT,
        "owner": OWNER,
        "owner_generation": 7,
        "sequence": 11,
        "connection_epoch": 3,
        "bearer_epoch": 4,
        "database_epoch": 5,
        "live_local_id": 2,
        "live_peer_present": 1,
        "live_peer": PEER,
        "value_handle": 0x1234,
        "value_length": 3,
        "copied_length": 3,
        "value": b"abc",
        "ccc_flags": 63,
        "cached_ccc": 1,
        "aggregate_ccc": 1,
        "per_peer_ccc": 1,
        "subscriber_count": 1,
        "selected_local_id": 2,
        "selected_peer": PEER,
        "decision": 6,
        "flags": 0x6007,
        "entry_index": 1,
        "times": (10, 11, 12, 14, 0, 0, 0, 0, 0, 0, 0, 13),
    }
    values.update(changes)
    return _pack(RECORD_FIELDS, values)


def completed_bytes(**changes: WireValue) -> bytes:
    """Callback and release precede submission/API return; all layers independent."""
    values: dict[str, WireValue] = {
        "flags": 0x70DF,
        "submit_attempts": 1,
        "submit_admitted": 1,
        "tx_callbacks": 1,
        "pdu_sequence": 19,
        "pdu_entries": 1,
        "pdu_tracked": 1,
        "pdu_opcode": 0x1B,
        "times": (10, 11, 12, 20, 14, 15, 16, 17, 18, 0, 0, 13),
    }
    values.update(changes)
    return record_bytes(**values)


def changed(
    payload: bytes, layout: tuple[tuple[str, str], ...], field: str, value: WireValue
) -> bytes:
    """Change exactly one named field without reparsing through the live codec."""
    offset = 0
    for name, code in layout:
        size = struct.calcsize("<" + code)
        if name == field:
            args = value if isinstance(value, tuple) else (value,)
            return (
                payload[:offset]
                + struct.pack("<" + code, *args)
                + payload[offset + size :]
            )
        offset += size
    raise KeyError(field)


class ScriptedTransport:
    """Precomputed responses on a real BtpSession, including send-time callbacks."""

    def __init__(
        self,
        responses: list[bytes],
        *,
        during_send: Callable[[BtpFrame], None] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.sent: list[BtpFrame] = []
        self.frames: list[BtpFrame] = []
        self.closed = False
        self.during_send = during_send

    def send(self, frame: BtpFrame) -> None:
        self.sent.append(frame)
        if self.during_send is not None:
            self.during_send(frame)
        self.frames.append(
            BtpFrame(frame.service, frame.opcode, frame.index, self.responses.pop(0))
        )

    def receive(self, timeout: float) -> BtpFrame:
        return self.frames.pop(0)

    def close(self) -> None:
        self.closed = True
