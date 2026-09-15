from __future__ import annotations

import unittest

from btp_spike import (
    BtpDispatcher,
    BtpFrame,
    BtpFrameError,
    BtpSessionError,
    BtpStreamParser,
)


class BtpFrameTests(unittest.TestCase):
    def test_round_trip_uses_little_endian_header(self) -> None:
        frame = BtpFrame(2, 3, 4, b"abc")
        encoded = frame.encode()
        self.assertEqual(encoded[:5], b"\x02\x03\x04\x03\x00")
        self.assertEqual(BtpFrame.decode(encoded), frame)

    def test_parser_accepts_fragmented_header_and_payload(self) -> None:
        encoded = BtpFrame(1, 0x80, 0, b"payload").encode()
        parser = BtpStreamParser()
        self.assertEqual(parser.feed(encoded[:2]), [])
        self.assertEqual(parser.feed(encoded[2:7]), [])
        self.assertEqual(parser.feed(encoded[7:]), [BtpFrame(1, 0x80, 0, b"payload")])
        parser.finish()

    def test_parser_rejects_truncated_stream(self) -> None:
        parser = BtpStreamParser()
        parser.feed(BtpFrame(1, 2, 0, b"x").encode()[:-1])
        with self.assertRaises(BtpFrameError):
            parser.finish()


class BtpDispatcherTests(unittest.TestCase):
    def test_event_is_retained_while_response_is_pending(self) -> None:
        dispatcher = BtpDispatcher()
        dispatcher.begin(service=1, opcode=2)
        event = BtpFrame(1, 0x80, 0, b"event")
        response = BtpFrame(1, 2, 0, b"response")
        self.assertIsNone(dispatcher.receive(event))
        self.assertEqual(dispatcher.receive(response), response)
        self.assertEqual(dispatcher.pop_event(), event)

    def test_wrong_response_and_concurrent_request_are_rejected(self) -> None:
        dispatcher = BtpDispatcher()
        dispatcher.begin(service=1, opcode=2)
        with self.assertRaises(BtpSessionError):
            dispatcher.begin(service=1, opcode=3)
        with self.assertRaises(BtpSessionError):
            dispatcher.receive(BtpFrame(1, 3, 0))

    def test_close_rejects_late_frames(self) -> None:
        dispatcher = BtpDispatcher()
        dispatcher.close()
        with self.assertRaises(BtpSessionError):
            dispatcher.receive(BtpFrame(1, 0x80, 0))


if __name__ == "__main__":
    unittest.main()
