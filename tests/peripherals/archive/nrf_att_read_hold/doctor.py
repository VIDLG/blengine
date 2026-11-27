"""Read-only J-Link VCOM/BTP diagnostic; never register, reset, or flash.

A passing response proves only that the connected device answers the private
protocol. It does not attest installed flash bytes or prove iPhone RF.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import cast

from tests.peripherals.archive.nrf_att_read_hold import device
from tests.peripherals.archive.nrf_att_read_hold.read_hold import (
    HoldState,
    ReadHold,
    Session,
    Snapshot,
)
from tests.peripherals.nrf52840.btp_commands import GAP, GATT, CoreCommands
from tests.peripherals.nrf52840.btp_session import BtpSession
from tests.peripherals.nrf52840.btp_transport import FramedTransport
from tests.peripherals.nrf52840.resource_lease import owned_lease
from tests.peripherals.nrf52840.serial_transport import SerialByteTransport


@dataclass(frozen=True)
class Observation:
    """Read-only firmware protocol observation, not installed-image attestation."""

    port: str
    probe_serial: str
    command_mask: int
    snapshot: Snapshot


def inspect(probe_serial: str, port: str | None = None) -> Observation:
    """Check exclusive ownership, private capabilities and quiescent hold state."""
    device.validate_probe_serial(probe_serial)
    if device.FIXTURE_PID.exists() or device.RN_LOCK.exists():
        raise device.DiagnosticError(
            "nRF provider or RN fixture owns the DK; refusing read-only probe"
        )
    with owned_lease("jlink", probe_serial, purpose="read-only DK doctor") as owner:
        selected, probe = device.detect_btp_port(probe_serial, port, lease=owner)
        with device.open_btp_port(selected) as connection:
            session = BtpSession(FramedTransport(SerialByteTransport(connection)))
            core = CoreCommands(session)
            required_services = 1 | (1 << GAP) | (1 << GATT)
            if core.read_supported_services() & required_services != required_services:
                raise device.DiagnosticError(
                    "BTP Core/GAP/GATT services are unavailable"
                )
            mask = core.read_supported_commands(GATT)
            if mask & device.READ_HOLD_COMMAND_MASK != device.READ_HOLD_COMMAND_MASK:
                raise device.DiagnosticError(
                    "GATT reset/read-hold command capabilities are missing"
                )
            snapshot = ReadHold(cast(Session, cast(object, session))).status()
            if snapshot.state not in (
                HoldState.IDLE,
                HoldState.RELEASED,
                HoldState.ABORTED,
                HoldState.EXPIRED,
            ):
                raise device.DiagnosticError(
                    f"read-hold firmware is busy: {snapshot.state.name}"
                )
        if device.verify_probe(probe_serial) != probe:
            raise device.DiagnosticError(
                "J-Link identity changed during read-only probe"
            )
        if device.FIXTURE_PID.exists() or device.RN_LOCK.exists():
            raise device.DiagnosticError(
                "nRF fixture ownership changed during read-only probe"
            )
    return Observation(selected, probe_serial, mask, snapshot)


def main(argv: list[str] | None = None) -> int:
    print(
        "legacy read-hold entry is unavailable: migrate to attested fault v2; no v1 wire fallback",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
