from __future__ import annotations

import unittest
from collections import deque

from tests.peripherals.nrf52840.btp_commands import BtpCommandError
from tests.peripherals.nrf52840.btp_protocol import BtpFrame
from tests.peripherals.nrf52840.gap_commands import GapCommands


class FakeSession:
    def __init__(self, responses: list[BtpFrame]) -> None:
        self.responses = deque(responses)
        self.requests: list[dict[str, object]] = []

    def request(self, **kwargs: object) -> BtpFrame:
        self.requests.append(kwargs)
        return self.responses.popleft()


class GapCommandsTests(unittest.TestCase):
    def test_set_powered_uses_controller_index_and_boolean_payload(self) -> None:
        session = FakeSession([BtpFrame(1, 5, 0)])
        GapCommands(session).set_powered(True)
        self.assertEqual(session.requests[0]["index"], 0)
        self.assertEqual(session.requests[0]["payload"], b"\x01")

    def test_start_advertising_encodes_ad_and_scan_response(self) -> None:
        session = FakeSession([BtpFrame(1, 10, 0)])
        GapCommands(session).start_advertising(
            {0x01: b"\x06", 0x09: b"nrf"}, {0x16: b"\xaa\xbb"}
        )
        payload = session.requests[0]["payload"]
        self.assertEqual(
            payload,
            b"\x08\x04\x01\x01\x06\x09\x03nrf\x16\x02\xaa\xbb"
            + b"\x00\x00\x00\x00\x00",
        )

    def test_disconnect_encodes_peer_address_and_type(self) -> None:
        session = FakeSession([BtpFrame(1, 15, 0)])
        GapCommands(session).disconnect("AA:BB:CC:DD:EE:FF", 1)
        self.assertEqual(
            session.requests[0]["payload"], bytes.fromhex("01FFEEDDCCBBAA")
        )

    def test_controller_info_requires_complete_payload(self) -> None:
        session = FakeSession([BtpFrame(1, 3, 0, b"short")])
        with self.assertRaises(BtpCommandError):
            GapCommands(session).read_controller_info()

    def test_stop_advertising_rejects_btp_error_response(self) -> None:
        session = FakeSession([BtpFrame(1, 0, 0, b"\x01")])
        with self.assertRaises(BtpCommandError):
            GapCommands(session).stop_advertising()


if __name__ == "__main__":
    unittest.main()
