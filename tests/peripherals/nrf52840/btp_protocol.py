"""Provider-owned BTP wire model and incremental stream parser.

This module intentionally has no AutoPTS, nrftest, serial, or socket dependency.
Transport and session lifecycle are kept in separate modules.
"""

from __future__ import annotations

import struct
from collections import deque
from dataclasses import dataclass
from enum import IntEnum

HEADER = struct.Struct("<BBBH")
MAX_PAYLOAD_LENGTH = 4096


class BtpError(ValueError):
    """Base error for malformed or invalid BTP traffic."""


class BtpFrameError(BtpError):
    """Raised when a frame is malformed or exceeds the safety bound."""


class BtpSessionError(RuntimeError):
    """Raised when a session cannot accept or complete a command."""


class BtpStatus(IntEnum):
    SUCCESS = 0x00


@dataclass(frozen=True)
class BtpFrame:
    service: int
    opcode: int
    index: int
    payload: bytes = b""

    def encode(self) -> bytes:
        if not 0 <= self.service <= 0xFF:
            raise BtpFrameError("service must fit in one byte")
        if not 0 <= self.opcode <= 0xFF:
            raise BtpFrameError("opcode must fit in one byte")
        if not 0 <= self.index <= 0xFF:
            raise BtpFrameError("index must fit in one byte")
        if len(self.payload) > MAX_PAYLOAD_LENGTH:
            raise BtpFrameError("payload exceeds safety bound")
        return (
            HEADER.pack(self.service, self.opcode, self.index, len(self.payload))
            + self.payload
        )

    @classmethod
    def decode(cls, encoded: bytes) -> BtpFrame:
        if len(encoded) < HEADER.size:
            raise BtpFrameError("frame is shorter than its header")
        service, opcode, index, length = HEADER.unpack_from(encoded)
        if length > MAX_PAYLOAD_LENGTH:
            raise BtpFrameError("payload exceeds safety bound")
        expected = HEADER.size + length
        if len(encoded) != expected:
            raise BtpFrameError("frame length does not match payload length")
        return cls(service, opcode, index, encoded[HEADER.size :])

    @property
    def is_event(self) -> bool:
        return self.opcode >= 0x80


class BtpStreamParser:
    """Incrementally parse frames from arbitrary byte chunks."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[BtpFrame]:
        self._buffer.extend(chunk)
        frames: list[BtpFrame] = []
        while len(self._buffer) >= HEADER.size:
            length = HEADER.unpack_from(self._buffer)[3]
            if length > MAX_PAYLOAD_LENGTH:
                raise BtpFrameError("payload exceeds safety bound")
            frame_length = HEADER.size + length
            if len(self._buffer) < frame_length:
                break
            raw = bytes(self._buffer[:frame_length])
            del self._buffer[:frame_length]
            frames.append(BtpFrame.decode(raw))
        return frames

    def finish(self) -> None:
        if self._buffer:
            raise BtpFrameError("stream ended with a truncated frame")


@dataclass(frozen=True)
class PendingRequest:
    service: int
    opcode: int
    index: int


class BtpDispatcher:
    """Serialize BTP requests and retain asynchronous events separately."""

    def __init__(self) -> None:
        self._pending: PendingRequest | None = None
        self._events: deque[BtpFrame] = deque()
        self._closed = False

    def begin(self, *, service: int, opcode: int, index: int = 0) -> BtpFrame:
        if self._closed:
            raise BtpSessionError("BTP session is closed")
        if self._pending is not None:
            raise BtpSessionError("only one BTP request may be pending")
        self._pending = PendingRequest(service, opcode, index)
        return BtpFrame(service, opcode, index)

    def receive(self, frame: BtpFrame) -> BtpFrame | None:
        if self._closed:
            raise BtpSessionError("received a frame after BTP session close")
        if frame.is_event:
            self._events.append(frame)
            return None
        pending = self._pending
        if pending is None:
            raise BtpSessionError("received a response without a pending request")
        expected = (pending.service, pending.opcode, pending.index)
        actual = (frame.service, frame.opcode, frame.index)
        if actual != expected and not (
            frame.service == pending.service and frame.opcode == 0
        ):
            raise BtpSessionError(
                f"response does not match pending request: expected={expected!r} actual={actual!r} payload_len={len(frame.payload)} payload={frame.payload.hex()}"
            )
        self._pending = None
        return frame

    def pop_event(self) -> BtpFrame | None:
        return self._events.popleft() if self._events else None

    def close(self) -> None:
        self._closed = True
        self._pending = None
        self._events.clear()

    @property
    def pending(self) -> bool:
        return self._pending is not None
