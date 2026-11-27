"""Serial/USB CDC byte transport for the nRF BTP provider."""

from __future__ import annotations

from typing import Protocol

from .btp_transport import BtpTransportError

DEFAULT_BAUDRATE = 115200
DEFAULT_READ_TIMEOUT = 1.0


class SerialPort(Protocol):
    timeout: float | None

    @property
    def in_waiting(self) -> int: ...

    def write(self, data: bytes) -> int: ...

    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


class SerialByteTransport:
    """Adapt a pyserial-compatible port to the provider byte transport."""

    def __init__(self, port: SerialPort) -> None:
        self._port = port
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise BtpTransportError("serial transport is closed")
        try:
            written = self._port.write(data)
        except OSError as error:
            raise BtpTransportError(f"serial write failed: {error}") from error
        if written != len(data):
            raise BtpTransportError(
                f"serial write was short: expected {len(data)} bytes, wrote {written}"
            )

    def read(self, timeout: float) -> bytes:
        if self._closed:
            raise BtpTransportError("serial transport is closed")
        if timeout <= 0:
            raise BtpTransportError("serial read timeout must be positive")
        try:
            # Only the first byte may block; a size-based read would wait for
            # 4096 bytes or exhaust the deadline even for a complete small frame.
            self._port.timeout = timeout
            data = self._port.read(1)
            if data:
                available = min(self._port.in_waiting, 4095)
                if available:
                    # The availability snapshot must not grant a second wait
                    # budget if fewer bytes remain by the time we read them.
                    self._port.timeout = 0
                    data += self._port.read(available)
        except (OSError, ValueError) as error:
            raise BtpTransportError(f"serial read failed: {error}") from error
        return data

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._port.close()


def open_serial(
    port: str,
    *,
    baudrate: int = DEFAULT_BAUDRATE,
    timeout: float = DEFAULT_READ_TIMEOUT,
) -> SerialByteTransport:
    """Open a raw serial port without importing pyserial at module import time."""
    if not port:
        raise BtpTransportError("serial port must not be empty")
    if baudrate <= 0:
        raise BtpTransportError("serial baudrate must be positive")
    if timeout <= 0:
        raise BtpTransportError("serial timeout must be positive")
    try:
        import serial

        connection = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=timeout,
            write_timeout=timeout,
        )
    except (ImportError, OSError, ValueError) as error:
        raise BtpTransportError(f"cannot open serial port {port}: {error}") from error
    return SerialByteTransport(connection)
