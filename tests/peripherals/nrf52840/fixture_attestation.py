"""Fail-closed comparison of a fixture's installed-image attestation.

The attestation must come from the physical fixture control plane.  A local
build manifest or a host-side record of a previous flash is deliberately not an
acceptable substitute.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fixture_contract import (
    FixtureContract,
    FixtureContractError,
    load_contract,
    validate_profile_contract,
)
from .profile import load_profile

SCHEMA_VERSION = 1


class FixtureAttestationError(RuntimeError):
    """Raised when installed fixture identity cannot satisfy a gate contract."""


@dataclass(frozen=True)
class InstalledImageAttestation:
    """Identity reported by the running fixture, not inferred by the host."""

    image_id: str
    image_version: str
    image_sha256: str
    board: str
    profile_id: str
    service_uuid: str
    local_name: str
    capabilities: frozenset[str]


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FixtureAttestationError(
            f"cannot read fixture attestation: {path}"
        ) from error
    if not isinstance(value, dict):
        raise FixtureAttestationError("fixture attestation must be a JSON object")
    return value


def load_attestation(path: Path) -> InstalledImageAttestation:
    """Parse the strict wire-shaped attestation captured from the fixture."""
    return parse_attestation(_object(path))


def parse_attestation(document: dict[str, Any]) -> InstalledImageAttestation:
    """Validate the same wire schema in a file or an attempt-bound live receipt."""
    expected_keys = {
        "schema_version",
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
        raise FixtureAttestationError(
            "fixture attestation keys must be exactly "
            + ", ".join(sorted(expected_keys))
        )
    if document.get("schema_version") != SCHEMA_VERSION:
        raise FixtureAttestationError(
            f"fixture attestation schema_version must be {SCHEMA_VERSION}"
        )
    fields = (
        "image_id",
        "image_version",
        "image_sha256",
        "board",
        "profile_id",
        "service_uuid",
        "local_name",
    )
    values: dict[str, str] = {}
    for field in fields:
        value = document.get(field)
        if not isinstance(value, str) or not value.strip():
            raise FixtureAttestationError(f"attestation {field} must be non-empty text")
        values[field] = value
    capabilities = document.get("capabilities")
    if not isinstance(capabilities, list) or any(
        not isinstance(value, str) or not value.strip() for value in capabilities
    ):
        raise FixtureAttestationError("attestation capabilities must be strings")
    if len(set(capabilities)) != len(capabilities):
        raise FixtureAttestationError(
            "attestation capabilities must not contain duplicates"
        )
    return InstalledImageAttestation(
        **values,
        capabilities=frozenset(capabilities),
    )


def assert_attestation(
    contract: FixtureContract, attestation: InstalledImageAttestation
) -> None:
    """Require exact identity and at least every capability named by the contract."""
    for field in (
        "image_id",
        "image_version",
        "image_sha256",
        "board",
        "profile_id",
        "service_uuid",
        "local_name",
    ):
        expected = getattr(contract, field)
        actual = getattr(attestation, field)
        if expected != actual:
            raise FixtureAttestationError(
                f"installed fixture mismatch for {field}: "
                f"expected {expected!r}, got {actual!r}"
            )
    missing = contract.capabilities - attestation.capabilities
    if missing:
        raise FixtureAttestationError(
            "installed fixture is missing required capabilities: "
            + ", ".join(sorted(missing))
        )


def assert_fixture_contract(
    contract_path: Path,
    attestation_path: Path,
    profile_path: Path | None = None,
    *,
    service_uuid: str | None = None,
) -> FixtureContract:
    """Run the complete host-side preflight before an RF process is started."""
    try:
        contract = load_contract(contract_path)
        if profile_path is not None:
            validate_profile_contract(contract, load_profile(profile_path))
    except (FixtureContractError, ValueError) as error:
        raise FixtureAttestationError(str(error)) from error
    if service_uuid is not None and contract.service_uuid != service_uuid.casefold():
        raise FixtureAttestationError(
            "runner service UUID does not match fixture contract: "
            f"expected {contract.service_uuid}, got {service_uuid}"
        )
    assert_attestation(contract, load_attestation(attestation_path))
    return contract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("assert")
    check.add_argument("--contract", type=Path, required=True)
    check.add_argument("--attestation", type=Path, required=True)
    check.add_argument("--profile", type=Path)
    args = parser.parse_args(argv)
    try:
        assert_fixture_contract(args.contract, args.attestation, args.profile)
    except (FixtureAttestationError, OSError, ValueError) as error:
        print(f"fixture_preflight=failed:{error}", file=sys.stderr)
        return 2
    print("fixture_preflight=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
