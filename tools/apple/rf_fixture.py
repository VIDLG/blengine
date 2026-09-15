#!/usr/bin/env python3
"""External Apple HIL fixture contract; no provider lifecycle or hardware control."""

from __future__ import annotations

import argparse
import json
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXTERNAL_MACOS_SCENARIOS = (
    "scan",
    "connection",
    "gatt",
    "io",
    "notification",
    "indication",
    "rssi",
    "passive-disconnect",
)


def _hex(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,512}", value) is None
    ):
        raise ValueError("IO values must be 1–512 bytes of unseparated hexadecimal")
    return value.lower()


@dataclass(frozen=True)
class RfFixtureConfig:
    profileId: str
    serviceUuid: str
    hasIndicationOnly: bool
    initialValueHex: str
    notificationValueHex: str
    writeWithResponseHex: str
    writeWithoutResponseHex: str

    def evidence(self) -> list[str]:
        return [
            f"apple_rf_profile_id={self.profileId}",
            f"apple_rf_service_uuid={self.serviceUuid}",
            f"apple_rf_indication_only_required={str(self.hasIndicationOnly).lower()}",
            f"apple_rf_initial_value={self.initialValueHex}",
            f"apple_rf_notification_value={self.notificationValueHex}",
            f"apple_rf_write_with_response_value={self.writeWithResponseHex}",
            f"apple_rf_write_without_response_value={self.writeWithoutResponseHex}",
        ]

    def app_arguments(self) -> list[str]:
        return ["--rf-fixture-config", json.dumps(asdict(self), separators=(",", ":"))]


def load_profile(
    path: Path,
    write_with_response: str = "102030",
    write_without_response: str = "abcd",
) -> RfFixtureConfig:
    """Accept the basic FFF1–4 contract, optionally including indication-only FFF5.

    This is deliberately not an arbitrary GATT test generator. Reject unsupported
    profiles before building/launching, rather than silently weakening assertions.
    """
    try:
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["schema_version"] != 1:
            raise ValueError("expected schema_version=1")
        profile_id = document["profile_id"]
        if (
            not isinstance(profile_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", profile_id) is None
        ):
            raise ValueError(
                "profile_id must be 1–128 letters, digits, underscores or hyphens"
            )
        advertised = document["advertising"]["service_uuids"]
        if not isinstance(advertised, list) or len(advertised) != 1:
            raise ValueError("expected exactly one advertised service UUID")
        service_uuid = str(uuid.UUID(advertised[0]))
        services = [
            s for s in document["services"] if str(uuid.UUID(s["uuid"])) == service_uuid
        ]
        if len(services) != 1 or services[0]["primary"] is not True:
            raise ValueError("expected exactly one matching primary service")
        expected = {
            "read-write": (1, {"read", "write", "write_without_response"}),
            "updates": (2, {"read", "notify", "indicate"}),
            "read-only": (3, {"read"}),
            "write-only": (4, {"write", "write_without_response"}),
            "indications": (5, {"read", "indicate"}),
        }
        characteristics = services[0]["characteristics"]
        by_role = {c["role"]: c for c in characteristics}
        if len(by_role) != len(characteristics) or set(by_role) not in (
            set(expected) - {"indications"},
            set(expected),
        ):
            raise ValueError(
                "expected FFF1–4 roles, optionally indications FFF5; no duplicates or extras"
            )
        for role, characteristic in by_role.items():
            number, properties = expected[role]
            if (
                str(uuid.UUID(characteristic["uuid"]))
                != f"fff{number}0000-0000-4000-8000-000000000000"
            ):
                raise ValueError(f"unsupported characteristic UUID for {role}")
            actual = characteristic["properties"]
            if (
                not isinstance(actual, list)
                or len(actual) != len(properties)
                or set(actual) != properties
            ):
                raise ValueError(f"property mismatch for {role}")
        initial = _hex(by_role["read-write"]["initial_value_hex"])
        notification = _hex(document.get("notification_value_hex", "deadbeef"))
        response = _hex(write_with_response)
        no_response = _hex(write_without_response)
        if len({initial, response, no_response}) != 3:
            raise ValueError(
                "initial and both write values must differ to prove readback"
            )
        return RfFixtureConfig(
            profile_id,
            service_uuid,
            "indications" in by_role,
            initial,
            notification,
            response,
            no_response,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"invalid Apple RF profile {path}: {error}") from error


def add_profile_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        type=Path,
        help="external basic-GATT profile JSON; never controls its provider",
    )
    parser.add_argument(
        "--write-with-response",
        default="102030",
        help="deterministic write/readback hex",
    )
    parser.add_argument(
        "--write-without-response",
        default="abcd",
        help="deterministic no-response write/readback hex",
    )


def fixture_from_arguments(args: argparse.Namespace) -> RfFixtureConfig:
    if args.profile is None:
        raise ValueError("external RF requires --profile")
    return load_profile(
        args.profile, args.write_with_response, args.write_without_response
    )
