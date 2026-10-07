"""Hardware-free correlation tests for the diagnostic ATT read-hold protocol."""

from __future__ import annotations

import struct
import unittest
from collections import deque
from dataclasses import dataclass

from read_hold import (
    ABORT,
    ARM,
    RELEASE,
    STATUS,
    HoldError,
    HoldState,
    ReadHold,
    Snapshot,
)


@dataclass(frozen=True)
class Response:
    service: int
    opcode: int
    index: int
    payload: bytes


class FakeSession:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = deque(responses)
        self.sent: list[tuple[int, bytes]] = []

    def request(
        self,
        *,
        service: int,
        opcode: int,
        index: int = 0,
        payload: bytes = b"",
        timeout: float = 1.0,
    ) -> Response:
        del index, timeout
        self.sent.append((opcode, payload))
        if service != 2 or not self.responses:
            raise AssertionError("unexpected BTP request")
        return self.responses.popleft()


def response(opcode: int, payload: bytes = b"") -> Response:
    return Response(2, opcode, 0, payload)


def status(state: HoldState, seq: int = 7, handle: int = 0x19) -> Response:
    return response(STATUS, struct.pack("<BIH", state, seq, handle))


class ReadHoldTests(unittest.TestCase):
    def test_exact_one_shot_release_and_terminal_correlation(self) -> None:
        session = FakeSession(
            [
                response(ARM, struct.pack("<IH", 7, 0x19)),
                status(HoldState.ARMED),
                status(HoldState.WAITING),
                response(RELEASE),
                status(HoldState.RELEASED),
            ]
        )
        hold = ReadHold(session)
        identity = hold.arm(0x19)
        self.assertEqual(hold.wait_until_held(float("inf")), identity)
        hold.release()
        self.assertEqual(hold.status().state, HoldState.RELEASED)
        with self.assertRaises(HoldError):
            hold.release()
        self.assertEqual(
            session.sent,
            [
                (ARM, b"\x19\x00"),
                (STATUS, b""),
                (STATUS, b""),
                (RELEASE, b"\x07\x00\x00\x00\x19\x00"),
                (STATUS, b""),
            ],
        )

    def test_unused_arm_can_be_aborted_but_not_released(self) -> None:
        session = FakeSession(
            [
                response(ARM, struct.pack("<IH", 8, 0x31)),
                response(ABORT),
                status(HoldState.ABORTED, 8, 0x31),
            ]
        )
        hold = ReadHold(session)
        hold.arm(0x31)
        with self.assertRaises(HoldError):
            hold.release()
        hold.abort()
        self.assertEqual(hold.status().state, HoldState.ABORTED)
        self.assertEqual(session.sent[1], (ABORT, struct.pack("<IH", 8, 0x31)))

    def test_wrong_identity_and_expiry_fail_closed(self) -> None:
        session = FakeSession(
            [
                response(ARM, struct.pack("<IH", 7, 0x19)),
                status(HoldState.WAITING, 6, 0x19),
            ]
        )
        hold = ReadHold(session)
        hold.arm(0x19)
        with self.assertRaisesRegex(HoldError, "generation"):
            hold.status()
        with self.assertRaisesRegex(HoldError, "controllable"):
            hold.release()

        expired = ReadHold(
            FakeSession(
                [
                    response(ARM, struct.pack("<IH", 7, 0x19)),
                    status(HoldState.EXPIRED),
                ]
            )
        )
        expired.arm(0x19)
        with self.assertRaisesRegex(HoldError, "EXPIRED"):
            expired.wait_until_held(float("inf"))
        with self.assertRaises(HoldError):
            expired.abort()

    def test_rejects_malformed_frames_and_btp_status_failures(self) -> None:
        for payload in (
            b"",
            b"\x02",
            struct.pack("<BIH", 99, 7, 0x19),
            struct.pack("<BIH", HoldState.IDLE, 7, 0x19),
        ):
            with self.subTest(payload=payload), self.assertRaises(HoldError):
                Snapshot.decode(payload)
        with self.assertRaisesRegex(HoldError, "rejected"):
            ReadHold(FakeSession([response(0, b"\x01")])).arm(0x19)
        with self.assertRaisesRegex(HoldError, "different"):
            ReadHold(FakeSession([response(ARM, struct.pack("<IH", 7, 0x20))])).arm(
                0x19
            )

    def test_timeout_before_att_entry_does_not_claim_waiting(self) -> None:
        hold = ReadHold(FakeSession([response(ARM, struct.pack("<IH", 1, 0x19))]))
        hold.arm(0x19)
        with self.assertRaisesRegex(HoldError, "deadline"):
            hold.wait_until_held(0.0)


if __name__ == "__main__":
    unittest.main()
