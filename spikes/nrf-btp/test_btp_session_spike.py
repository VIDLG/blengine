from __future__ import annotations

import unittest
from collections import deque

from btp_session_spike import BtpSession
from btp_spike import BtpFrame, BtpSessionError


class FakeTransport:
    def __init__(self, incoming: list[object]) -> None:
        self.incoming = deque(incoming)
        self.sent: list[BtpFrame] = []
        self.closed = False

    def send(self, frame: BtpFrame) -> None:
        self.sent.append(frame)

    def receive(self, timeout: float) -> BtpFrame:
        del timeout
        if not self.incoming:
            raise TimeoutError("fake timeout")
        item = self.incoming.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class BtpSessionTests(unittest.TestCase):
    def test_request_preserves_events_and_sends_one_frame(self) -> None:
        event = BtpFrame(1, 0x80, 0, b"connected")
        response = BtpFrame(1, 2, 0, b"ok")
        transport = FakeTransport([event, response])
        session = BtpSession(transport)

        self.assertEqual(session.request(service=1, opcode=2), response)
        self.assertEqual(session.pop_event(), event)
        self.assertEqual(transport.sent, [BtpFrame(1, 2, 0)])
        self.assertFalse(transport.closed)

    def test_timeout_closes_and_invalidates_session(self) -> None:
        transport = FakeTransport([])
        session = BtpSession(transport)

        with self.assertRaises(TimeoutError):
            session.request(service=1, opcode=2, timeout=0.01)
        self.assertTrue(transport.closed)
        with self.assertRaises(BtpSessionError):
            session.request(service=1, opcode=3)

    def test_eof_closes_session(self) -> None:
        transport = FakeTransport([EOFError("device closed")])
        session = BtpSession(transport)

        with self.assertRaises(EOFError):
            session.request(service=1, opcode=2)
        self.assertTrue(transport.closed)

    def test_wrong_response_is_terminal(self) -> None:
        transport = FakeTransport([BtpFrame(1, 3, 0)])
        session = BtpSession(transport)

        with self.assertRaises(BtpSessionError):
            session.request(service=1, opcode=2)
        self.assertTrue(transport.closed)


if __name__ == "__main__":
    unittest.main()
