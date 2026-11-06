"""Explicit ATT hold scenario loader, isolated from the normal nRF provider."""

from __future__ import annotations

from pathlib import Path

from tests.peripherals.nrf52840.fixture_contract import FixtureContract, load_contract
from tests.peripherals.nrf52840.profile import ProfileError, ProfilePlan

from .historical_profile import PERMISSION_READ_AUTHORIZATION
from .historical_profile import load_profile as load_nrf_profile

CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "tests/conformance/scenarios/fixture-contracts/diagnostic-pending-read-v1.json"
)
TARGET_ROLE = "read-write"
TARGET_UUID = "fff10000-0000-4000-8000-000000000000"
TARGET_PERMISSIONS = 0x43


def load_diagnostic_contract() -> FixtureContract:
    """Load the tracked identity contract shared by image and profile checks."""
    return load_contract(CONTRACT_PATH)


def load_diagnostic_profile(path: Path) -> ProfilePlan:
    """Require the exact one-target authorization layout, not a mutated normal profile."""
    contract = load_diagnostic_contract()
    profile = load_nrf_profile(path, allow_read_authorization=True)
    if (profile.profile_id, profile.service_uuid, profile.local_name) != (
        contract.profile_id,
        contract.service_uuid,
        contract.local_name,
    ):
        raise ProfileError("unexpected ATT hold scenario identity")
    targets = [c for c in profile.characteristics if c.role == TARGET_ROLE]
    if (
        len(targets) != 1
        or targets[0].uuid != TARGET_UUID
        or targets[0].permissions != TARGET_PERMISSIONS
        or targets[0].initial_value != b"\x00"
        or sum(
            bool(c.permissions & PERMISSION_READ_AUTHORIZATION)
            for c in profile.characteristics
        )
        != 1
    ):
        raise ProfileError("ATT hold requires exactly one authorized read-write target")
    return profile
