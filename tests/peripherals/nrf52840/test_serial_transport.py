from __future__ import annotations

import unittest
from collections import deque
from unittest.mock import PropertyMock, patch

from serial import PortNotOpenError, SerialException

from tests.peripherals.nrf52840.btp_protocol import MAX_PAYLOAD_LENGTH, BtpFrame
from tests.peripherals.nrf52840.btp_transport import BtpTransportError, FramedTransport
from tests.peripherals.nrf52840.serial_transport import SerialByteTransport


class FakeSerial:
    """Model sized pyserial reads with separately arriving byte batches.

    A blocking read exposes the next batch after a simulated arrival delay.
    An undersized batch consumes the remaining timeout, whereas timeout=0
    only drains the current buffer. No real sleep or serial hardware is used.
    """

    def __init__(self, reads: list[bytes], written: int | None = None) -> None:
        self.reads = deque(reads)
        self.buffer = bytearray()
        self.writes: list[bytes] = []
        self.written = written
        self.closed = False
        self.close_calls = 0
        self.timeout: float | None = 1.0
        self.read_calls: list[tuple[int, float]] = []
        self.waited = 0.0
        self.availability_checks = 0

    @property
    def in_waiting(self) -> int:
        if self.closed:
            raise PortNotOpenError()
        self.availability_checks += 1
        return len(self.buffer)

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data) if self.written is None else self.written

    def read(self, size: int) -> bytes:
        if self.closed:
            raise PortNotOpenError()
        assert self.timeout is not None
        self.read_calls.append((size, self.timeout))
        elapsed = 0.0
        if not self.buffer and self.timeout > 0 and self.reads:
            self.buffer.extend(self.reads.popleft())
            elapsed = min(0.01, self.timeout)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        self.waited += self.timeout if len(data) < size else elapsed
        return data

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class SerialByteTransportTests(unittest.TestCase):
    def test_small_response_waits_only_for_first_byte_and_drains_available(
        self,
    ) -> None:
        serial = FakeSerial([b"reply"])
        transport = SerialByteTransport(serial)

        transport.write(b"request")
        self.assertEqual(transport.read(1.0), b"reply")
        self.assertEqual(serial.writes, [b"request"])
        self.assertEqual(serial.read_calls, [(1, 1.0), (4, 0)])
        self.assertEqual(serial.availability_checks, 1)
        self.assertEqual(serial.waited, 0.01)

    def test_each_read_has_one_blocking_budget_without_waiting_for_next_batch(
        self,
    ) -> None:
        serial = FakeSerial([b"a", b"bc", b"d"])
        transport = SerialByteTransport(serial)

        self.assertEqual(transport.read(0.5), b"a")
        self.assertEqual(transport.read(0.25), b"bc")
        self.assertEqual(transport.read(0.125), b"d")
        self.assertEqual(serial.read_calls, [(1, 0.5), (1, 0.25), (1, 0), (1, 0.125)])
        self.assertAlmostEqual(serial.waited, 0.03)

    def test_drain_is_nonblocking_when_availability_snapshot_is_stale(self) -> None:
        serial = FakeSerial([b"ab"])
        transport = SerialByteTransport(serial)
        with patch.object(
            FakeSerial, "in_waiting", new_callable=PropertyMock
        ) as waiting:
            waiting.return_value = 8
            self.assertEqual(transport.read(0.007), b"ab")
            waiting.assert_called_once_with()
        self.assertEqual(serial.read_calls, [(1, 0.007), (8, 0)])
        self.assertEqual(serial.waited, 0.007)

    def test_empty_drain_preserves_first_byte(self) -> None:
        serial = FakeSerial([b"a", b"later"])
        transport = SerialByteTransport(serial)
        with patch.object(
            FakeSerial, "in_waiting", new_callable=PropertyMock
        ) as waiting:
            waiting.return_value = 4
            self.assertEqual(transport.read(0.5), b"a")
        self.assertEqual(serial.read_calls, [(1, 0.5), (4, 0)])
        self.assertEqual(serial.waited, 0.01)
        self.assertEqual(list(serial.reads), [b"later"])

    def test_chunk_is_bounded_and_remaining_bytes_are_not_lost(self) -> None:
        data = bytes(range(256)) * 32
        serial = FakeSerial([data])
        transport = SerialByteTransport(serial)

        self.assertEqual(transport.read(1.0), data[:4096])
        self.assertEqual(transport.read(1.0), data[4096:])
        self.assertEqual(serial.read_calls, [(1, 1.0), (4095, 0)] * 2)
        self.assertEqual(serial.waited, 0.01)
        self.assertFalse(serial.buffer)

    def test_framed_transport_reassembles_partial_and_max_payload_frames(self) -> None:
        for payload in (b"payload", b"x" * MAX_PAYLOAD_LENGTH):
            with self.subTest(payload_length=len(payload)):
                frame = BtpFrame(1, 2, 0, payload)
                encoded = frame.encode()
                serial = FakeSerial([encoded[:1], encoded[1:3], encoded[3:]])
                transport = FramedTransport(SerialByteTransport(serial))

                self.assertEqual(transport.receive(1.0), frame)
                self.assertFalse(serial.buffer)
                self.assertFalse(serial.reads)
                self.assertTrue(all(size <= 4095 for size, _ in serial.read_calls))
                self.assertAlmostEqual(serial.waited, 0.03)

    def test_coalesced_frames_are_available_without_another_serial_read(self) -> None:
        first = BtpFrame(1, 2, 0, b"one")
        second = BtpFrame(1, 3, 0, b"two")
        encoded = first.encode() + second.encode()
        serial = FakeSerial([encoded])
        transport = FramedTransport(SerialByteTransport(serial))

        self.assertEqual(transport.receive(1.0), first)
        self.assertEqual(transport.receive(1.0), second)
        self.assertEqual(serial.read_calls, [(1, 1.0), (len(encoded) - 1, 0)])
        self.assertEqual(serial.waited, 0.01)

    def test_short_write_is_terminal_error(self) -> None:
        serial = FakeSerial([], written=1)
        transport = SerialByteTransport(serial)
        with self.assertRaises(BtpTransportError):
            transport.write(b"request")

    def test_empty_read_is_available_for_framed_eof_policy(self) -> None:
        serial = FakeSerial([])
        transport = SerialByteTransport(serial)
        self.assertEqual(transport.read(0.25), b"")
        self.assertEqual(serial.read_calls, [(1, 0.25)])
        self.assertEqual(serial.availability_checks, 0)
        self.assertEqual(serial.waited, 0.25)
        with self.assertRaises(EOFError):
            FramedTransport(transport).receive(0.25)

    def test_partial_frame_followed_by_empty_read_keeps_existing_eof_policy(
        self,
    ) -> None:
        serial = FakeSerial([BtpFrame(1, 2, 0, b"payload").encode()[:3]])
        transport = FramedTransport(SerialByteTransport(serial))
        with self.assertRaises(EOFError):
            transport.receive(0.25)
        self.assertEqual(serial.read_calls, [(1, 0.25), (2, 0), (1, 0.25)])
        self.assertAlmostEqual(serial.waited, 0.26)

    def test_nonpositive_timeout_is_rejected_before_port_io(self) -> None:
        serial = FakeSerial([b"reply"])
        transport = SerialByteTransport(serial)
        for timeout in (0, -1):
            with (
                self.subTest(timeout=timeout),
                self.assertRaisesRegex(BtpTransportError, "must be positive"),
            ):
                transport.read(timeout)
        self.assertEqual(serial.read_calls, [])
        self.assertEqual(serial.availability_checks, 0)

    def test_read_and_drain_errors_are_transport_errors_with_original_cause(
        self,
    ) -> None:
        for call in (1, 2):
            for error in (OSError("I/O failed"), SerialException("disconnected")):
                with self.subTest(call=call, error=type(error).__name__):
                    serial = FakeSerial([b"reply"])
                    transport = SerialByteTransport(serial)
                    if call == 2:
                        serial.buffer.extend(b"eply")
                    with patch.object(serial, "read") as read:
                        read.side_effect = error if call == 1 else [b"r", error]
                        with self.assertRaisesRegex(
                            BtpTransportError, "serial read failed"
                        ) as raised:
                            transport.read(1.0)
                    self.assertIs(raised.exception.__cause__, error)

    def test_availability_and_timeout_setting_errors_are_transport_errors(self) -> None:
        for attribute, error in (
            ("in_waiting", OSError("ioctl failed")),
            ("timeout", OSError("configuration failed")),
            ("timeout", ValueError("invalid timeout")),
        ):
            with self.subTest(attribute=attribute, error=type(error).__name__):
                serial = FakeSerial([b"reply"])
                transport = SerialByteTransport(serial)
                with patch.object(
                    FakeSerial, attribute, new_callable=PropertyMock, create=True
                ) as setting:
                    setting.side_effect = error
                    with self.assertRaisesRegex(
                        BtpTransportError, "serial read failed"
                    ) as raised:
                        transport.read(1.0)
                self.assertIs(raised.exception.__cause__, error)

    def test_externally_closed_port_is_reported_as_transport_error(self) -> None:
        serial = FakeSerial([])
        transport = SerialByteTransport(serial)
        serial.close()
        with self.assertRaisesRegex(BtpTransportError, "serial read failed") as raised:
            transport.read(1.0)
        self.assertIsInstance(raised.exception.__cause__, PortNotOpenError)

    def test_close_is_idempotent_and_rejects_future_io(self) -> None:
        serial = FakeSerial([])
        transport = SerialByteTransport(serial)
        transport.close()
        transport.close()
        self.assertEqual(serial.close_calls, 1)
        with self.assertRaisesRegex(BtpTransportError, "closed"):
            transport.read(1.0)
        with self.assertRaisesRegex(BtpTransportError, "closed"):
            transport.write(b"request")
        self.assertEqual(serial.read_calls, [])
        self.assertEqual(serial.writes, [])


if __name__ == "__main__":
    unittest.main()
