from __future__ import annotations

import unittest
from collections import deque

from tests.peripherals.nrf52840.btp_commands import BtpCommandError
from tests.peripherals.nrf52840.btp_protocol import BtpFrame
from tests.peripherals.nrf52840.gatt_commands import (
    GattCommands,
    tester_characteristic_native_permissions,
    uuid_to_le_bytes,
)


class FakeSession:
    def __init__(self, responses: list[BtpFrame]) -> None:
        self.responses = deque(responses)
        self.requests: list[dict[str, object]] = []

    def request(self, **kwargs: object) -> BtpFrame:
        self.requests.append(kwargs)
        return self.responses.popleft()


class GattCommandsTests(unittest.TestCase):
    def test_tester_native_permissions_are_not_btp_authorization_bits(self) -> None:
        for supplied, native in (
            (0x03, 0x43),
            (0x43, 0x43),
            (0x40, 0x41),
            (0x80, 0x42),
            (0x44, 0x44),
            (0x88, 0x48),
            (0x50, 0x50),
            (0xA0, 0x60),
        ):
            with self.subTest(supplied=supplied):
                self.assertEqual(
                    tester_characteristic_native_permissions(supplied), native
                )
        for invalid in (-1, 256):
            with self.subTest(invalid=invalid), self.assertRaises(BtpCommandError):
                tester_characteristic_native_permissions(invalid)

    def test_uuid_conversion_is_little_endian(self) -> None:
        self.assertEqual(uuid_to_le_bytes("180D"), b"\x0d\x18")
        self.assertEqual(
            uuid_to_le_bytes("00112233-4455-6677-8899-aabbccddeeff"),
            bytes.fromhex("ffeeddccbbaa99887766554433221100"),
        )

    def test_add_service_encodes_type_and_uuid(self) -> None:
        session = FakeSession([BtpFrame(2, 2, 0)])
        GattCommands(session).add_service(0, "180D")
        self.assertEqual(session.requests[0]["payload"], b"\x00\x02\x0d\x18")

    def test_add_characteristic_and_set_value_encode_little_endian_fields(self) -> None:
        session = FakeSession([BtpFrame(2, 3, 0), BtpFrame(2, 6, 0)])
        commands = GattCommands(session)
        commands.add_characteristic(0x1234, 0x0A, 0x03, "180F")
        commands.set_value(0x1234, b"\xde\xad")
        self.assertEqual(
            session.requests[0]["payload"], b"\x34\x12\x0a\x03\x02\x0f\x18"
        )
        self.assertEqual(session.requests[1]["payload"], b"\x34\x12\x02\x00\xde\xad")

    def test_attribute_value_encodes_peer_and_handle(self) -> None:
        session = FakeSession([BtpFrame(2, 0x1D, 0, b"\x00\x02\x00\xde\xad")])
        value = GattCommands(session).get_attribute_value(0x1234)
        self.assertEqual(value.att_response, 0)
        self.assertEqual(value.value, b"\xde\xad")
        self.assertEqual(
            session.requests[0]["payload"],
            b"\x00\x00\x00\x00\x00\x00\x00\x34\x12",
        )

    def test_handle_response_must_be_uint16(self) -> None:
        session = FakeSession([BtpFrame(2, 0x22, 0, b"\x34\x12")])
        self.assertEqual(GattCommands(session).get_handle_from_uuid("180D"), 0x1234)
        bad = FakeSession([BtpFrame(2, 0x22, 0, b"\x01")])
        with self.assertRaises(BtpCommandError):
            GattCommands(bad).get_handle_from_uuid("180D")

    def test_invalid_uuid_is_rejected(self) -> None:
        with self.assertRaises(BtpCommandError):
            uuid_to_le_bytes("not-a-uuid")


if __name__ == "__main__":
    unittest.main()
