"""Provider-owned byte and framed transports for the nRF BTP session."""

from __future__ import annotations

import select
import socket
from collections.abc import Callable
from typing import Protocol

from .btp_protocol import BtpFrame, BtpStreamParser


class BtpTransportError(RuntimeError):
    """Raised when the BTP byte stream cannot make progress safely."""


class ByteTransport(Protocol):
    def write(self, data: bytes) -> None: ...

    def read(self, timeout: float) -> bytes: ...

    def close(self) -> None: ...


class FramedTransport:
    """Turn a byte transport into the frame transport consumed by BtpSession."""

    def __init__(self, transport: ByteTransport) -> None:
        self._transport = transport
        self._parser = BtpStreamParser()
        self._frames: list[BtpFrame] = []

    def send(self, frame: BtpFrame) -> None:
        self._transport.write(frame.encode())

    def receive(self, timeout: float) -> BtpFrame:
        while not self._frames:
            chunk = self._transport.read(timeout)
            if not chunk:
                raise EOFError("BTP transport returned EOF")
            self._frames.extend(self._parser.feed(chunk))
        return self._frames.pop(0)

    def close(self) -> None:
        self._transport.close()


class SocketByteTransport:
    """Bounded byte transport for an already-connected BTP socket."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise BtpTransportError("BTP socket is closed")
        try:
            self._connection.sendall(data)
        except OSError as error:
            raise BtpTransportError(f"BTP socket write failed: {error}") from error

    def read(self, timeout: float) -> bytes:
        if self._closed:
            raise BtpTransportError("BTP socket is closed")
        try:
            readable, _, _ = select.select([self._connection], [], [], timeout)
            if not readable:
                raise TimeoutError("BTP socket read timed out")
            data = self._connection.recv(4096)
        except TimeoutError:
            raise
        except OSError as error:
            raise BtpTransportError(f"BTP socket read failed: {error}") from error
        if not data:
            raise EOFError("BTP socket closed by peer")
        return data

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._connection.close()


ConnectSocket = Callable[[str, int], socket.socket]


def connect_socket(path: str, *, timeout: float = 5.0) -> SocketByteTransport:
    """Connect to an AutoPTS-compatible Unix socket with an explicit timeout."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(path)
    except OSError as error:
        connection.close()
        raise BtpTransportError(
            f"cannot connect to BTP socket {path}: {error}"
        ) from error
    finally:
        connection.settimeout(None)
    return SocketByteTransport(connection)
