from __future__ import annotations

import json
from pathlib import Path
from typing import NotRequired, TypedDict, cast

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCENARIO_ROOT = PROJECT_ROOT / "tests" / "conformance" / "scenarios"
PROFILE_PATHS = (
    SCENARIO_ROOT / "android-basic-gatt-peripheral.json",
    SCENARIO_ROOT / "qemu-basic-gatt-peripheral.json",
)


class CharacteristicDefinition(TypedDict):
    role: str
    uuid: str
    properties: list[str]
    initial_value_hex: NotRequired[str]


class ServiceDefinition(TypedDict):
    uuid: str
    primary: bool
    advertise: bool
    characteristics: list[CharacteristicDefinition]


class AdvertisingDefinition(TypedDict):
    local_name: str
    require_local_name: bool
    service_uuids: list[str]


class ProviderProfile(TypedDict):
    schema_version: int
    profile_id: str
    advertising: AdvertisingDefinition
    services: list[ServiceDefinition]


def load_profile(path: Path) -> ProviderProfile:
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise TypeError(f"{path.name}: profile root must be an object")
    document = cast(ProviderProfile, cast(object, raw))
    if document.get("schema_version") != 1:
        raise ValueError(f"{path.name}: schema_version must be 1")
    services = document["services"]
    advertising = document["advertising"]
    if len(services) != 1:
        raise ValueError(f"{path.name}: exactly one root service is required")
    if advertising.get("service_uuids") != [services[0].get("uuid")]:
        raise ValueError(
            f"{path.name}: advertised service must be the single root service"
        )
    return document


def semantic_signature(profile: ProviderProfile) -> dict[str, object]:
    service = profile["services"][0]
    characteristics: list[dict[str, object]] = [
        {
            "role": characteristic["role"],
            "uuid": characteristic["uuid"].casefold(),
            "properties": sorted(characteristic["properties"]),
            "initial_value_hex": characteristic.get("initial_value_hex"),
        }
        for characteristic in service["characteristics"]
    ]
    return {
        "schema_version": profile["schema_version"],
        "require_local_name": profile["advertising"]["require_local_name"],
        "primary": service["primary"],
        "advertise": service["advertise"],
        "characteristics": characteristics,
    }


def require_distinct(values: list[str], label: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"provider profiles must use distinct {label} values")


def main() -> int:
    profiles = [load_profile(path) for path in PROFILE_PATHS]
    require_distinct(
        [profile["profile_id"] for profile in profiles],
        "profile_id",
    )
    require_distinct(
        [profile["advertising"]["local_name"] for profile in profiles],
        "local name",
    )
    service_uuids = [profile["services"][0]["uuid"].casefold() for profile in profiles]
    require_distinct(service_uuids, "advertised/root service UUID")

    expected = semantic_signature(profiles[0])
    for path, profile in zip(PROFILE_PATHS[1:], profiles[1:], strict=True):
        if semantic_signature(profile) != expected:
            message = (
                f"{path.name}: provider-independent GATT semantics differ from "
                + PROFILE_PATHS[0].name
            )
            raise ValueError(message)

    for path, profile, service_uuid in zip(
        PROFILE_PATHS, profiles, service_uuids, strict=True
    ):
        message = (
            f"profile={profile['profile_id']} file={path.name} "
            + f"service_uuid={service_uuid} status=valid"
        )
        print(message)
    print("provider_profile_isolation=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
