"""Minimal GAP Peripheral-control BTP command wrappers."""

from __future__ import annotations

import struct

from .btp_commands import GAP, BtpCommandError, require_success
from .btp_session import BtpSession

GAP_READ_CONTROLLER_INFO = 0x03
GAP_SET_POWERED = 0x05
GAP_SET_CONNECTABLE = 0x06
GAP_SET_DISCOVERABLE = 0x08
GAP_START_ADVERTISING = 0x0A
GAP_STOP_ADVERTISING = 0x0B
GAP_DISCONNECT = 0x0F
GAP_DEVICE_CONNECTED = 0x82
GAP_DEVICE_DISCONNECTED = 0x83

GAP_GENERAL_DISCOVERABLE = 0x01
GAP_NON_DISCOVERABLE = 0x00

CONTROLLER_INFO = struct.Struct("<6sII3s249s11s")


def _tlv(data: dict[int, bytes]) -> bytes:
    encoded = bytearray()
    for ad_type, value in data.items():
        if not 0 <= ad_type <= 0xFF or len(value) > 0xFF:
            raise BtpCommandError("advertising field does not fit BTP AD format")
        encoded.extend((ad_type, len(value)))
        encoded.extend(value)
    return bytes(encoded)


class GapCommands:
    def __init__(self, session: BtpSession) -> None:
        self._session = session

    def read_controller_info(self) -> bytes:
        response = self._session.request(
            service=GAP, opcode=GAP_READ_CONTROLLER_INFO, index=0
        )
        if len(response.payload) < CONTROLLER_INFO.size:
            raise BtpCommandError("GAP controller-info response is too short")
        return response.payload

    def set_powered(self, powered: bool) -> None:
        self._set_one_byte(GAP_SET_POWERED, int(powered), "GAP set powered")

    def set_connectable(self, connectable: bool) -> None:
        self._set_one_byte(GAP_SET_CONNECTABLE, int(connectable), "GAP set connectable")

    def set_discoverable(self, discoverable: bool) -> None:
        value = GAP_GENERAL_DISCOVERABLE if discoverable else GAP_NON_DISCOVERABLE
        self._set_one_byte(GAP_SET_DISCOVERABLE, value, "GAP set discoverable")

    def start_advertising(
        self,
        advertising_data: dict[int, bytes],
        scan_response: dict[int, bytes] | None = None,
        *,
        duration: int = 0,
        own_address_type: int = 0,
    ) -> None:
        ad = _tlv(advertising_data)
        sd = _tlv(scan_response or {})
        if not 0 <= duration <= 0xFFFFFFFF:
            raise BtpCommandError("advertising duration does not fit uint32")
        if not 0 <= own_address_type <= 0xFF:
            raise BtpCommandError("own address type does not fit one byte")
        payload = bytes((len(ad), len(sd))) + ad + sd + struct.pack("<I", duration)
        payload += bytes((own_address_type,))
        response = self._session.request(
            service=GAP,
            opcode=GAP_START_ADVERTISING,
            index=0,
            payload=payload,
        )
        require_success(response, operation="GAP start advertising")

    def disconnect(self, address: str, address_type: int) -> None:
        compact = address.replace(":", "")
        if len(compact) != 12:
            raise BtpCommandError("peer address must contain six bytes")
        try:
            encoded_address = bytes.fromhex(compact)[::-1]
        except ValueError as error:
            raise BtpCommandError("peer address is not hexadecimal") from error
        if not 0 <= address_type <= 0xFF:
            raise BtpCommandError("peer address type does not fit one byte")
        response = self._session.request(
            service=GAP,
            opcode=GAP_DISCONNECT,
            index=0,
            payload=bytes((address_type,)) + encoded_address,
        )
        require_success(response, operation="GAP disconnect")

    def stop_advertising(self) -> None:
        response = self._session.request(
            service=GAP, opcode=GAP_STOP_ADVERTISING, index=0
        )
        require_success(response, operation="GAP stop advertising")

    def _set_one_byte(self, opcode: int, value: int, operation: str) -> None:
        response = self._session.request(
            service=GAP, opcode=opcode, index=0, payload=bytes((value,))
        )
        require_success(response, operation=operation)
