from __future__ import annotations

import unittest
from collections import deque

from tests.peripherals.nrf52840.btp_protocol import BtpFrame
from tests.peripherals.nrf52840.btp_transport import FramedTransport


class FakeBytes:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = deque(chunks)
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def read(self, timeout: float) -> bytes:
        del timeout
        if not self.chunks:
            raise TimeoutError("fake timeout")
        return self.chunks.popleft()

    def close(self) -> None:
        self.closed = True


class FramedTransportTests(unittest.TestCase):
    def test_fragmented_bytes_are_reassembled(self) -> None:
        frame = BtpFrame(1, 2, 0, b"payload")
        encoded = frame.encode()
        byte_stream = FakeBytes([encoded[:1], encoded[1:5], encoded[5:]])
        transport = FramedTransport(byte_stream)

        transport.send(frame)
        self.assertEqual(transport.receive(1.0), frame)
        self.assertEqual(byte_stream.writes, [encoded])

    def test_multiple_frames_in_one_read_are_queued(self) -> None:
        first = BtpFrame(1, 2, 0, b"one")
        second = BtpFrame(1, 3, 0, b"two")
        byte_stream = FakeBytes([first.encode() + second.encode()])
        transport = FramedTransport(byte_stream)

        self.assertEqual(transport.receive(1.0), first)
        self.assertEqual(transport.receive(1.0), second)

    def test_empty_read_is_eof(self) -> None:
        transport = FramedTransport(FakeBytes([b""]))
        with self.assertRaises(EOFError):
            transport.receive(1.0)

    def test_close_delegates_to_byte_transport(self) -> None:
        byte_stream = FakeBytes([])
        FramedTransport(byte_stream).close()
        self.assertTrue(byte_stream.closed)


if __name__ == "__main__":
    unittest.main()
