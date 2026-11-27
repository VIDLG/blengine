"""Scoped direct-fixture resources with acquisition/release invariants."""

from __future__ import annotations

import struct
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import direct_fixture, fixture_session
from tests.peripherals.nrf52840.btp_commands import CORE, GAP, GATT
from tests.peripherals.nrf52840.btp_protocol import BtpFrame
from tests.peripherals.nrf52840.btp_session import BtpSession
from tests.peripherals.nrf52840.resource_lease import DeviceLease, ResourceLeaseError


def sample_plan(
    *, port: str = "test-port", resource_identity: str = "jlink:123456"
) -> SimpleNamespace:
    """One immutable-profile session sample, with explicit identity-mismatch inputs."""
    return SimpleNamespace(
        profile_path=Path("immutable-profile.json"),
        session_id="12345678-1234-4234-8234-123456789abc",
        port=port,
        resource_identity=resource_identity,
    )


class ServiceWire:
    """Synthetic Tester dispatch: GATT commands exist only after registration."""

    def __init__(
        self, events: list[str], *, warm: bool = False, fault: str = ""
    ) -> None:
        self.events, self.fault = events, fault
        self.registered = {GAP, GATT} if warm else set()
        self.frames: deque[BtpFrame] = deque()
        self.sent: list[BtpFrame] = []
        self.attestation: bytes | None = None

    def send(self, frame: BtpFrame) -> None:
        self.sent.append(frame)
        service, opcode, index = frame.service, frame.opcode, frame.index
        payload = b""
        if service == 255 and opcode == 0x01 and index == 255:
            self.events.append("attest")
            assert self.attestation is not None
            payload = self.attestation
        elif service == CORE and opcode == 2 and index == 0xFF:
            self.events.append("services")
            payload = (6).to_bytes(4, "little")
        elif service == CORE and opcode == 3 and index == 0xFF:
            target = frame.payload[0]
            self.events.append(f"register-{target}")
            if target in self.registered:
                raise AssertionError("duplicate service registration")
            self.registered.add(target)
        elif service in (GAP, GATT) and opcode == 1 and index == 0xFF:
            self.events.append(f"probe-{service}")
            if service not in self.registered:
                opcode, payload = 0, b"\x02"
            else:
                payload = b"\xff\xff\xff\xff"
        elif service == 255 and opcode == 0x10 and index == 255:
            self.events.append("status")
            if self.fault == "unknown":
                opcode, payload = 0, b"\x02"
            else:
                payload = (
                    b"\x02\x00"
                    + bytes(range(1, 17))
                    + struct.pack(
                        "<BBBBBHIIIIB7sIII",
                        7,
                        0,
                        1,
                        1,
                        20,
                        4 if self.fault == "foreign" else 0,
                        10000,
                        20000,
                        2000,
                        5000,
                        0,
                        bytes(7),
                        0,
                        0,
                        0,
                    )
                )
        elif service == GAP and opcode == 3 and index == 0:
            self.events.append("controller")
            payload = bytes(277)
        else:
            raise AssertionError(f"unexpected synthetic command: {frame}")
        self.frames.append(BtpFrame(service, opcode, index, payload))

    def receive(self, timeout: float) -> BtpFrame:
        return self.frames.popleft()

    def close(self) -> None:
        pass


def bind_btp_session(resources: SimpleNamespace, session: BtpSession) -> None:
    """Route mocked requests/close through the real synthetic-wire BTP session."""
    resources.session.request.side_effect = session.request
    resources.session.close.side_effect = lambda: (
        session.close(),
        resources.step("session-close"),
    )


@contextmanager
def startup_mocks(
    resources: SimpleNamespace, handles: dict[str, int]
) -> Iterator[SimpleNamespace]:
    """Connect serve-only startup mocks; leave start and suite dispatch unpatched."""
    with (
        patch.object(
            fixture_session,
            "attest_session",
            side_effect=lambda *a, **kw: resources.step("attest"),
        ) as attest,
        patch.object(direct_fixture, "NrfDoctor") as doctor,
        patch.object(
            direct_fixture.DirectFixture,
            "_start_advertising",
            side_effect=lambda: resources.step("advertise"),
        ),
        patch.object(direct_fixture.signal, "signal"),
        patch.object(Path, "mkdir"),
    ):
        doctor.return_value.run.side_effect = lambda _: resources.step(
            "doctor", SimpleNamespace(role_handles=handles)
        )
        yield SimpleNamespace(attest=attest, doctor=doctor)


@contextmanager
def resources(
    events: list[str], failures: tuple[str, ...] = ()
) -> Iterator[SimpleNamespace]:
    """Mock one fixture lease and reject resource use outside its lifetime."""

    def step(name: str, result: object = None) -> object:
        if name != "acquire":
            if events[0] != "acquire":
                raise AssertionError("resource use before acquisition")
            if "release" in events:
                raise AssertionError("resource use after release")
        events.append(name)
        if name in failures:
            error_type = ResourceLeaseError if name == "acquire" else RuntimeError
            raise error_type(f"failed {name}")
        return result

    owner = Mock(spec=DeviceLease)
    owner.identity = "jlink:123456"

    @contextmanager
    def lease(*args: object, **kwargs: object) -> Iterator[DeviceLease]:
        step("acquire")
        try:
            yield owner
        finally:
            events.append("release")

    serial = Mock()
    serial.close.side_effect = lambda: step("serial-close")
    session = Mock()
    session.request.return_value = BtpFrame(
        255,
        0x10,
        255,
        b"\x02\x00"
        + bytes(range(1, 17))
        + struct.pack(
            "<BBBBBHIIIIB7sIII",
            7,
            0,
            1,
            1,
            20,
            0,
            10000,
            20000,
            2000,
            5000,
            0,
            bytes(7),
            0,
            0,
            0,
        ),
    )
    session.close.side_effect = lambda: step("session-close")
    gap = Mock()
    gap.stop_advertising.side_effect = lambda: step("advertising-stop")
    gatt = Mock()
    gatt.reset_server.side_effect = lambda: step("reset")
    with (
        patch.object(direct_fixture, "port_lease", side_effect=lease) as port_lease,
        patch.object(
            direct_fixture,
            "open_serial",
            side_effect=lambda _: step("open", serial),
        ) as open_serial,
        patch.object(
            direct_fixture,
            "FramedTransport",
            side_effect=lambda _: step("transport"),
        ),
        patch.object(
            direct_fixture,
            "BtpSession",
            side_effect=lambda _: step("session", session),
        ),
        patch.object(
            direct_fixture, "GapCommands", side_effect=lambda _: step("gap", gap)
        ),
        patch.object(
            direct_fixture, "GattCommands", side_effect=lambda _: step("gatt", gatt)
        ),
        patch.object(
            direct_fixture,
            "load_profile",
            side_effect=lambda _: step("profile", Mock()),
        ),
    ):
        yield SimpleNamespace(
            serial=serial,
            session=session,
            owner=owner,
            gap=gap,
            gatt=gatt,
            port_lease=port_lease,
            open_serial=open_serial,
            step=step,
        )
