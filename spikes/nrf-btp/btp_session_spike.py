"""Fake-transport BTP session spike built on the protocol boundary."""

from __future__ import annotations

from typing import Protocol

from btp_spike import BtpDispatcher, BtpFrame, BtpSessionError


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
        timeout: float = 1.0,
    ) -> BtpFrame:
        if self._closed:
            raise BtpSessionError("BTP session is closed")
        request = self._dispatcher.begin(service=service, opcode=opcode, index=index)
        self._transport.send(request)
        try:
            while True:
                response = self._dispatcher.receive(self._transport.receive(timeout))
                if response is not None:
                    return response
        except (TimeoutError, EOFError, BtpSessionError):
            self._closed = True
            self._transport.close()
            raise

    def pop_event(self) -> BtpFrame | None:
        return self._dispatcher.pop_event()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._dispatcher.close()
            self._transport.close()
