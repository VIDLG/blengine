from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from builtins import BaseExceptionGroup
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import (
    direct_fixture,
    doctor,
    fixture_session,
)
from tests.peripherals.nrf52840.btp_commands import GAP, GATT, BtpCommandError
from tests.peripherals.nrf52840.btp_session import BtpSession
from tests.peripherals.nrf52840.btp_transport import FramedTransport
from tests.peripherals.nrf52840.fault_control import FaultController, FaultWireError
from tests.peripherals.nrf52840.fixture_attestation import (
    FixtureAttestationError,
    parse_attestation,
)
from tests.peripherals.nrf52840.fixture_attestation_wire import encode_attestation
from tests.peripherals.nrf52840.gap_commands import CONTROLLER_INFO
from tests.peripherals.nrf52840.profile import load_profile
from tests.peripherals.nrf52840.resource_lease import DeviceLease, ResourceLeaseError
from tests.peripherals.nrf52840.serial_transport import SerialByteTransport
from tests.peripherals.nrf52840.support.direct_fixture_resources import (
    ServiceWire,
    bind_btp_session,
    sample_plan,
    startup_mocks,
)
from tests.peripherals.nrf52840.support.direct_fixture_resources import (
    resources as fixture_resources,
)


class DirectFixtureActiveSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        firmware = Path(__file__).resolve().parent / "firmware"
        profile_path = firmware / "profiles/multifunction-rf-v2-b.json"
        profile = load_profile(profile_path)
        declaration = json.loads(
            (firmware / "images/multifunction-rf-v2-b.json").read_bytes()
        )
        declaration.pop("settings")
        declaration.update(
            image_sha256="ab" * 32,
            service_uuid=profile.service_uuid,
            local_name=profile.local_name,
        )
        contract_path = root / "synthetic-contract.json"
        contract_path.write_text(json.dumps(declaration), encoding="utf-8")
        with patch.object(
            fixture_session, "identity_for_port", return_value="jlink:123456"
        ):
            self.plan = fixture_session.prepare_session(
                contract_path,
                profile_path,
                root / "attempt",
                port="test-port",
                service_uuid=profile.service_uuid,
            )
        declaration.pop("fixture_role")
        self.wire = ServiceWire([])
        self.installed = parse_attestation(declaration)
        self.wire.attestation = encode_attestation(self.installed)
        self.resources = self.enterContext(fixture_resources(self.wire.events))
        self.resources.session.pop_event.return_value = None
        # Exercise DeviceLease.require itself; only its subprocess observation is fake.
        self.lease = DeviceLease("jlink", "123456", purpose="synthetic active guard")
        self.lease._pid = os.getpid()
        self.lease._held = True
        self.broker = Mock()
        self.broker.poll.return_value = None
        self.lease._process = self.broker
        self.resources.owner.require.side_effect = self.lease.require
        self.enterContext(
            patch.object(direct_fixture, "load_profile", wraps=load_profile)
        )
        bind_btp_session(self.resources, BtpSession(self.wire))
        self.provision = self.enterContext(
            patch.object(doctor, "provision", return_value={"read-write": 51})
        )
        self.fixture = direct_fixture.DirectFixture(
            self.plan.port, self.plan.profile_path, fixture_session=self.plan
        )
        self.addCleanup(self.fixture.stop)

    def guard(self, plan: fixture_session.FixtureSession | None = None) -> None:
        before = (
            len(self.wire.sent),
            list(self.resources.serial.mock_calls),
            list(self.resources.gap.mock_calls),
            list(self.resources.gatt.mock_calls),
        )
        receipt = (
            (
                self.plan.receipt_path.read_bytes(),
                self.plan.receipt_path.stat().st_mtime_ns,
            )
            if self.plan.receipt_path.exists()
            else None
        )
        try:
            self.assertIsNone(
                self.fixture.require_active_session(self.plan if plan is None else plan)
            )
        finally:
            self.assertEqual(
                before,
                (
                    len(self.wire.sent),
                    list(self.resources.serial.mock_calls),
                    list(self.resources.gap.mock_calls),
                    list(self.resources.gatt.mock_calls),
                ),
            )
            self.assertEqual(
                receipt,
                (
                    self.plan.receipt_path.read_bytes(),
                    self.plan.receipt_path.stat().st_mtime_ns,
                )
                if self.plan.receipt_path.exists()
                else None,
            )

    def test_unstarted_and_receipt_only_fixture_are_not_active(self) -> None:
        for old_receipt in (False, True):
            with self.subTest(old_receipt=old_receipt):
                if old_receipt:
                    self.plan.receipt_path.write_text("old receipt", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "not active"):
                    self.guard()
        self.resources.owner.require.assert_called_once()  # Constructor only.
        self.assertEqual(self.wire.sent, [])

    def test_real_start_attests_advertises_then_guard_is_read_only_until_stop(
        self,
    ) -> None:
        def advertising(*args: object) -> None:
            with self.assertRaisesRegex(RuntimeError, "not active"):
                self.guard()

        self.resources.gap.start_advertising.side_effect = advertising
        self.assertEqual(self.fixture.start(), {"read-write": 51})
        receipt = self.plan.receipt_path.read_bytes()
        fixture_session.verify_receipt(
            self.plan, fixture_identity=(os.getpid(), os.getpgid(0))
        )
        self.assertEqual(self.wire.events.count("attest"), 1)
        self.resources.gap.start_advertising.assert_called_once()
        self.resources.owner.require.reset_mock()
        self.guard(replace(self.plan))  # Value equality, not Python object identity.
        self.resources.owner.require.assert_called_once_with("jlink", "123456")

        def stopping() -> None:
            with self.assertRaisesRegex(RuntimeError, "not active"):
                self.guard()

        with patch.object(
            self.fixture, "_disconnect_connected_peers", side_effect=stopping
        ):
            self.fixture.stop()
        with self.assertRaisesRegex(RuntimeError, "not active"):
            self.guard()
        self.assertEqual(self.plan.receipt_path.read_bytes(), receipt)

    def failed_start(self, stage: str) -> None:
        if stage == "attest":
            self.wire.attestation = encode_attestation(
                replace(self.installed, image_sha256="00" * 32)
            )
        elif stage == "provision":
            self.provision.side_effect = RuntimeError("provision failed")
        else:
            self.resources.gap.start_advertising.side_effect = RuntimeError(
                "advertise failed"
            )
        with self.assertRaises(RuntimeError):
            self.fixture.start()
        with self.assertRaisesRegex(RuntimeError, "not active"):
            self.guard()
        before = len(self.wire.sent), self.provision.call_count
        with self.assertRaises(RuntimeError):
            self.fixture.start()
        self.assertEqual((len(self.wire.sent), self.provision.call_count), before)
        self.fixture.stop()
        with self.assertRaisesRegex(RuntimeError, "not active"):
            self.guard()
        if stage == "attest":
            self.assertFalse(self.plan.receipt_path.exists())
            self.assertEqual(self.resources.gap.mock_calls, [])
            self.assertEqual(self.resources.gatt.mock_calls, [])
            self.provision.assert_not_called()
        else:
            self.assertTrue(self.plan.receipt_path.exists())

    def test_failed_proof_never_becomes_active_or_allows_teardown_mutation(
        self,
    ) -> None:
        self.failed_start("attest")

    def test_provision_failure_never_becomes_active_or_retries(self) -> None:
        self.failed_start("provision")

    def test_advertising_failure_never_becomes_active_or_retries(self) -> None:
        self.failed_start("advertise")

    def test_old_receipt_does_not_authorize_unstarted_or_new_fixture_start(
        self,
    ) -> None:
        self.fixture.start()
        original = self.plan.receipt_path.read_bytes()
        self.fixture.stop()
        self.wire.events.clear()
        with fixture_resources(self.wire.events) as resources:
            fixture = direct_fixture.DirectFixture(
                self.plan.port, self.plan.profile_path, fixture_session=self.plan
            )
            with self.assertRaisesRegex(RuntimeError, "not active"):
                fixture.require_active_session(self.plan)
            with self.assertRaisesRegex(
                fixture_session.FixtureSessionError, "existing receipt"
            ):
                fixture.start()
            resources.session.request.assert_not_called()
            self.assertEqual(resources.gap.mock_calls, [])
            self.assertEqual(resources.gatt.mock_calls, [])
            self.assertEqual(self.plan.receipt_path.read_bytes(), original)
            with self.assertRaisesRegex(RuntimeError, "not active"):
                fixture.require_active_session(self.plan)

    def test_foreign_plan_and_local_identity_drift_fail_before_broker_check(
        self,
    ) -> None:
        self.fixture.start()
        self.resources.owner.require.reset_mock()
        for foreign in (
            replace(self.plan, session_id="87654321-4321-4321-8321-cba987654321"),
            replace(self.plan, port="foreign-port"),
            replace(self.plan, resource_identity="jlink:999999"),
        ):
            with self.subTest(plan=foreign), self.assertRaises(RuntimeError):
                self.guard(foreign)
        for target, field, value in (
            (self.fixture, "_fixture_session", None),
            (self.fixture, "_port", "foreign-port"),
            (self.resources.owner, "identity", "jlink:999999"),
            (self.fixture, "_stop_requested", True),
            (self.fixture, "_mutation_allowed", False),
        ):
            with (
                self.subTest(field=field),
                patch.object(target, field, value),
                self.assertRaises(RuntimeError),
            ):
                self.guard()
        self.resources.owner.require.assert_not_called()

    def test_broker_loss_is_rejected_without_btp_or_serial_io(self) -> None:
        self.fixture.start()
        for change in ("exited", "released", "inherited"):
            with (
                self.subTest(change=change),
                patch.object(
                    self.broker, "poll", return_value=9 if change == "exited" else None
                ),
                patch.object(self.lease, "_held", change != "released"),
                patch.object(self.lease, "_pid", os.getpid() + (change == "inherited")),
                self.assertRaisesRegex(ResourceLeaseError, "not active"),
            ):
                self.guard()

    def test_local_state_is_rechecked_after_broker_validation(self) -> None:
        self.fixture.start()
        for target, field, value in (
            (self.fixture, "_fixture_session", None),
            (self.fixture, "_port", "foreign-port"),
            (self.resources.owner, "identity", "jlink:999999"),
            (self.fixture, "_stop_requested", True),
            (self.fixture, "_stopping", True),
            (self.fixture, "_started", False),
            (self.fixture, "_mutation_allowed", False),
        ):
            with self.subTest(field=field):
                original = getattr(target, field)

                with patch.object(
                    self.fixture._transport,
                    "require",
                    side_effect=partial(setattr, target, field, value),
                ):
                    try:
                        with self.assertRaises(RuntimeError):
                            self.guard()
                    finally:
                        setattr(target, field, original)

    def test_duplicate_start_is_rejected_without_attestation_or_provisioning(
        self,
    ) -> None:
        self.fixture.start()
        before = len(self.wire.sent), self.provision.call_count
        with self.assertRaisesRegex(RuntimeError, "already"):
            self.fixture.start()
        self.assertEqual((len(self.wire.sent), self.provision.call_count), before)
        self.guard()


class DirectFixtureLeaseTests(unittest.TestCase):
    def test_attestation_failure_blocks_all_mutation_and_ready_and_releases(
        self,
    ) -> None:
        plan = sample_plan()
        profile = plan.profile_path
        for error in (
            FixtureAttestationError("wrong image"),
            TimeoutError("attestation timed out"),
            ResourceLeaseError("selected identity mismatch"),
        ):
            with self.subTest(error=error):
                events: list[str] = []
                attest = Mock(side_effect=error)

                with (
                    fixture_resources(events) as resources,
                    patch.object(fixture_session, "attest_session", attest),
                    patch.object(direct_fixture, "NrfDoctor") as doctor,
                    redirect_stdout(io.StringIO()) as output,
                    self.assertRaisesRegex(type(error), str(error)),
                ):
                    direct_fixture.serve("test-port", profile, fixture_session=plan)
                attest.assert_called_once_with(
                    plan, resources.session, lease=resources.owner, port="test-port"
                )
                doctor.assert_not_called()
                self.assertEqual(resources.gap.mock_calls, [])
                self.assertEqual(resources.gatt.mock_calls, [])
                self.assertNotIn("NRF52840_DIRECT_READY", output.getvalue())
                resources.session.close.assert_called_once_with()
                resources.serial.close.assert_called_once_with()
                self.assertEqual(
                    events[-3:], ["session-close", "serial-close", "release"]
                )
                self.assertEqual(events.count("release"), 1)

    def test_real_start_registers_before_hook_and_provisions_only_after_hook(
        self,
    ) -> None:
        for warm in (False, True):
            with self.subTest(warm=warm):
                events: list[str] = []
                plan = sample_plan()
                profile = plan.profile_path
                session = BtpSession(ServiceWire(events, warm=warm))
                handles = {"read-write": 51}
                with (
                    fixture_resources(events) as resources,
                    patch.object(
                        fixture_session,
                        "attest_session",
                        side_effect=lambda *a, **kw: resources.step("attest"),
                    ),
                    patch.object(
                        doctor,
                        "provision",
                        side_effect=lambda *a, handles=handles: resources.step(
                            "provision", handles
                        ),
                    ),
                    patch.object(
                        direct_fixture.DirectFixture,
                        "_start_advertising",
                        side_effect=lambda: resources.step("advertise"),
                    ),
                ):
                    bind_btp_session(resources, session)
                    fixture = direct_fixture.DirectFixture(
                        "test-port", profile, fixture_session=plan
                    )

                    def hook(fixture: direct_fixture.DirectFixture = fixture) -> None:
                        resources.step("hook")
                        self.assertEqual(
                            FaultController(
                                fixture._session, fixture._fixture_session.session_id
                            )
                            .require_idle()
                            .engine_flags,
                            0,
                        )

                    start = len(events)
                    try:
                        self.assertEqual(fixture.start(before_mutations=hook), handles)
                        service_steps = ["services"]
                        for service in (GAP, GATT):
                            service_steps.append(f"probe-{service}")
                            if not warm:
                                service_steps.extend(
                                    (f"register-{service}", f"probe-{service}")
                                )
                        self.assertEqual(
                            events[start:],
                            [
                                "attest",
                                *service_steps,
                                "status",
                                "hook",
                                "status",
                                "advertising-stop",
                                "services",
                                f"probe-{GAP}",
                                f"probe-{GATT}",
                                "controller",
                                "provision",
                                "advertising-stop",
                                "advertise",
                            ],
                        )
                    finally:
                        fixture._mutation_allowed = False
                        fixture.stop()
                    self.assertEqual(events.count("release"), 1)
                    resources.session.close.assert_called_once_with()

    def test_real_start_hook_failure_never_provisions_or_performs_teardown_mutations(
        self,
    ) -> None:
        for fault in ("foreign", "unknown", "hook"):
            with self.subTest(fault=fault):
                events: list[str] = []
                plan = sample_plan()
                profile = plan.profile_path
                session = BtpSession(ServiceWire(events, fault=fault))
                with (
                    fixture_resources(events) as resources,
                    patch.object(fixture_session, "attest_session"),
                    patch.object(doctor, "provision") as provision,
                    patch.object(
                        direct_fixture.DirectFixture, "_start_advertising"
                    ) as advertise,
                ):
                    bind_btp_session(resources, session)
                    fixture = direct_fixture.DirectFixture(
                        "test-port", profile, fixture_session=plan
                    )

                    def hook(fixture: direct_fixture.DirectFixture = fixture) -> None:
                        FaultController(
                            fixture._session, fixture._fixture_session.session_id
                        ).require_idle()
                        raise RuntimeError("hook failed")

                    error_type, message = (
                        (BtpCommandError, "ATT fault BTP failure")
                        if fault == "unknown"
                        else (FaultWireError, "foreign resources")
                        if fault == "foreign"
                        else (RuntimeError, "hook failed")
                    )
                    with self.assertRaisesRegex(error_type, message):
                        fixture.start(before_mutations=hook)
                    fixture.stop()
                    self.assertFalse(fixture._mutation_allowed)
                    provision.assert_not_called()
                    advertise.assert_not_called()
                    self.assertEqual(resources.gap.mock_calls, [])
                    self.assertEqual(resources.gatt.mock_calls, [])
                    self.assertEqual(events.count(f"register-{GATT}"), 1)
                    self.assertLess(
                        events.index(f"probe-{GATT}"), events.index("status")
                    )
                    self.assertEqual(
                        events[-3:], ["session-close", "serial-close", "release"]
                    )
                    resources.session.close.assert_called_once_with()

    def test_session_profile_mismatch_fails_before_lease_or_serial(self) -> None:
        with (
            patch.object(direct_fixture, "port_lease") as lease,
            patch.object(direct_fixture, "open_serial") as serial,
            self.assertRaisesRegex(ValueError, "profile does not match"),
        ):
            direct_fixture.DirectFixture(
                "test-port",
                Path("canonical-profile.json"),
                fixture_session=SimpleNamespace(
                    profile_path=Path("snapshot.json"),
                    port="test-port",
                    resource_identity="jlink:123456",
                ),
            )
        lease.assert_not_called()
        serial.assert_not_called()

    def test_session_port_and_dk_identity_mismatch_release_without_serial_open(
        self,
    ) -> None:
        profile = Path("immutable-profile.json")
        for port, identity, message in (
            ("swapped-port", "jlink:123456", "port does not match"),
            ("test-port", "jlink:999999", "resource identity does not match"),
        ):
            with self.subTest(port=port, identity=identity):
                events: list[str] = []
                plan = sample_plan(port=port, resource_identity=identity)
                with (
                    fixture_resources(events) as resources,
                    patch.object(fixture_session, "attest_session") as attest,
                    redirect_stdout(io.StringIO()) as output,
                    self.assertRaisesRegex(ResourceLeaseError, message),
                ):
                    direct_fixture.serve("test-port", profile, fixture_session=plan)
                resources.port_lease.assert_called_once_with(
                    "test-port", purpose="BleHub direct BTP fixture", lease=None
                )
                resources.open_serial.assert_not_called()
                resources.serial.close.assert_not_called()
                resources.session.close.assert_not_called()
                self.assertEqual(resources.gap.mock_calls, [])
                self.assertEqual(resources.gatt.mock_calls, [])
                attest.assert_not_called()
                self.assertNotIn("NRF52840_DIRECT_READY", output.getvalue())
                self.assertEqual(events, ["acquire", "release"])

    def test_lease_loss_before_start_closes_without_mutation(self) -> None:
        events: list[str] = []
        with fixture_resources(events) as resources:
            fixture = direct_fixture.DirectFixture("test-port", Path("test-profile"))
            resources.owner.require.side_effect = ResourceLeaseError("broker exited")
            with self.assertRaisesRegex(ResourceLeaseError, "broker exited"):
                fixture.start()
            fixture.stop()
            resources.gap.stop_advertising.assert_not_called()
            self.assertEqual(resources.gatt.mock_calls, [])
        self.assertEqual(events[-3:], ["session-close", "serial-close", "release"])
        self.assertEqual(events.count("release"), 1)

    def test_lease_loss_blocks_each_serial_read_and_write_but_not_close(self) -> None:
        owner = Mock(spec=DeviceLease)
        owner.identity = "jlink:123456"
        owner.require.side_effect = ResourceLeaseError("broker exited")
        serial = Mock()
        transport = direct_fixture._LeasedTransport(serial, owner)
        for operation in (
            lambda: transport.write(b"command"),
            lambda: transport.read(1.0),
        ):
            with self.assertRaisesRegex(ResourceLeaseError, "broker exited"):
                operation()
        serial.write.assert_not_called()
        serial.read.assert_not_called()
        self.assertEqual(owner.require.call_count, 2)
        owner.require.assert_called_with("jlink", "123456")
        transport.close()
        serial.close.assert_called_once_with()

    def test_conflict_prevents_serial_open(self) -> None:
        events: list[str] = []
        with fixture_resources(events, ("acquire",)) as resources:
            with self.assertRaisesRegex(ResourceLeaseError, "failed acquire"):
                direct_fixture.DirectFixture("test-port", Path("test-profile"))
            resources.open_serial.assert_not_called()
            resources.serial.close.assert_not_called()
            resources.session.close.assert_not_called()
        self.assertEqual(events, ["acquire"])

    def test_constructor_failure_closes_resources_before_releasing_lease(self) -> None:
        for failure in ("open", "transport", "session", "gap", "gatt", "profile"):
            with self.subTest(failure=failure):
                events: list[str] = []
                with fixture_resources(events, (failure,)) as resources:
                    with self.assertRaisesRegex(RuntimeError, f"failed {failure}"):
                        direct_fixture.DirectFixture("test-port", Path("test-profile"))
                    if failure != "open":
                        resources.serial.close.assert_called_once_with()
                        self.assertEqual(events[-2], "serial-close")
                    if failure in ("gap", "gatt", "profile"):
                        resources.session.close.assert_called_once_with()
                        self.assertEqual(events[-3], "session-close")
                    else:
                        resources.session.close.assert_not_called()
                self.assertEqual(events[-1], "release")

    def test_constructor_cleanup_failures_still_release_lease(self) -> None:
        events: list[str] = []
        with (
            fixture_resources(events, ("profile", "session-close", "serial-close")),
            self.assertRaises(BaseExceptionGroup) as caught,
        ):
            direct_fixture.DirectFixture("test-port", Path("test-profile"))
        self.assertIsInstance(caught.exception, direct_fixture.FixtureConstructionError)
        self.assertIsInstance(caught.exception, Exception)
        self.assertEqual(
            [str(error) for error in caught.exception.exceptions],
            ["failed profile", "failed session-close", "failed serial-close"],
        )
        self.assertEqual(
            events[-4:], ["profile", "session-close", "serial-close", "release"]
        )

    def test_constructor_primary_and_each_unwind_error_keep_identity_and_kind(
        self,
    ) -> None:
        for interrupted in ("none", "construction", "session", "lease"):
            with self.subTest(interrupted=interrupted):
                events: list[str] = []
                primary = (
                    KeyboardInterrupt("construction interrupted")
                    if interrupted == "construction"
                    else ValueError("invalid profile")
                )
                session_error = (
                    KeyboardInterrupt("session close interrupted")
                    if interrupted == "session"
                    else RuntimeError("session close failed")
                )
                serial_error = OSError("serial close failed")
                lease_error = (
                    KeyboardInterrupt("lease release interrupted")
                    if interrupted == "lease"
                    else ResourceLeaseError("lease release failed")
                )
                with fixture_resources(events) as resources:
                    original_scope = resources.port_lease.side_effect

                    @contextmanager
                    def scope(
                        *args,
                        original_scope=original_scope,
                        lease_error=lease_error,
                        **kwargs,
                    ):
                        with original_scope(*args, **kwargs) as owner:
                            yield owner
                        raise lease_error

                    def fail(step, error):
                        resources.step(step)
                        raise error

                    resources.port_lease.side_effect = scope
                    direct_fixture.load_profile.side_effect = (
                        lambda _, primary=primary: fail("profile", primary)
                    )
                    resources.session.close.side_effect = partial(
                        fail, "session-close", session_error
                    )
                    resources.serial.close.side_effect = partial(
                        fail, "serial-close", serial_error
                    )
                    with self.assertRaises(BaseException) as caught:
                        direct_fixture.DirectFixture("test-port", Path("test-profile"))
                    error = caught.exception
                    self.assertIsInstance(error, BaseExceptionGroup)
                    self.assertIsInstance(
                        error, direct_fixture.FixtureConstructionError
                    )
                    self.assertEqual(
                        error.exceptions,
                        (primary, session_error, serial_error, lease_error),
                    )
                    self.assertEqual(
                        isinstance(error, Exception), interrupted == "none"
                    )
                    # Parent classification survives except*/subgroup processing.
                    ordinary, interruption = error.split(Exception)
                    self.assertIsInstance(
                        ordinary, direct_fixture.FixtureConstructionError
                    )
                    if interrupted != "none":
                        self.assertIsInstance(
                            interruption, direct_fixture.FixtureConstructionError
                        )
                    resources.session.close.assert_called_once()
                    resources.serial.close.assert_called_once()
                    self.assertEqual(
                        events[-4:],
                        ["profile", "session-close", "serial-close", "release"],
                    )
                    self.assertEqual(resources.gap.mock_calls, [])
                    self.assertEqual(resources.gatt.mock_calls, [])

    def test_constructor_real_session_and_serial_close_failure_is_collected_once(
        self,
    ) -> None:
        events: list[str] = []
        primary = ValueError("profile rejected after serial open")
        secondary = KeyboardInterrupt("physical serial close interrupted")
        port = Mock()
        port.close.side_effect = secondary
        serial = SerialByteTransport(port)
        with (
            fixture_resources(events) as resources,
            patch.object(direct_fixture, "FramedTransport", FramedTransport),
            patch.object(direct_fixture, "BtpSession", BtpSession),
        ):
            resources.open_serial.side_effect = lambda _: resources.step("open", serial)
            direct_fixture.load_profile.side_effect = primary
            with self.assertRaises(BaseExceptionGroup) as caught:
                direct_fixture.DirectFixture("test-port", Path("test-profile"))
            self.assertIsInstance(
                caught.exception, direct_fixture.FixtureConstructionError
            )
            self.assertEqual(caught.exception.exceptions, (primary, secondary))
            port.close.assert_called_once()
            port.write.assert_not_called()
            port.read.assert_not_called()
            self.assertEqual(events.count("release"), 1)
            self.assertEqual(events[-1], "release")
            self.assertEqual(resources.gap.mock_calls, [])
            self.assertEqual(resources.gatt.mock_calls, [])

    def test_constructor_clean_failure_and_interruption_are_reraised_unchanged(
        self,
    ) -> None:
        for primary in (
            ValueError("invalid profile"),
            KeyboardInterrupt("construct interrupted"),
        ):
            with self.subTest(primary=primary):
                events: list[str] = []
                with fixture_resources(events) as resources:
                    direct_fixture.load_profile.side_effect = primary
                    with self.assertRaises(type(primary)) as caught:
                        direct_fixture.DirectFixture("test-port", Path("test-profile"))
                    self.assertIs(caught.exception, primary)
                    resources.session.close.assert_called_once()
                    resources.serial.close.assert_called_once()
                    self.assertEqual(
                        events[-3:], ["session-close", "serial-close", "release"]
                    )
                    self.assertEqual(resources.gap.mock_calls, [])
                    self.assertEqual(resources.gatt.mock_calls, [])

    def test_stop_holds_lease_through_cleanup_and_releases_once(self) -> None:
        events: list[str] = []
        with fixture_resources(events) as resources:
            fixture = direct_fixture.DirectFixture("test-port", Path("test-profile"))
            self.assertNotIn("release", events)
            with patch.object(
                fixture,
                "_disconnect_connected_peers",
                side_effect=lambda: resources.step("disconnect"),
            ):
                fixture.stop()
                fixture.stop()
            self.assertEqual(
                events[-6:],
                [
                    "disconnect",
                    "advertising-stop",
                    "reset",
                    "session-close",
                    "serial-close",
                    "release",
                ],
            )
            resources.port_lease.assert_called_once_with(
                "test-port", purpose="BleHub direct BTP fixture", lease=None
            )
            resources.session.close.assert_called_once_with()
            resources.serial.close.assert_called_once_with()
            self.assertEqual(events.count("release"), 1)

    def test_stop_failures_still_close_and_release_once(self) -> None:
        for failure in (
            "disconnect",
            "advertising-stop",
            "reset",
            "session-close",
            "serial-close",
        ):
            with self.subTest(failure=failure):
                events: list[str] = []
                with fixture_resources(events, (failure,)) as resources:
                    fixture = direct_fixture.DirectFixture(
                        "test-port", Path("test-profile")
                    )
                    with patch.object(
                        fixture,
                        "_disconnect_connected_peers",
                        side_effect=lambda: resources.step("disconnect"),
                    ):
                        with self.assertRaisesRegex(RuntimeError, f"failed {failure}"):
                            fixture.stop()
                        fixture.stop()
                    resources.session.close.assert_called_once_with()
                    resources.serial.close.assert_called_once_with()
                self.assertEqual(
                    events[-3:], ["session-close", "serial-close", "release"]
                )
                self.assertEqual(events.count("release"), 1)

    def test_serve_start_failure_uses_fixture_lease_and_cleanup(self) -> None:
        events: list[str] = []
        with (
            fixture_resources(events) as resources,
            patch.object(
                direct_fixture.DirectFixture,
                "start",
                side_effect=RuntimeError("start failed"),
            ),
            patch.object(direct_fixture.DirectFixture, "_disconnect_connected_peers"),
            self.assertRaisesRegex(RuntimeError, "start failed"),
        ):
            direct_fixture.serve("test-port", Path("test-profile"))
        resources.port_lease.assert_called_once()
        resources.open_serial.assert_called_once_with("test-port")
        self.assertEqual(events[-3:], ["session-close", "serial-close", "release"])


class DirectFixtureLocalAddressTests(unittest.TestCase):
    def fixture(self, *, started: bool = True) -> direct_fixture.DirectFixture:
        fixture = object.__new__(direct_fixture.DirectFixture)
        fixture._started = started
        fixture._stopping = False
        fixture._stop_requested = False
        fixture._gap = SimpleNamespace(
            read_controller_info=lambda: CONTROLLER_INFO.pack(
                bytes.fromhex("665544332211"), 0, 0, bytes(3), bytes(249), bytes(11)
            )
        )
        return fixture

    def test_the_address_is_the_controllers_own_static_address(self):
        # BTP carries it little-endian; evidence and a sniffer both want display order.
        self.assertEqual(self.fixture().local_address(), "11:22:33:44:55:66")

    def test_an_inactive_fixture_refuses_to_report_an_address(self):
        with self.assertRaises(RuntimeError):
            self.fixture(started=False).local_address()


class DirectFixtureCycleTests(unittest.TestCase):
    def test_an_adopted_peer_is_refused_by_default(self) -> None:
        # The strict answer: a link that appears during the resume readback contradicts every
        # scene that expects the link to follow its own scan.
        with self.assertRaisesRegex(RuntimeError, "adopted a peer"):
            direct_fixture.require_resume_adoption_allowed(True, expect_adoption=False)

    def test_a_scene_that_states_adoption_is_not_refused(self) -> None:
        # `auto-connect` exists to prove a standing "connect when the peer appears" request,
        # so the peer adopting the resumed advertisement is the expected answer.
        direct_fixture.require_resume_adoption_allowed(True, expect_adoption=True)
        direct_fixture.require_resume_adoption_allowed(False, expect_adoption=False)

    def test_a_next_cycle_proves_absence_before_offering_the_peer_again(self) -> None:
        fixture = object.__new__(direct_fixture.DirectFixture)
        calls: list[tuple[str, float]] = []
        fixture.settle_no_peer = lambda **kwargs: calls.append(
            ("settle", kwargs["deadline"])
        )
        fixture.resume_advertising = lambda **kwargs: calls.append(
            ("resume", kwargs["deadline"])
        )
        fixture.prepare_next_cycle(deadline=12.5)
        self.assertEqual(calls, [("settle", 12.5), ("resume", 12.5)])


if __name__ == "__main__":
    unittest.main()
