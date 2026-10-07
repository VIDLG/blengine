"""Read-only J-Link VCOM/BTP diagnostic; never register, reset, or flash.

A passing response proves only that the connected device answers the private
protocol. It does not attest installed flash bytes or prove iPhone RF.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import cast

from tests.peripherals.nrf52840.btp_commands import GAP, GATT, CoreCommands
from tests.peripherals.nrf52840.btp_session import BtpSession
from tests.peripherals.nrf52840.btp_transport import FramedTransport
from tests.peripherals.nrf52840.diagnostic import device
from tests.peripherals.nrf52840.diagnostic.read_hold import (
    HoldState,
    ReadHold,
    Session,
    Snapshot,
)
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
    selected, probe = device.detect_btp_port(probe_serial, port)
    with device.open_btp_port(selected) as connection:
        session = BtpSession(FramedTransport(SerialByteTransport(connection)))
        core = CoreCommands(session)
        required_services = 1 | (1 << GAP) | (1 << GATT)
        if core.read_supported_services() & required_services != required_services:
            raise device.DiagnosticError("BTP Core/GAP/GATT services are unavailable")
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
        raise device.DiagnosticError("J-Link identity changed during read-only probe")
    if device.FIXTURE_PID.exists() or device.RN_LOCK.exists():
        raise device.DiagnosticError(
            "nRF fixture ownership changed during read-only probe"
        )
    return Observation(selected, probe_serial, mask, snapshot)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-serial", required=True, help="explicit numeric J-Link USB serial"
    )
    parser.add_argument(
        "--port", help="optional assertion against the uniquely responding VCOM"
    )
    args = parser.parse_args(argv)
    try:
        result = inspect(args.probe_serial, args.port)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"read-only DK diagnostic failed: {error}", file=sys.stderr)
        return 2
    print(
        f"btp_vcom={result.port} probe_serial={result.probe_serial} "
        f"gatt_commands=0x{result.command_mask:x} "
        f"hold={result.snapshot.state.name} identity={result.snapshot.identity} "
        "installed_image_attested=false rf_tested=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
