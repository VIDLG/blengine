"""Hardware-independent orchestration for the nRF BTP readiness doctor."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .btp_commands import GAP, GATT, BtpCommandError, CoreCommands
from .btp_session import BtpSession
from .gap_commands import GapCommands
from .gatt_commands import GattCommands
from .profile import ProfilePlan, load_profile, provision


@dataclass(frozen=True)
class DoctorResult:
    profile_id: str
    supported_services: int
    supported_commands: dict[int, int]
    controller_info: bytes
    role_handles: dict[str, int]


class NrfDoctor:
    """Run the minimum Core/GAP/GATT checks before declaring profile readiness."""

    def __init__(self, session: BtpSession) -> None:
        self._core = CoreCommands(session)
        self._gap = GapCommands(session)
        self._gatt = GattCommands(session)

    def ensure_services(self) -> tuple[int, dict[int, int]]:
        """Register missing BTP services without changing the GATT profile."""
        services = self._core.read_supported_services()
        required_services = (1 << GAP) | (1 << GATT)
        if services & required_services != required_services:
            raise BtpCommandError(
                f"nRF firmware lacks required GAP/GATT services: 0x{services:08x}"
            )
        supported_commands: dict[int, int] = {}
        for service in (GAP, GATT):
            try:
                supported_commands[service] = self._core.read_supported_commands(
                    service
                )
            except BtpCommandError:
                self._core.register_service(service)
                supported_commands[service] = self._core.read_supported_commands(
                    service
                )
        return services, supported_commands

    def run(self, profile: ProfilePlan) -> DoctorResult:
        services, supported_commands = self.ensure_services()
        controller_info = self._gap.read_controller_info()
        role_handles = provision(self._gatt, profile)
        return DoctorResult(
            profile_id=profile.profile_id,
            supported_services=services,
            supported_commands=supported_commands,
            controller_info=controller_info,
            role_handles=role_handles,
        )


def run_profile_doctor(session: BtpSession, profile_path: Path) -> DoctorResult:
    return NrfDoctor(session).run(load_profile(profile_path))
