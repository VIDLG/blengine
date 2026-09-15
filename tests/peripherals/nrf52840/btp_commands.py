"""Minimal Core/GAP/GATT server BTP command wrappers."""

from __future__ import annotations

from .btp_protocol import BtpFrame
from .btp_session import BtpSession

CORE = 0x00
GAP = 0x01
GATT = 0x02
INDEX_NONE = 0xFF
STATUS_SUCCESS = 0x00

CORE_READ_SUPPORTED_COMMANDS = 0x01
CORE_READ_SUPPORTED_SERVICES = 0x02
CORE_REGISTER_SERVICE = 0x03
CORE_UNREGISTER_SERVICE = 0x04

GAP_READ_CONTROLLER_INFO = 0x03
GAP_SET_POWERED = 0x05
GAP_SET_CONNECTABLE = 0x06
GAP_SET_DISCOVERABLE = 0x08
GAP_START_ADVERTISING = 0x0A
GAP_STOP_ADVERTISING = 0x0B

GATT_ADD_SERVICE = 0x02
GATT_ADD_CHARACTERISTIC = 0x03
GATT_ADD_DESCRIPTOR = 0x04
GATT_SET_VALUE = 0x06
GATT_START_SERVER = 0x07
GATT_GET_ATTRIBUTES = 0x1C
GATT_GET_ATTRIBUTE_VALUE = 0x1D
GATT_GET_HANDLE_FROM_UUID = 0x22


class BtpCommandError(RuntimeError):
    """Raised for a non-success BTP command response."""


def require_success(response: BtpFrame, *, operation: str) -> bytes:
    """Validate a successful command response.

    BTP success responses for this command family have an empty payload; the
    first byte is an error status only on a protocol error response.
    """
    if response.opcode == 0:
        if response.payload == b"\x00":
            return b""
        status = response.payload[0] if response.payload else 0xFF
        raise BtpCommandError(f"{operation} failed with BTP status 0x{status:02x}")
    return response.payload


class CoreCommands:
    def __init__(self, session: BtpSession) -> None:
        self._session = session

    def read_supported_services(self) -> int:
        response = self._session.request(
            service=CORE,
            opcode=CORE_READ_SUPPORTED_SERVICES,
            index=INDEX_NONE,
        )
        if response.opcode == 0:
            raise BtpCommandError(
                f"Core supported-services failed with status 0x{response.payload.hex()}"
            )
        payload = response.payload
        if len(payload) == 5 and payload[-1] == 0:
            payload = payload[:4]
        elif len(payload) == 5 and payload[0] == 0:
            payload = payload[1:]
        if len(payload) != 4:
            raise BtpCommandError(
                "Core supported-services response must be four bytes: "
                f"service=0x{response.service:02x} opcode=0x{response.opcode:02x} "
                f"index=0x{response.index:02x} payload={response.payload.hex()}"
            )
        return int.from_bytes(payload, "little")

    def read_supported_commands(self, service: int) -> int:
        response = self._session.request(
            service=service,
            opcode=CORE_READ_SUPPORTED_COMMANDS,
            index=INDEX_NONE,
        )
        if response.opcode == 0:
            raise BtpCommandError(
                f"Core supported-commands failed with status 0x{response.payload.hex()}"
            )
        payload = response.payload
        if len(payload) > 1 and payload[0] == 0:
            payload = payload[1:]
        if not payload:
            raise BtpCommandError("Core supported-commands response is empty")
        return int.from_bytes(payload, "little")

    def register_service(self, service: int) -> None:
        response = self._session.request(
            service=CORE,
            opcode=CORE_REGISTER_SERVICE,
            index=INDEX_NONE,
            payload=bytes((service,)),
        )
        require_success(response, operation="Core register service")

    def unregister_service(self, service: int) -> None:
        response = self._session.request(
            service=CORE,
            opcode=CORE_UNREGISTER_SERVICE,
            index=INDEX_NONE,
            payload=bytes((service,)),
        )
        require_success(response, operation="Core unregister service")
