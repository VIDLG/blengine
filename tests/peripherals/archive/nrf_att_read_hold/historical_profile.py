"""Archived AUTH_READ profile parser; never imported by maintained live owners."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840.profile import (
    PERMISSION_READ,
    PERMISSION_WRITE,
    PROPERTY_BITS,
    CharacteristicPlan,
    ProfileError,
    ProfilePlan,
)

PERMISSION_READ_AUTHORIZATION = 1 << 6


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
    require_local_name = advertising.get("require_local_name")
    if type(require_local_name) is not bool:
        raise ProfileError("advertising.require_local_name must be a bool")
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
        profile_id,
        local_name,
        service_uuid,
        notification_value,
        tuple(characteristics),
        require_local_name,
    )
