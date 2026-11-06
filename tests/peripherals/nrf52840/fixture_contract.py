"""Immutable contract declarations for installed nRF52840 fixture images.

A contract is test input, not proof that a DK currently contains an image.  The
installed-image proof is a separate attestation returned by the fixture control
plane and checked by :mod:`fixture_attestation` before an RF gate starts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .profile import ProfilePlan

SCHEMA_VERSION = 1
MULTIFUNCTION_CAPABILITIES = (
    "normal-rf",
    "gatt-reset",
    "notification",
    "indication",
    "passive-disconnect",
    "att-read-fault-v2",
    "att-write-fault-v2",
    "att-ccc-fault-v2",
    "att-precommit-defer-v2",
    "att-fault-arm-timeout-10000",
    "att-fault-hold-timeout-20000",
)
NOTIFICATION_OBSERVATION_CAPABILITY = "notification-tx-observation-v1"
MULTIFUNCTION_CAPABILITY_VARIANTS = (
    MULTIFUNCTION_CAPABILITIES,
    (*MULTIFUNCTION_CAPABILITIES, NOTIFICATION_OBSERVATION_CAPABILITY),
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


class FixtureContractError(ValueError):
    """Raised when a fixture contract is malformed or internally inconsistent."""


@dataclass(frozen=True)
class FixtureContract:
    """Exact fixture identity and capabilities required by one RF gate."""

    fixture_role: str
    image_id: str
    image_version: str
    image_sha256: str
    board: str
    profile_id: str
    service_uuid: str
    local_name: str
    capabilities: frozenset[str]


def _object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FixtureContractError(f"cannot read {label}: {path}") from error
    if not isinstance(value, dict):
        raise FixtureContractError(f"{label} must be a JSON object: {path}")
    return value


def _required_text(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise FixtureContractError(f"contract {name} must be a non-empty string")
    return value


def _canonical_uuid(value: str, *, field: str) -> str:
    if _UUID.fullmatch(value.casefold()) is None:
        raise FixtureContractError(f"contract {field} must be a canonical UUID")
    return value.casefold()


def _sha256(value: str, *, field: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise FixtureContractError(f"contract {field} must be lowercase SHA-256")
    return value


def load_contract(path: Path) -> FixtureContract:
    """Load one strict contract; unknown keys are rejected to prevent drift."""
    if not path.exists():
        raise FixtureContractError(
            f"missing fixture contract: {path}; build/export the v2 image first; "
            "a declaration is not an installed-image contract (no v1 fallback)"
        )
    document = _object(path, label="fixture contract")
    expected_keys = {
        "schema_version",
        "fixture_role",
        "image_id",
        "image_version",
        "image_sha256",
        "board",
        "profile_id",
        "service_uuid",
        "local_name",
        "capabilities",
    }
    if set(document) != expected_keys:
        raise FixtureContractError(
            "fixture contract keys must be exactly " + ", ".join(sorted(expected_keys))
        )
    if document.get("schema_version") != SCHEMA_VERSION:
        raise FixtureContractError(
            f"fixture contract schema_version must be {SCHEMA_VERSION}"
        )
    capabilities = document.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or any(
            not isinstance(value, str) or not value.strip() for value in capabilities
        )
    ):
        raise FixtureContractError("contract capabilities must be non-empty strings")
    if len(set(capabilities)) != len(capabilities):
        raise FixtureContractError("contract capabilities must not contain duplicates")
    contract = FixtureContract(
        fixture_role=_required_text(document, "fixture_role"),
        image_id=_required_text(document, "image_id"),
        image_version=_required_text(document, "image_version"),
        image_sha256=_sha256(
            _required_text(document, "image_sha256"), field="image_sha256"
        ),
        board=_required_text(document, "board"),
        profile_id=_required_text(document, "profile_id"),
        service_uuid=_canonical_uuid(
            _required_text(document, "service_uuid"), field="service_uuid"
        ),
        local_name=_required_text(document, "local_name"),
        capabilities=frozenset(capabilities),
    )
    if (
        contract.fixture_role == "normal-rf"
        and "normal-rf" not in contract.capabilities
    ):
        raise FixtureContractError("normal-rf contract must declare normal-rf")
    if (
        contract.fixture_role == "multifunction-rf"
        and tuple(capabilities) not in MULTIFUNCTION_CAPABILITY_VARIANTS
    ):
        raise FixtureContractError(
            "multifunction-rf contract must declare exactly the eleven ordered v2 firmware "
            "capabilities, optionally followed only by "
            + NOTIFICATION_OBSERVATION_CAPABILITY
        )
    if contract.fixture_role == "multifunction-rf" and (
        contract.image_version != "2"
        or contract.image_id
        not in {"nrf-multifunction-rf-v2-a", "nrf-multifunction-rf-v2-b"}
        or contract.profile_id
        != "blehub-nrf52840-multifunction-v2-" + contract.image_id[-1]
    ):
        raise FixtureContractError(
            "multifunction-rf requires a v2 image/profile identity; v1 is historical only"
        )
    return contract


def validate_profile_contract(contract: FixtureContract, profile: ProfilePlan) -> None:
    """Ensure the advertised test profile is the one named by the image contract."""
    mismatches = {
        "profile_id": (contract.profile_id, profile.profile_id),
        "service_uuid": (contract.service_uuid, profile.service_uuid.casefold()),
        "local_name": (contract.local_name, profile.local_name),
    }
    for field, (expected, actual) in mismatches.items():
        if expected != actual:
            raise FixtureContractError(
                f"fixture contract/profile mismatch for {field}: "
                f"expected {expected!r}, got {actual!r}"
            )
