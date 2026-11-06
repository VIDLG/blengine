"""BleHub-owned BTP vendor wire format for installed fixture attestation.

The standard BTP Core service has no firmware-image identity command.  This
codec deliberately lives in the nRF provider and uses the reserved vendor
service; firmware support must be added separately before a physical preflight
can claim success.
"""

from __future__ import annotations

import struct
import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .fixture_attestation import InstalledImageAttestation

INDEX_NONE = 0xFF
VENDOR = 0xFF
ATTESTATION_SCHEMA_VERSION = 1
READ_IMAGE_ATTESTATION = 0x01
MAX_PAYLOAD_LENGTH = 1019  # Tester BTP_MTU (1024) minus its five-byte header.
FIRMWARE_CAPABILITY_MAX = 16
FIRMWARE_CAPABILITY_NAME_MAX = 32
FIRMWARE_TEXT_BUDGET = 320
DIGEST_SLOT_MARKER = b"BLEHUB_ATTESTATION_SHA256_SLOT_V1"
DIGEST_SLOT_LENGTH = 64

_TAG_IMAGE_ID = 1
_TAG_IMAGE_VERSION = 2
_TAG_IMAGE_SHA256 = 3
_TAG_BOARD = 4
_TAG_PROFILE_ID = 5
_TAG_SERVICE_UUID = 6
_TAG_LOCAL_NAME = 7
_TAG_CAPABILITIES = 8
_REQUIRED_TAGS = frozenset(
    {
        _TAG_IMAGE_ID,
        _TAG_IMAGE_VERSION,
        _TAG_IMAGE_SHA256,
        _TAG_BOARD,
        _TAG_PROFILE_ID,
        _TAG_SERVICE_UUID,
        _TAG_LOCAL_NAME,
        _TAG_CAPABILITIES,
    }
)


class FixtureAttestationWireError(ValueError):
    """Raised when the firmware attestation payload is malformed."""


def _text(value: bytes, *, field: str) -> str:
    if not value or b"\x00" in value:
        raise FixtureAttestationWireError(
            f"{field} must be non-empty UTF-8 without NUL"
        )
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FixtureAttestationWireError(f"{field} is not valid UTF-8") from error
    if not decoded.strip():
        raise FixtureAttestationWireError(f"{field} must not be blank")
    return decoded


def _capabilities(value: bytes) -> frozenset[str]:
    if not value:
        raise FixtureAttestationWireError("capabilities payload is empty")
    count = value[0]
    if count == 0:
        raise FixtureAttestationWireError("capabilities must not be empty")
    offset = 1
    decoded: list[str] = []
    for _ in range(count):
        if offset >= len(value):
            raise FixtureAttestationWireError("capabilities payload is truncated")
        length = value[offset]
        offset += 1
        end = offset + length
        if length == 0 or end > len(value):
            raise FixtureAttestationWireError("capability has an invalid length")
        decoded.append(_text(value[offset:end], field="capability"))
        offset = end
    if offset != len(value) or len(set(decoded)) != len(decoded):
        raise FixtureAttestationWireError(
            "capabilities payload has trailing or duplicate data"
        )
    return frozenset(decoded)


def decode_attestation(payload: bytes) -> InstalledImageAttestation:
    """Decode schema 1, independent of a particular firmware family's limits.

    The schema uses u8 capability counts/name lengths and u16 text lengths.
    Current Tester build budgets constrain encoding below, not historical or
    other schema-1 identities decoded here. Neither path authorizes a family.
    """
    if len(payload) > MAX_PAYLOAD_LENGTH:
        raise FixtureAttestationWireError("attestation payload exceeds BTP limit")
    if len(payload) < 2:
        raise FixtureAttestationWireError("attestation payload is truncated")
    if payload[0] != ATTESTATION_SCHEMA_VERSION:
        raise FixtureAttestationWireError(
            f"unsupported attestation schema version: {payload[0]}"
        )
    field_count = payload[1]
    if field_count != len(_REQUIRED_TAGS):
        raise FixtureAttestationWireError("attestation field count is not exact")
    offset = 2
    fields: dict[int, bytes] = {}
    for _ in range(field_count):
        if offset + 3 > len(payload):
            raise FixtureAttestationWireError("attestation field header is truncated")
        tag = payload[offset]
        length = struct.unpack_from("<H", payload, offset + 1)[0]
        offset += 3
        end = offset + length
        if tag not in _REQUIRED_TAGS or tag in fields:
            raise FixtureAttestationWireError(
                f"unknown or duplicate attestation tag: {tag}"
            )
        if end > len(payload):
            raise FixtureAttestationWireError("attestation field is truncated")
        fields[tag] = payload[offset:end]
        offset = end
    if offset != len(payload) or set(fields) != _REQUIRED_TAGS:
        raise FixtureAttestationWireError(
            "attestation payload has trailing or missing fields"
        )

    image_sha256 = fields[_TAG_IMAGE_SHA256]
    service_uuid = fields[_TAG_SERVICE_UUID]
    if len(image_sha256) != 32:
        raise FixtureAttestationWireError("image SHA-256 must contain 32 raw bytes")
    if len(service_uuid) != 16:
        raise FixtureAttestationWireError("service UUID must contain 16 raw bytes")
    from .fixture_attestation import InstalledImageAttestation

    return InstalledImageAttestation(
        image_id=_text(fields[_TAG_IMAGE_ID], field="image_id"),
        image_version=_text(fields[_TAG_IMAGE_VERSION], field="image_version"),
        image_sha256=image_sha256.hex(),
        board=_text(fields[_TAG_BOARD], field="board"),
        profile_id=_text(fields[_TAG_PROFILE_ID], field="profile_id"),
        service_uuid=str(uuid.UUID(bytes=service_uuid)),
        local_name=_text(fields[_TAG_LOCAL_NAME], field="local_name"),
        capabilities=_capabilities(fields[_TAG_CAPABILITIES]),
    )


def _encoded_text(value: str, *, field: str) -> bytes:
    encoded = value.encode("utf-8")
    _text(encoded, field=field)
    return encoded


def _encoded_capabilities(values: Iterable[str]) -> bytes:
    capabilities = sorted(values)
    if not capabilities or len(capabilities) > FIRMWARE_CAPABILITY_MAX:
        raise FixtureAttestationWireError(
            "firmware capabilities count is outside 1..16"
        )
    if len(set(capabilities)) != len(capabilities):
        raise FixtureAttestationWireError("capabilities must not contain duplicates")
    result = bytearray((len(capabilities),))
    for capability in capabilities:
        encoded = _encoded_text(capability, field="capability")
        if len(encoded) > FIRMWARE_CAPABILITY_NAME_MAX:
            raise FixtureAttestationWireError(
                "firmware capability is longer than 32 bytes"
            )
        result.extend((len(encoded),))
        result.extend(encoded)
    return bytes(result)


def encode_attestation(attestation: InstalledImageAttestation) -> bytes:
    """Encode a canonical response within current Tester firmware build budgets."""
    try:
        image_sha256 = bytes.fromhex(attestation.image_sha256)
        service_uuid = uuid.UUID(attestation.service_uuid).bytes
    except (ValueError, TypeError) as error:
        raise FixtureAttestationWireError(
            "attestation identity has invalid binary fields"
        ) from error
    if len(image_sha256) != 32:
        raise FixtureAttestationWireError("image SHA-256 must contain 32 raw bytes")
    fields = (
        (_TAG_IMAGE_ID, _encoded_text(attestation.image_id, field="image_id")),
        (
            _TAG_IMAGE_VERSION,
            _encoded_text(attestation.image_version, field="image_version"),
        ),
        (_TAG_IMAGE_SHA256, image_sha256),
        (_TAG_BOARD, _encoded_text(attestation.board, field="board")),
        (_TAG_PROFILE_ID, _encoded_text(attestation.profile_id, field="profile_id")),
        (_TAG_SERVICE_UUID, service_uuid),
        (_TAG_LOCAL_NAME, _encoded_text(attestation.local_name, field="local_name")),
        (_TAG_CAPABILITIES, _encoded_capabilities(attestation.capabilities)),
    )
    text_tags = {
        _TAG_IMAGE_ID,
        _TAG_IMAGE_VERSION,
        _TAG_BOARD,
        _TAG_PROFILE_ID,
        _TAG_LOCAL_NAME,
    }
    if (
        sum(len(value) for tag, value in fields if tag in text_tags)
        > FIRMWARE_TEXT_BUDGET
    ):
        raise FixtureAttestationWireError("firmware five-text budget exceeds 320 bytes")
    payload = bytearray((ATTESTATION_SCHEMA_VERSION, len(fields)))
    for tag, value in fields:
        if len(value) > 0xFFFF:
            raise FixtureAttestationWireError(f"attestation field {tag} is too large")
        payload.append(tag)
        payload.extend(struct.pack("<H", len(value)))
        payload.extend(value)
    if len(payload) > MAX_PAYLOAD_LENGTH:
        raise FixtureAttestationWireError("encoded attestation exceeds BTP limit")
    return bytes(payload)


__all__ = [
    "ATTESTATION_SCHEMA_VERSION",
    "DIGEST_SLOT_LENGTH",
    "DIGEST_SLOT_MARKER",
    "INDEX_NONE",
    "MAX_PAYLOAD_LENGTH",
    "READ_IMAGE_ATTESTATION",
    "VENDOR",
    "FixtureAttestationWireError",
    "decode_attestation",
    "encode_attestation",
]
