from __future__ import annotations

import json
from pathlib import Path
from typing import NotRequired, TypedDict, cast

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCENARIO_ROOT = PROJECT_ROOT / "tests" / "conformance" / "scenarios"
ARCHIVED_PROFILE_PATHS = (
    SCENARIO_ROOT / "archive" / "android" / "android-peripheral.json",
    SCENARIO_ROOT / "archive" / "bless" / "qemu-basic-gatt-peripheral.json",
)
NRF_PROFILE_PATH = SCENARIO_ROOT / "blehub-nrf-indication-v2.json"


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
    archived_profiles = [load_profile(path) for path in ARCHIVED_PROFILE_PATHS]
    nrf_profile = load_profile(NRF_PROFILE_PATH)
    all_profiles = [*archived_profiles, nrf_profile]
    require_distinct(
        [profile["profile_id"] for profile in all_profiles],
        "profile_id",
    )
    require_distinct(
        [profile["advertising"]["local_name"] for profile in all_profiles],
        "local name",
    )
    service_uuids = [
        profile["services"][0]["uuid"].casefold() for profile in archived_profiles
    ]
    all_service_uuids = [
        profile["services"][0]["uuid"].casefold() for profile in all_profiles
    ]
    require_distinct(all_service_uuids, "advertised/root service UUID")

    expected = semantic_signature(archived_profiles[0])
    for path, profile in zip(
        ARCHIVED_PROFILE_PATHS[1:], archived_profiles[1:], strict=True
    ):
        if semantic_signature(profile) != expected:
            message = (
                f"{path.name}: provider-independent GATT semantics differ from "
                + ARCHIVED_PROFILE_PATHS[0].name
            )
            raise ValueError(message)

    for path, profile, service_uuid in zip(
        ARCHIVED_PROFILE_PATHS, archived_profiles, service_uuids, strict=True
    ):
        message = (
            f"profile={profile['profile_id']} file={path.name} "
            + f"service_uuid={service_uuid} status=valid"
        )
        print(message)
    android_roles = {
        characteristic["role"]
        for characteristic in archived_profiles[0]["services"][0]["characteristics"]
    }
    if "updates" not in android_roles or "indication-updates" not in android_roles:
        raise ValueError(
            "Android profile must include both updates and indication-updates roles"
        )
    indication = next(
        characteristic
        for characteristic in archived_profiles[0]["services"][0]["characteristics"]
        if characteristic["role"] == "indication-updates"
    )
    if sorted(indication["properties"]) != ["indicate", "read"]:
        raise ValueError("indication-updates characteristic must be read+indicate only")
    print(
        f"profile={archived_profiles[0]['profile_id']} file={ARCHIVED_PROFILE_PATHS[0].name} "
        + "modes=notification+strict-indication status=valid"
    )
    nrf_roles = {
        characteristic["role"]
        for characteristic in nrf_profile["services"][0]["characteristics"]
    }
    if nrf_roles != {"read-write", "updates", "indications", "read-only", "write-only"}:
        raise ValueError(
            "nRF52840 profile must contain exactly the five v2 GATT roles"
        )
    print(
        f"profile={nrf_profile['profile_id']} file={NRF_PROFILE_PATH.name} status=valid"
    )
    print("provider_profile_isolation=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
