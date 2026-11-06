"""RN host fixture checks delegated to the authoritative nRF provider/session.

This module owns no lease, session, attestation or fixture process. It validates
host inputs and the startup identity without taking over provider ownership.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.peripherals.nrf52840 import provider


class RfFixtureError(RuntimeError):
    """Raised when RN host fixture inputs or live ownership are invalid."""


def preflight_fixture(
    *,
    fixture_contract: Path | None,
    fixture_profile: Path | None,
    service_uuid: str | None = None,
) -> str:
    if fixture_contract is None or fixture_profile is None:
        raise RfFixtureError(
            "RF gate requires explicit --fixture-contract and --fixture-profile"
        )
    from tests.peripherals.nrf52840.fixture_contract import load_contract
    from tests.peripherals.nrf52840.fixture_session import validate_fixture_inputs

    try:
        contract = load_contract(fixture_contract)
        if service_uuid is None:
            service_uuid = contract.service_uuid
        if contract.fixture_role != "multifunction-rf":
            raise ValueError(
                "RF gate requires multifunction-rf, not legacy active probing"
            )
        validate_fixture_inputs(
            fixture_contract,
            fixture_profile,
            service_uuid=service_uuid,
        )
    except (OSError, ValueError, RuntimeError) as error:
        raise RfFixtureError(f"fixture preflight failed: {error}") from error
    return service_uuid


def fixture_identity_from_stdout(stdout: str) -> tuple[int, int]:
    """Accept only one exact positive PID and PGID line from successful startup."""
    identity: list[int] = []
    for key in ("nrf52840_fixture_pid", "nrf52840_fixture_pgid"):
        lines = [line for line in stdout.splitlines() if line.startswith(f"{key}=")]
        if len(lines) != 1 or re.fullmatch(rf"{key}=[0-9]+", lines[0]) is None:
            raise RfFixtureError(
                f"direct-start must emit exactly one {key}=<positive int> line"
            )
        value = int(lines[0].split("=", 1)[1])
        if value <= 0:
            raise RfFixtureError(f"direct-start emitted nonpositive {key}")
        identity.append(value)
    return identity[0], identity[1]


def guard_fixture(identity: tuple[int, int]) -> None:
    if provider._process_identity() != identity:
        raise RfFixtureError("fixture PID/PGID identity changed")
    if provider._fixture_state(*identity) != provider._PROCESS_RUNNING:
        raise RfFixtureError("fixture is not RUNNING with the same PID/PGID")
