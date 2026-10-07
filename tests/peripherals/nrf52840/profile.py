"""Scenario-to-GATT provisioning plan for the nRF BTP provider."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .btp_commands import BtpCommandError
from .gatt_commands import GattCommands

PROPERTY_BITS = {
    "read": 1 << 1,
    "write_without_response": 1 << 2,
    "write": 1 << 3,
    "notify": 1 << 4,
    "indicate": 1 << 5,
}
PERMISSION_READ = 1 << 0
PERMISSION_WRITE = 1 << 1
PERMISSION_READ_AUTHORIZATION = 1 << 6
PRIMARY_SERVICE = 0
CCC_UUID = "2902"


class ProfileError(ValueError):
    """Raised when a scenario cannot be represented by the nRF fixture."""


@dataclass(frozen=True)
class CharacteristicPlan:
    role: str
    uuid: str
    properties: int
    permissions: int
    initial_value: bytes
    requires_ccc: bool


@dataclass(frozen=True)
class ProfilePlan:
    profile_id: str
    local_name: str
    service_uuid: str
    notification_value: bytes
    characteristics: tuple[CharacteristicPlan, ...]


def _hex(value: Any, *, field: str) -> bytes:
    if not isinstance(value, str):
        raise ProfileError(f"{field} must be a hex string")
    try:
        return bytes.fromhex(value)
    except ValueError as error:
        raise ProfileError(f"{field} is not valid hexadecimal") from error


def load_profile(path: Path, *, allow_read_authorization: bool = False) -> ProfilePlan:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProfileError(f"cannot read profile {path}: {error}") from error
    if document.get("schema_version") != 1:
        raise ProfileError("profile schema_version must be 1")
    services = document.get("services")
    advertising = document.get("advertising")
    if not isinstance(services, list) or len(services) != 1:
        raise ProfileError("nRF profile must contain exactly one service")
    if not isinstance(advertising, dict):
        raise ProfileError("profile advertising must be an object")
    service = services[0]
    if not isinstance(service, dict) or not isinstance(
        service.get("characteristics"), list
    ):
        raise ProfileError("profile service must define characteristics")
    service_uuid = service.get("uuid")
    local_name = advertising.get("local_name")
    profile_id = document.get("profile_id")
    if not all(
        isinstance(value, str) and value
        for value in (profile_id, local_name, service_uuid)
    ):
        raise ProfileError("profile_id, local_name, and service uuid are required")

    notification_value = _hex(
        document.get("notification_value_hex", "ef"),
        field="notification_value_hex",
    )
    characteristics: list[CharacteristicPlan] = []
    roles: set[str] = set()
    for item in service["characteristics"]:
        if not isinstance(item, dict):
            raise ProfileError("characteristic must be an object")
        role = item.get("role")
        characteristic_uuid = item.get("uuid")
        properties = item.get("properties")
        if not isinstance(role, str) or not role or role in roles:
            raise ProfileError(
                f"characteristic role is missing or duplicated: {role!r}"
            )
        if not isinstance(characteristic_uuid, str) or not isinstance(properties, list):
            raise ProfileError(f"invalid characteristic definition for role {role}")
        try:
            property_bits = sum(PROPERTY_BITS[name] for name in properties)
        except (KeyError, TypeError) as error:
            raise ProfileError(f"unsupported properties for role {role}") from error
        permissions = 0
        if "read" in properties:
            permissions |= PERMISSION_READ
        if "write" in properties or "write_without_response" in properties:
            permissions |= PERMISSION_WRITE
        if "read_authorization" in item:
            if (
                not allow_read_authorization
                or item["read_authorization"] is not True
                or "read" not in properties
            ):
                raise ProfileError(
                    "read authorization requires an opt-in readable diagnostic"
                )
            permissions |= PERMISSION_READ_AUTHORIZATION
        initial_value = _hex(
            item.get("initial_value_hex", ""), field=f"{role}.initial_value_hex"
        )
        characteristics.append(
            CharacteristicPlan(
                role=role,
                uuid=characteristic_uuid,
                properties=property_bits,
                permissions=permissions,
                initial_value=initial_value,
                requires_ccc="notify" in properties or "indicate" in properties,
            )
        )
        roles.add(role)
    return ProfilePlan(
        profile_id, local_name, service_uuid, notification_value, tuple(characteristics)
    )


def _canonical_attribute_uuid(value: bytes) -> str:
    if len(value) == 2:
        return f"0000{value[::-1].hex()}-0000-1000-8000-00805f9b34fb"
    if len(value) == 16:
        return str(uuid.UUID(bytes=value[::-1]))
    raise ProfileError(f"unsupported service declaration UUID length: {len(value)}")


def _target_service_handles(
    commands: GattCommands, attributes: tuple[Any, ...], service_uuid: str
) -> tuple[int, ...]:
    handles: list[int] = []
    for attribute in attributes:
        if attribute.uuid.casefold() not in {"2800", "2801"}:
            continue
        try:
            value = commands.get_attribute_value(attribute.handle).value
        except BtpCommandError as error:
            raise ProfileError(
                f"cannot inspect existing service declaration at handle {attribute.handle}"
            ) from error
        if _canonical_attribute_uuid(value) == service_uuid.casefold():
            handles.append(attribute.handle)
    return tuple(handles)


def provision(commands: GattCommands, profile: ProfilePlan) -> dict[str, int]:
    """Provision or attach one fixed profile and return role-to-handle mappings.

    A resident nRF GATT database survives the host process. Never append a profile
    when its root service already exists with a different or incomplete topology.
    """
    try:
        existing = commands.get_attributes()
    except BtpCommandError:
        existing = ()
    target_services = _target_service_handles(commands, existing, profile.service_uuid)
    if len(target_services) > 1:
        raise ProfileError(
            f"profile root service {profile.service_uuid} appears {len(target_services)} times; "
            """remove stale services or reset the dynamic GATT database before retrying"""
        )
    normalized_existing = {
        attribute.uuid.casefold(): attribute.handle for attribute in existing
    }
    complete_characteristics = all(
        characteristic.uuid.casefold() in normalized_existing
        for characteristic in profile.characteristics
    )
    if target_services and not complete_characteristics:
        raise ProfileError(
            f"profile root service {profile.service_uuid} has an incomplete characteristic topology; "
            """refusing to append a duplicate service"""
        )
    if complete_characteristics:
        role_handles = {
            characteristic.role: normalized_existing[characteristic.uuid.casefold()]
            for characteristic in profile.characteristics
        }
        for characteristic in profile.characteristics:
            commands.set_value(
                role_handles[characteristic.role], characteristic.initial_value
            )
        return role_handles

    commands.add_service(PRIMARY_SERVICE, profile.service_uuid)
    for characteristic in profile.characteristics:
        commands.add_characteristic(
            0,
            characteristic.properties,
            characteristic.permissions,
            characteristic.uuid,
        )
        try:
            commands.set_value(0, characteristic.initial_value)
        except BtpCommandError as error:
            raise ProfileError(
                f"failed to initialize role {characteristic.role} "
                f"({characteristic.uuid}): {error}"
            ) from error
        if characteristic.requires_ccc:
            commands.add_descriptor(0, PERMISSION_READ | PERMISSION_WRITE, CCC_UUID)
    commands.start_server()
    attributes = commands.get_attributes()
    normalized = {
        attribute.uuid.casefold(): attribute.handle for attribute in attributes
    }
    missing = [
        characteristic.uuid
        for characteristic in profile.characteristics
        if characteristic.uuid.casefold() not in normalized
    ]
    if missing:
        raise ProfileError(
            f"profile characteristics missing from GATT database: {missing}"
        )
    return {
        characteristic.role: normalized[characteristic.uuid.casefold()]
        for characteristic in profile.characteristics
    }
