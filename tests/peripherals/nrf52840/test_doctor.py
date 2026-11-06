from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840.btp_commands import CORE, GAP, GATT, BtpCommandError
from tests.peripherals.nrf52840.btp_protocol import BtpFrame
from tests.peripherals.nrf52840.doctor import NrfDoctor
from tests.peripherals.nrf52840.profile import load_profile

PROFILE = (
    Path(__file__).parents[2]
    / "conformance"
    / "scenarios"
    / "blehub-nrf-indication-v2.json"
)


class FakeSession:
    def __init__(
        self,
        services: int = (1 << GAP) | (1 << GATT),
        *,
        registered: tuple[int, ...] = (),
    ) -> None:
        self.services = services
        self.registered = set(registered)
        self.requests: list[dict[str, object]] = []

    def request(self, **kwargs: object) -> BtpFrame:
        self.requests.append(kwargs)
        service = kwargs["service"]
        opcode = kwargs["opcode"]
        if service == CORE and opcode == 2:
            return BtpFrame(CORE, opcode, 0xFF, self.services.to_bytes(4, "little"))
        if service == CORE and opcode == 3:
            target = kwargs["payload"][0]
            if target in self.registered:
                raise AssertionError("service registered twice")
            self.registered.add(target)
            return BtpFrame(CORE, opcode, 0xFF)
        if service in (GAP, GATT) and opcode == 1:
            if service not in self.registered:
                return BtpFrame(service, 0, 0xFF, b"\x02")
            return BtpFrame(service, opcode, 0xFF, b"\xff\xff\xff\xff")
        if service == GAP and opcode == 3:
            return BtpFrame(GAP, opcode, 0, b"\x00" * 277)
        if service == GATT and opcode == 0x1C:
            payload = bytearray([5])
            for handle, uuid in (
                (0x25, "fff10000-0000-4000-8000-000000000000"),
                (0x28, "fff20000-0000-4000-8000-000000000000"),
                (0x2B, "fff30000-0000-4000-8000-000000000000"),
                (0x2E, "fff40000-0000-4000-8000-000000000000"),
                (0x31, "fff50000-0000-4000-8000-000000000000"),
            ):
                raw = bytes.fromhex(uuid.replace("-", ""))[::-1]
                payload.extend(handle.to_bytes(2, "little"))
                payload.extend((3, 16))
                payload.extend(raw)
            return BtpFrame(GATT, opcode, 0, bytes(payload))
        return BtpFrame(service, opcode, kwargs["index"])


class DoctorTests(unittest.TestCase):
    def test_doctor_registers_services_and_provisions_profile(self) -> None:
        session = FakeSession()
        result = NrfDoctor(session).run(load_profile(PROFILE))
        self.assertEqual(result.profile_id, "blehub-nrf52840-indication-v2")
        self.assertEqual(
            set(result.role_handles),
            {"read-write", "updates", "indications", "read-only", "write-only"},
        )
        self.assertEqual(result.supported_services, 6)
        self.assertEqual(session.requests[0]["opcode"], 2)
        self.assertEqual(
            [
                (request["service"], request["opcode"])
                for request in session.requests
                if request["opcode"] == 1
            ],
            [(GAP, 1), (GAP, 1), (GATT, 1), (GATT, 1)],
        )

    def test_ensure_services_cold_then_warm_never_provisions(self) -> None:
        session = FakeSession()
        doctor = NrfDoctor(session)
        with patch("tests.peripherals.nrf52840.doctor.provision") as provision:
            cold = doctor.ensure_services()
            warm = doctor.ensure_services()
        self.assertEqual(cold, (6, {GAP: 0xFFFFFFFF, GATT: 0xFFFFFFFF}))
        self.assertEqual(warm, cold)
        self.assertEqual(
            [
                (r["service"], r["opcode"], r["index"], r.get("payload", b""))
                for r in session.requests
            ],
            [
                (CORE, 2, 0xFF, b""),
                (GAP, 1, 0xFF, b""),
                (CORE, 3, 0xFF, bytes((GAP,))),
                (GAP, 1, 0xFF, b""),
                (GATT, 1, 0xFF, b""),
                (CORE, 3, 0xFF, bytes((GATT,))),
                (GATT, 1, 0xFF, b""),
                (CORE, 2, 0xFF, b""),
                (GAP, 1, 0xFF, b""),
                (GATT, 1, 0xFF, b""),
            ],
        )
        provision.assert_not_called()

    def test_service_registration_or_reprobe_failure_blocks_provision(self) -> None:
        for failure in ("register", "reprobe"):
            with self.subTest(failure=failure):
                doctor = NrfDoctor(FakeSession())
                error = BtpCommandError(f"failed {failure}")
                if failure == "register":
                    doctor._core.register_service = Mock(side_effect=error)
                else:
                    doctor._core.read_supported_commands = Mock(
                        side_effect=[BtpCommandError("unregistered"), error]
                    )
                with (
                    patch("tests.peripherals.nrf52840.doctor.provision") as provision,
                    self.assertRaises(BtpCommandError) as caught,
                ):
                    doctor.run(load_profile(PROFILE))
                self.assertIs(caught.exception, error)
                provision.assert_not_called()

    def test_doctor_rejects_firmware_without_gatt(self) -> None:
        with self.assertRaisesRegex(BtpCommandError, "lacks required GAP/GATT"):
            session = FakeSession(1 << GAP)
            NrfDoctor(session).run(load_profile(PROFILE))
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(session.registered, set())


if __name__ == "__main__":
    unittest.main()
