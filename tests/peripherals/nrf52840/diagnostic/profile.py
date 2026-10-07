"""Explicit ATT hold scenario loader, isolated from the normal nRF provider."""

from __future__ import annotations

from pathlib import Path

from ..profile import PERMISSION_READ_AUTHORIZATION, ProfileError, ProfilePlan
from ..profile import load_profile as load_nrf_profile

PROFILE_ID = "blehub-nrf52840-att-read-hold-v1"
SERVICE_UUID = "fd100000-0000-4000-8000-000000000000"
LOCAL_NAME = "BleHubAttHoldDK"
TARGET_ROLE = "read-write"
TARGET_UUID = "fff10000-0000-4000-8000-000000000000"
TARGET_PERMISSIONS = 0x43


def load_diagnostic_profile(path: Path) -> ProfilePlan:
    """Require the exact one-target authorization layout, not a mutated normal profile."""
    profile = load_nrf_profile(path, allow_read_authorization=True)
    if (profile.profile_id, profile.service_uuid, profile.local_name) != (
        PROFILE_ID,
        SERVICE_UUID,
        LOCAL_NAME,
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
