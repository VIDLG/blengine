"""Hardware-free tests for the BleHub-owned BTP attestation extension."""

from __future__ import annotations

import struct
import unittest
from collections import deque
from dataclasses import replace
from typing import cast

from .btp_commands import BtpCommandError, VendorCommands
from .btp_protocol import BtpFrame
from .btp_session import BtpSession
from .fixture_attestation import InstalledImageAttestation
from .fixture_attestation_wire import (
    MAX_PAYLOAD_LENGTH,
    READ_IMAGE_ATTESTATION,
    VENDOR,
    FixtureAttestationWireError,
    decode_attestation,
    encode_attestation,
)
from .fixture_contract import MULTIFUNCTION_CAPABILITIES

ATTESTATION = InstalledImageAttestation(
    image_id="nrf-multifunction-rf-v2-a",
    image_version="2",
    image_sha256="ab" * 32,  # Synthetic parser sample, never an exported contract.
    board="nrf52840dk/nrf52840",
    profile_id="blehub-nrf52840-multifunction-v2-a",
    service_uuid="fd600000-0000-4000-8000-000000000000",
    local_name="BleHubMultiRfV2DKA",
    capabilities=frozenset(MULTIFUNCTION_CAPABILITIES),
)


class FakeSession:
    def __init__(self, responses: list[BtpFrame]) -> None:
        self.responses = deque(responses)
        self.requests: list[dict[str, object]] = []

    def request(self, **kwargs: object) -> BtpFrame:
        self.requests.append(kwargs)
        return self.responses.popleft()


class FixtureAttestationWireTests(unittest.TestCase):
    def test_round_trip_preserves_installed_identity(self) -> None:
        payload = encode_attestation(ATTESTATION)
        self.assertEqual(decode_attestation(payload), ATTESTATION)

    def test_btp_vendor_command_uses_reserved_service_and_opcode(self) -> None:
        fake_session = FakeSession(
            [
                BtpFrame(
                    VENDOR,
                    READ_IMAGE_ATTESTATION,
                    0xFF,
                    encode_attestation(ATTESTATION),
                )
            ]
        )
        session = cast(BtpSession, fake_session)
        self.assertEqual(VendorCommands(session).read_image_attestation(), ATTESTATION)
        self.assertEqual(
            fake_session.requests,
            [{"service": VENDOR, "opcode": READ_IMAGE_ATTESTATION, "index": 0xFF}],
        )

    def test_btp_error_is_not_interpreted_as_attestation(self) -> None:
        session = cast(
            BtpSession,
            FakeSession([BtpFrame(VENDOR, 0, 0xFF, b"\x02")]),
        )
        with self.assertRaisesRegex(
            BtpCommandError, "does not implement image attestation"
        ):
            VendorCommands(session).read_image_attestation()

    def test_unknown_duplicate_and_truncated_payloads_fail_closed(self) -> None:
        payload = bytearray(encode_attestation(ATTESTATION))
        first_tag_offset = 2
        second_field_offset = (
            first_tag_offset + 3 + int.from_bytes(payload[3:5], "little")
        )
        payload[first_tag_offset] = 0xFE
        with self.assertRaises(FixtureAttestationWireError):
            decode_attestation(bytes(payload))

        payload = bytearray(encode_attestation(ATTESTATION))
        payload[second_field_offset] = payload[first_tag_offset]
        with self.assertRaises(FixtureAttestationWireError):
            decode_attestation(bytes(payload))

        with self.assertRaises(FixtureAttestationWireError):
            decode_attestation(encode_attestation(ATTESTATION)[:-1])

    def test_invalid_sha_and_duplicate_capability_are_rejected_on_encode(self) -> None:
        invalid_sha = replace(ATTESTATION, image_sha256="00")
        with self.assertRaises(FixtureAttestationWireError):
            encode_attestation(invalid_sha)

        duplicate_capability = replace(
            ATTESTATION,
            capabilities=cast(frozenset[str], ["normal-rf", "normal-rf"]),
        )
        with self.assertRaises(FixtureAttestationWireError):
            encode_attestation(duplicate_capability)

    def test_firmware_encoder_utf8_budgets_are_exact_not_character_counts(self) -> None:
        capabilities = frozenset(f"{i:02d}" + "c" * 30 for i in range(16))
        # Five text fields total exactly 320 UTF-8 bytes; worst firmware envelope.
        maximum = replace(
            ATTESTATION,
            image_id="é" * 80,
            image_version="v" * 40,
            board="b" * 40,
            profile_id="p" * 40,
            local_name="n" * 40,
            capabilities=capabilities,
        )
        payload = encode_attestation(maximum)
        self.assertEqual(len(payload), 923)
        self.assertEqual(decode_attestation(payload), maximum)
        for invalid in (
            replace(maximum, local_name="n" * 41),
            replace(maximum, capabilities=capabilities | {"extra"}),
            replace(maximum, capabilities=frozenset({"é" * 16 + "x"})),
            replace(maximum, capabilities=frozenset()),
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(FixtureAttestationWireError),
            ):
                encode_attestation(invalid)
        valid = replace(maximum, capabilities=frozenset({"é" * 16}))
        self.assertEqual(decode_attestation(encode_attestation(valid)), valid)

    def test_schema_decoder_accepts_1019_rejects_1020_without_changing_btp(
        self,
    ) -> None:
        # Build actual schema-1 TLVs, not padding or a mocked length. The schema
        # has no family-specific five-text budget; current firmware encoding does.
        def schema_payload(text_length: int) -> bytes:
            values = (
                b"x" * text_length,
                b"v",
                bytes(32),
                b"b",
                b"p",
                bytes(16),
                b"n",
                b"\x01\x01c",
            )
            return b"\x01\x08" + b"".join(
                bytes([tag]) + struct.pack("<H", len(value)) + value
                for tag, value in enumerate(values, 1)
            )

        boundary = schema_payload(938)
        self.assertEqual(MAX_PAYLOAD_LENGTH, 1019)
        self.assertEqual(len(boundary), 1019)
        self.assertEqual(decode_attestation(boundary).image_id, "x" * 938)
        self.assertEqual(
            len(BtpFrame(VENDOR, READ_IMAGE_ATTESTATION, 255, boundary).encode()), 1024
        )
        too_large = schema_payload(939)
        self.assertEqual(len(too_large), 1020)
        with self.assertRaisesRegex(FixtureAttestationWireError, "BTP limit"):
            decode_attestation(too_large)
        # The generic framing contract is intentionally untouched.
        frame = BtpFrame(VENDOR, READ_IMAGE_ATTESTATION, 255, too_large)
        self.assertEqual(BtpFrame.decode(frame.encode()), frame)


if __name__ == "__main__":
    unittest.main()
