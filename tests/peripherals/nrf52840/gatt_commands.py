"""Minimal GATT server BTP command wrappers for the nRF fixture."""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass

from .btp_commands import GATT, BtpCommandError, require_success
from .btp_session import BtpSession

GATT_ADD_SERVICE = 0x02
GATT_RESET_SERVER = 0x08
GATT_ADD_CHARACTERISTIC = 0x03
GATT_ADD_DESCRIPTOR = 0x04
GATT_SET_VALUE = 0x06
GATT_START_SERVER = 0x07
GATT_GET_ATTRIBUTES = 0x1C
GATT_GET_ATTRIBUTE_VALUE = 0x1D
GATT_GET_HANDLE_FROM_UUID = 0x22
GATT_REMOVE_HANDLE_FROM_DB = 0x23


@dataclass(frozen=True)
class GattAttributeValue:
    att_response: int
    value: bytes


@dataclass(frozen=True)
class GattAttribute:
    """GET_ATTRIBUTES entry; permissions are Zephyr native bits, not ADD input bits."""

    handle: int
    permissions: int
    uuid: str


def tester_characteristic_native_permissions(btp_permissions: int) -> int:
    """Map ADD_CHARACTERISTIC input to GET_ATTRIBUTES native permissions.

    Tester consumes BTP authorization bits (0x40/0x80) into private value
    flags, then unconditionally adds native PREPARE_WRITE (also 0x40).
    GET_ATTRIBUTES exposes attr->perm, so it cannot attest authorization flags.
    Mirrors Zephyr tester alloc_characteristic(), including its legacy fallback.
    """
    if not 0 <= btp_permissions <= 0xFF:
        raise BtpCommandError("characteristic permissions do not fit one byte")
    native = btp_permissions & 0x3F
    if btp_permissions & 0x40 and not native & 0x14:
        native |= 0x01
    if btp_permissions & 0x80 and not native & 0x28:
        native |= 0x02
    return native | 0x40


def uuid_to_le_bytes(value: str) -> bytes:
    compact = value.replace("-", "")
    if len(compact) == 4:
        try:
            return bytes.fromhex(compact)[::-1]
        except ValueError as error:
            raise BtpCommandError(f"invalid 16-bit UUID: {value}") from error
    if len(compact) != 32:
        raise BtpCommandError(f"UUID must be 16-bit or 128-bit: {value}")
    try:
        return bytes.fromhex(compact)[::-1]
    except ValueError as error:
        raise BtpCommandError(f"invalid UUID: {value}") from error


def _uuid_from_le_bytes(value: bytes) -> str:
    if len(value) == 2:
        return value[::-1].hex()
    if len(value) == 16:
        return str(uuid.UUID(bytes=value[::-1]))
    raise BtpCommandError(f"unsupported GATT UUID length: {len(value)}")


def _uuid_field(value: str) -> bytes:
    encoded = uuid_to_le_bytes(value)
    return bytes((len(encoded),)) + encoded


def _handle(value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise BtpCommandError("GATT handle does not fit uint16")
    return struct.pack("<H", value)


def _byte(value: int, name: str) -> bytes:
    if not 0 <= value <= 0xFF:
        raise BtpCommandError(f"{name} does not fit one byte")
    return bytes((value,))


class GattCommands:
    def __init__(self, session: BtpSession) -> None:
        self._session = session

    def reset_server(self) -> None:
        response = self._session.request(
            service=GATT, opcode=GATT_RESET_SERVER, index=0
        )
        require_success(response, operation="GATT reset server")

    def add_service(self, service_type: int, service_uuid: str) -> None:
        payload = _byte(service_type, "service type") + _uuid_field(service_uuid)
        response = self._session.request(
            service=GATT, opcode=GATT_ADD_SERVICE, index=0, payload=payload
        )
        require_success(response, operation="GATT add service")

    def add_characteristic(
        self, handle: int, properties: int, permissions: int, characteristic_uuid: str
    ) -> None:
        payload = (
            _handle(handle)
            + _byte(properties, "characteristic properties")
            + _byte(permissions, "characteristic permissions")
            + _uuid_field(characteristic_uuid)
        )
        response = self._session.request(
            service=GATT, opcode=GATT_ADD_CHARACTERISTIC, index=0, payload=payload
        )
        require_success(response, operation="GATT add characteristic")

    def add_descriptor(
        self, handle: int, permissions: int, descriptor_uuid: str
    ) -> None:
        payload = _handle(handle) + _byte(permissions, "descriptor permissions")
        payload += _uuid_field(descriptor_uuid)
        response = self._session.request(
            service=GATT, opcode=GATT_ADD_DESCRIPTOR, index=0, payload=payload
        )
        require_success(response, operation="GATT add descriptor")

    def set_value(self, handle: int, value: bytes) -> None:
        if len(value) > 0xFFFF:
            raise BtpCommandError("attribute value does not fit uint16 length")
        payload = _handle(handle) + struct.pack("<H", len(value)) + value
        response = self._session.request(
            service=GATT, opcode=GATT_SET_VALUE, index=0, payload=payload
        )
        require_success(response, operation="GATT set value")

    def start_server(self) -> None:
        response = self._session.request(
            service=GATT, opcode=GATT_START_SERVER, index=0
        )
        require_success(response, operation="GATT start server")

    def get_attributes(
        self,
        start_handle: int = 1,
        end_handle: int = 0xFFFF,
        attribute_uuid: str | None = None,
    ) -> tuple[GattAttribute, ...]:
        payload = _handle(start_handle) + _handle(end_handle)
        payload += b"\x00" if attribute_uuid is None else _uuid_field(attribute_uuid)
        response = self._session.request(
            service=GATT, opcode=GATT_GET_ATTRIBUTES, index=0, payload=payload
        )
        if response.opcode == 0:
            raise BtpCommandError("GATT get attributes returned an error")
        data = response.payload
        if not data:
            raise BtpCommandError("GATT get attributes response is empty")
        count = data[0]
        offset = 1
        attributes: list[GattAttribute] = []
        for _ in range(count):
            if offset + 4 > len(data):
                raise BtpCommandError("GATT attribute entry is truncated")
            handle, permissions, uuid_length = struct.unpack_from("<HBB", data, offset)
            offset += 4
            if offset + uuid_length > len(data):
                raise BtpCommandError("GATT attribute UUID is truncated")
            raw_uuid = data[offset : offset + uuid_length]
            offset += uuid_length
            attributes.append(
                GattAttribute(handle, permissions, _uuid_from_le_bytes(raw_uuid))
            )
        if offset != len(data):
            raise BtpCommandError("GATT get attributes response has trailing bytes")
        return tuple(attributes)

    def get_attribute_value(
        self,
        handle: int,
        *,
        peer_address: str = "000000000000",
        peer_address_type: int = 0,
    ) -> GattAttributeValue:
        if len(peer_address) != 12:
            raise BtpCommandError("peer address must contain six octets")
        try:
            address = bytes.fromhex(peer_address)
        except ValueError as error:
            raise BtpCommandError("peer address must be hexadecimal") from error
        payload = bytes((peer_address_type,)) + address + _handle(handle)
        response = self._session.request(
            service=GATT,
            opcode=GATT_GET_ATTRIBUTE_VALUE,
            index=0,
            payload=payload,
        )
        if response.opcode == 0 or len(response.payload) < 3:
            raise BtpCommandError("GATT get attribute value returned an error")
        att_response = response.payload[0]
        value_length = struct.unpack_from("<H", response.payload, 1)[0]
        value = response.payload[3:]
        if value_length != len(value):
            raise BtpCommandError("GATT attribute value length does not match payload")
        return GattAttributeValue(att_response, value)

    def remove_service_containing_handle(self, handle: int) -> None:
        """Remove the dynamic GATT service containing handle via BTP 0x23."""
        response = self._session.request(
            service=GATT,
            opcode=GATT_REMOVE_HANDLE_FROM_DB,
            index=0,
            payload=_handle(handle),
        )
        require_success(response, operation="GATT remove service")

    def get_handle_from_uuid(self, attribute_uuid: str) -> int:
        response = self._session.request(
            service=GATT,
            opcode=GATT_GET_HANDLE_FROM_UUID,
            index=0,
            payload=_uuid_field(attribute_uuid),
        )
        if len(response.payload) != 2:
            raise BtpCommandError(
                f"GATT handle response must be two bytes, got {len(response.payload)} "
                + f"opcode=0x{response.opcode:02x} payload={response.payload.hex()}"
            )
        return struct.unpack("<H", response.payload)[0]
