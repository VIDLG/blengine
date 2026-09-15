from __future__ import annotations

import unittest
from collections import deque

from tests.peripherals.nrf52840.btp_transport import BtpTransportError
from tests.peripherals.nrf52840.serial_transport import SerialByteTransport


class FakeSerial:
    def __init__(self, reads: list[bytes], written: int | None = None) -> None:
        self.reads = deque(reads)
        self.writes: list[bytes] = []
        self.written = written
        self.closed = False

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data) if self.written is None else self.written

    def read(self, size: int) -> bytes:
        del size
        if not self.reads:
            return b""
        return self.reads.popleft()

    def close(self) -> None:
        self.closed = True


class SerialByteTransportTests(unittest.TestCase):
    def test_write_and_read_use_raw_bytes(self) -> None:
        serial = FakeSerial([b"reply"])
        transport = SerialByteTransport(serial)

        transport.write(b"request")
        self.assertEqual(transport.read(1.0), b"reply")
        self.assertEqual(serial.writes, [b"request"])

    def test_short_write_is_terminal_error(self) -> None:
        serial = FakeSerial([], written=1)
        transport = SerialByteTransport(serial)
        with self.assertRaises(BtpTransportError):
            transport.write(b"request")

    def test_empty_read_is_available_for_framed_eof_policy(self) -> None:
        serial = FakeSerial([])
        transport = SerialByteTransport(serial)
        self.assertEqual(transport.read(1.0), b"")

    def test_close_is_idempotent_and_rejects_future_io(self) -> None:
        serial = FakeSerial([])
        transport = SerialByteTransport(serial)
        transport.close()
        transport.close()
        self.assertTrue(serial.closed)
        with self.assertRaises(BtpTransportError):
            transport.read(1.0)


if __name__ == "__main__":
    unittest.main()
