"""Fake-transport BTP session spike built on the protocol boundary."""

from __future__ import annotations

import math
import time
from typing import Protocol

from .btp_protocol import BtpDispatcher, BtpFrame, BtpSessionError


class FrameTransport(Protocol):
    def send(self, frame: BtpFrame) -> None: ...

    def receive(self, timeout: float) -> BtpFrame: ...

    def close(self) -> None: ...


class BtpSession:
    """A synchronous, single-request BTP session for deterministic testing."""

    def __init__(self, transport: FrameTransport) -> None:
        self._transport = transport
        self._dispatcher = BtpDispatcher()
        self._closed = False

    def request(
        self,
        *,
        service: int,
        opcode: int,
        index: int = 0,
        payload: bytes = b"",
        timeout: float = 1.0,
    ) -> BtpFrame:
        if self._closed:
            raise BtpSessionError("BTP session is closed")
        if not math.isfinite(timeout) or timeout <= 0:
            raise BtpSessionError("BTP request timeout must be positive and finite")
        deadline = time.monotonic() + timeout
        request = self._dispatcher.begin(service=service, opcode=opcode, index=index)
        request = BtpFrame(request.service, request.opcode, request.index, payload)
        self._transport.send(request)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("BTP request timed out")
                response = self._dispatcher.receive(self._transport.receive(remaining))
                if response is not None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("BTP request timed out")
                    return response
        except (TimeoutError, EOFError, BtpSessionError):
            self._closed = True
            self._transport.close()
            raise

    def pop_event(self) -> BtpFrame | None:
        return self._dispatcher.pop_event()

    def poll_event(self, timeout: float = 0.1) -> BtpFrame | None:
        """Consume one queued or newly read event while no command is pending.

        A direct Peripheral fixture is the sole owner of its BTP transport. It
        therefore needs an idle path to observe GAP connection events without
        issuing a competing command or opening a second serial session.
        Serial timeout is represented as EOF by the byte transport, so an idle
        read with no frame is normalized to ``None`` here.
        """
        if self._closed:
            raise BtpSessionError("BTP session is closed")
        queued = self._dispatcher.pop_event()
        if queued is not None:
            return queued
        try:
            frame = self._transport.receive(timeout)
        except (TimeoutError, EOFError):
            return None
        response = self._dispatcher.receive(frame)
        if response is not None:
            raise BtpSessionError("received a response while polling BTP events")
        return self._dispatcher.pop_event()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._dispatcher.close()
            self._transport.close()
