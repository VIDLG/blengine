from __future__ import annotations

import unittest
from collections import deque

from tests.peripherals.nrf52840.btp_commands import BtpCommandError, CoreCommands
from tests.peripherals.nrf52840.btp_protocol import BtpFrame


class FakeSession:
    def __init__(self, responses: list[BtpFrame]) -> None:
        self.responses = deque(responses)
        self.requests: list[dict[str, object]] = []

    def request(self, **kwargs: object) -> BtpFrame:
        self.requests.append(kwargs)
        return self.responses.popleft()


class CoreCommandsTests(unittest.TestCase):
    def test_supported_services_uses_special_four_byte_response(self) -> None:
        session = FakeSession([BtpFrame(0, 2, 0xFF, (1 << 1).to_bytes(4, "little"))])
        self.assertEqual(CoreCommands(session).read_supported_services(), 2)
        self.assertEqual(session.requests[0]["service"], 0)
        self.assertEqual(session.requests[0]["opcode"], 2)
        self.assertEqual(session.requests[0]["index"], 0xFF)

    def test_register_gap_uses_empty_success_response(self) -> None:
        session = FakeSession([BtpFrame(0, 3, 0xFF)])
        CoreCommands(session).register_service(1)
        self.assertEqual(session.requests[0]["payload"], b"\x01")

    def test_nonempty_success_command_payload_is_returned_by_validator(self) -> None:
        session = FakeSession([BtpFrame(0, 3, 0xFF, b"\x01")])
        CoreCommands(session).register_service(1)

    def test_supported_commands_uses_target_service_id(self) -> None:
        session = FakeSession([BtpFrame(1, 1, 0xFF, b"\x01\x00\x00\x00")])
        self.assertEqual(CoreCommands(session).read_supported_commands(1), 1)
        self.assertEqual(session.requests[0]["service"], 1)
        self.assertNotIn("payload", session.requests[0])

    def test_supported_commands_rejects_empty_payload(self) -> None:
        session = FakeSession([BtpFrame(1, 1, 0xFF, b"")])
        with self.assertRaises(BtpCommandError):
            CoreCommands(session).read_supported_commands(1)


if __name__ == "__main__":
    unittest.main()
