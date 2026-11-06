from __future__ import annotations

import unittest
from collections import deque
from unittest.mock import patch

from tests.peripherals.nrf52840.btp_protocol import BtpFrame, BtpSessionError
from tests.peripherals.nrf52840.btp_session import BtpSession


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TimedTransport:
    """Advance a fake clock on I/O, optionally modeling late native delivery."""

    def __init__(
        self,
        clock: FakeClock,
        frames: list[tuple[float, BtpFrame]],
        *,
        honor_timeout: bool = True,
        send_delay: float = 0.0,
    ) -> None:
        self.clock = clock
        self.frames = deque(frames)
        self.honor_timeout = honor_timeout
        self.send_delay = send_delay
        self.sent: list[BtpFrame] = []
        self.timeouts: list[float] = []
        self.close_calls = 0

    def send(self, frame: BtpFrame) -> None:
        self.sent.append(frame)
        self.clock.now += self.send_delay

    def receive(self, timeout: float) -> BtpFrame:
        self.timeouts.append(timeout)
        delay, frame = self.frames[0]
        if self.honor_timeout and delay > timeout:
            self.clock.now += timeout
            raise TimeoutError("fake receive timed out")
        self.clock.now += delay
        self.frames.popleft()
        return frame

    def close(self) -> None:
        self.close_calls += 1


class BtpSessionDeadlineTests(unittest.TestCase):
    def assert_fail_closed_once(
        self, session: BtpSession, transport: TimedTransport
    ) -> None:
        self.assertEqual(transport.close_calls, 1)
        session.close()
        session.close()
        with self.assertRaisesRegex(BtpSessionError, "closed"):
            session.request(service=1, opcode=3)
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(len(transport.sent), 1)

    def test_ten_events_cannot_renew_one_second_request_deadline(self) -> None:
        clock = FakeClock()
        events = [BtpFrame(1, 0x82, 0, bytes([i])) for i in range(10)]
        response = BtpFrame(1, 3, 0, b"status")
        transport = TimedTransport(
            clock, [(0.9, event) for event in events] + [(0.0, response)]
        )
        session = BtpSession(transport)

        with (
            patch("tests.peripherals.nrf52840.btp_session.time.monotonic", clock),
            self.assertRaises(TimeoutError),
        ):
            session.request(service=1, opcode=3, timeout=1.0)

        self.assertAlmostEqual(clock.now, 1.0)
        self.assertEqual(len(transport.timeouts), 2)
        self.assertAlmostEqual(transport.timeouts[0], 1.0)
        self.assertAlmostEqual(transport.timeouts[1], 0.1)
        self.assertEqual(len(transport.frames), 10)
        self.assertEqual(session.pop_event(), events[0])
        self.assertIsNone(session.pop_event())
        self.assert_fail_closed_once(session, transport)

    def test_late_event_cannot_trigger_another_receive_after_deadline(self) -> None:
        clock = FakeClock()
        event = BtpFrame(1, 0x82, 0, b"peer")
        transport = TimedTransport(clock, [(0.9, event)] * 10, honor_timeout=False)
        session = BtpSession(transport)

        with (
            patch("tests.peripherals.nrf52840.btp_session.time.monotonic", clock),
            self.assertRaisesRegex(TimeoutError, "BTP request timed out"),
        ):
            session.request(service=1, opcode=3, timeout=1.0)

        self.assertEqual(len(transport.timeouts), 2)
        self.assertAlmostEqual(transport.timeouts[1], 0.1)
        self.assertAlmostEqual(clock.now, 1.8)
        self.assertEqual(session.pop_event(), event)
        self.assertEqual(session.pop_event(), event)
        self.assertIsNone(session.pop_event())
        self.assert_fail_closed_once(session, transport)

    def test_response_at_or_after_deadline_cannot_succeed(self) -> None:
        for response_delay in (0.1, 0.2):
            with self.subTest(response_delay=response_delay):
                clock = FakeClock()
                event = BtpFrame(1, 0x82, 0, b"peer")
                response = BtpFrame(1, 3, 0, b"status")
                transport = TimedTransport(
                    clock,
                    [(0.9, event), (response_delay, response)],
                    honor_timeout=False,
                )
                session = BtpSession(transport)
                with (
                    patch(
                        "tests.peripherals.nrf52840.btp_session.time.monotonic", clock
                    ),
                    self.assertRaisesRegex(TimeoutError, "BTP request timed out"),
                ):
                    session.request(service=1, opcode=3, timeout=1.0)
                self.assertEqual(len(transport.timeouts), 2)
                self.assertAlmostEqual(transport.timeouts[1], 0.1)
                self.assertEqual(session.pop_event(), event)
                self.assertIsNone(session.pop_event())
                self.assert_fail_closed_once(session, transport)

    def test_timely_events_are_buffered_and_response_is_returned(self) -> None:
        clock = FakeClock()
        connected = BtpFrame(1, 0x82, 0, b"peer")
        disconnected = BtpFrame(1, 0x83, 0, b"peer")
        response = BtpFrame(1, 3, 0xFF, b"status")
        transport = TimedTransport(
            clock, [(0.2, connected), (0.3, disconnected), (0.1, response)]
        )
        session = BtpSession(transport)

        with patch("tests.peripherals.nrf52840.btp_session.time.monotonic", clock):
            self.assertEqual(
                session.request(
                    service=1, opcode=3, index=0xFF, payload=b"request", timeout=1.0
                ),
                response,
            )

        self.assertEqual(transport.sent, [BtpFrame(1, 3, 0xFF, b"request")])
        self.assertEqual(transport.timeouts, [1.0, 0.8, 0.5])
        self.assertAlmostEqual(clock.now, 0.6)
        self.assertEqual(session.pop_event(), connected)
        self.assertEqual(session.pop_event(), disconnected)
        self.assertIsNone(session.pop_event())
        self.assertEqual(transport.close_calls, 0)

    def test_invalid_timeout_is_rejected_before_dispatch_without_poisoning_session(
        self,
    ) -> None:
        clock = FakeClock()
        response = BtpFrame(1, 3, 0, b"status")
        transport = TimedTransport(clock, [(0.1, response)])
        session = BtpSession(transport)

        with patch("tests.peripherals.nrf52840.btp_session.time.monotonic", clock):
            for timeout in (0.0, -1.0, float("nan"), float("inf"), float("-inf")):
                with (
                    self.subTest(timeout=timeout),
                    self.assertRaisesRegex(BtpSessionError, "positive and finite"),
                ):
                    session.request(service=1, opcode=3, timeout=timeout)
            self.assertEqual(transport.sent, [])
            self.assertEqual(transport.timeouts, [])
            self.assertEqual(transport.close_calls, 0)
            self.assertEqual(session.request(service=1, opcode=3), response)

    def test_send_time_is_part_of_request_budget(self) -> None:
        for send_delay in (0.4, 1.0):
            with self.subTest(send_delay=send_delay):
                clock = FakeClock()
                response = BtpFrame(1, 3, 0, b"status")
                transport = TimedTransport(
                    clock, [(0.1, response)], send_delay=send_delay
                )
                session = BtpSession(transport)
                with patch(
                    "tests.peripherals.nrf52840.btp_session.time.monotonic", clock
                ):
                    if send_delay < 1.0:
                        self.assertEqual(
                            session.request(service=1, opcode=3, timeout=1.0), response
                        )
                        self.assertEqual(transport.timeouts, [0.6])
                        self.assertEqual(transport.close_calls, 0)
                    else:
                        with self.assertRaisesRegex(
                            TimeoutError, "BTP request timed out"
                        ):
                            session.request(service=1, opcode=3, timeout=1.0)
                        self.assertEqual(transport.timeouts, [])
                        self.assert_fail_closed_once(session, transport)


if __name__ == "__main__":
    unittest.main()
