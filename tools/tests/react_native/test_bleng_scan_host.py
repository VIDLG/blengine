"""Synthetic owned-scan integration; no SDK, serial, provisioning or RF evidence."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import direct_fixture, fixture_session
from tests.peripherals.nrf52840.btp_protocol import BtpFrame
from tests.peripherals.nrf52840.fixture_attestation import parse_attestation
from tests.peripherals.nrf52840.fixture_attestation_wire import encode_attestation
from tests.peripherals.nrf52840.profile import load_profile
from tests.peripherals.nrf52840.resource_lease import DeviceLease
from tests.peripherals.nrf52840.support.direct_fixture_resources import resources
from tests.peripherals.nrf52840.support.session_receipts import write_v2_inputs
from tools.android import state_control
from tools.react_native import bleng_scan_host as host
from tools.react_native import ios_radio_lease, run_bleng_ui
from tools.tests.react_native.support.bleng_ui import (
    ScanDriver,
    advertisement,
    fast_clock,
)


class OwnedScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        contract_path, profile_path = write_v2_inputs(self.root, "b")
        with patch.object(
            fixture_session, "identity_for_port", return_value="jlink:123456"
        ):
            self.plan = fixture_session.prepare_session(
                contract_path,
                profile_path,
                self.root / "attempt",
                port="test-port",
                service_uuid=load_profile(profile_path).service_uuid,
            )
        self.resources = self.enterContext(resources([]))
        self.enterContext(
            patch.object(direct_fixture, "load_profile", wraps=load_profile)
        )
        doctor = self.enterContext(patch.object(direct_fixture, "NrfDoctor"))
        doctor.return_value.run.return_value = SimpleNamespace(
            role_handles={"updates": 37}
        )
        self.resources.session.pop_event.return_value = None
        self.lease = DeviceLease("jlink", "123456", purpose="synthetic owned scan")
        self.lease._pid, self.lease._held = os.getpid(), True
        self.broker = Mock()
        self.broker.poll.return_value = None
        self.lease._process = self.broker
        self.resources.owner.require.side_effect = self.lease.require
        installed = json.loads(contract_path.read_bytes())
        installed.pop("fixture_role")
        payload = encode_attestation(parse_attestation(installed))
        status = self.resources.session.request.return_value

        def request(service: int, opcode: int, **kwargs: object) -> BtpFrame:
            return (
                BtpFrame(255, 1, 255, payload)
                if (service, opcode) == (255, 1)
                else status
            )

        self.resources.session.request.side_effect = request
        self.fixture = direct_fixture.DirectFixture(
            self.plan.port, self.plan.profile_path, fixture_session=self.plan
        )
        self.addCleanup(self.fixture.stop)
        self.fixture.start()  # Real start, attest_session, vendor codec and receipt publication.
        self.clock = self.enterContext(fast_clock())
        self.phone_count = 0

    def phone(
        self, platform: str = "ios", *, initial: bool = False, scope: str | None = None
    ):
        self.phone_count += 1
        evidence = self.root / f"phone-{self.phone_count}.json"
        locks = self.root / f"locks-{self.phone_count}"
        if platform == "ios":
            phone = ios_radio_lease.IosRadioLease(
                "00008110-001234567890ABCD",
                self.plan.session_id,
                evidence,
                lock_root=locks,
                scope=scope or ios_radio_lease.SCAN_OBSERVATION_SCOPE,
            )
        else:
            phone = state_control.AndroidRadioController(
                "synthetic-physical-serial",
                self.plan.session_id,
                evidence,
                lock_root=locks,
            )
        phone.acquire()
        self.addCleanup(phone._release)
        if platform == "android" and initial:
            with (
                patch.object(
                    state_control, "adb_executable", return_value=Path("/synthetic/adb")
                ),
                patch.object(
                    state_control.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "state: ON\n"),
                ) as sdk,
            ):
                phone.initial()
            self.assertEqual(sdk.call_count, 1)
            self.assertIn("dumpsys", sdk.call_args.args[0])
        return phone

    def driver(self, platform: str = "ios") -> ScanDriver:
        return ScanDriver(
            platform,
            [
                advertisement(
                    self.plan.contract.local_name, (self.plan.contract.service_uuid,)
                )
            ],
        )

    def exercise(self, phone=None, driver=None, **overrides):
        phone = self.phone() if phone is None else phone
        driver = self.driver() if driver is None else driver
        binding = (
            getattr(phone, "device", None),
            getattr(phone, "run_id", None),
            driver.app_id,
            driver.session_id,
        )

        def require_app_identity() -> None:
            self.assertEqual(
                (phone.device, phone.run_id, driver.app_id, driver.session_id), binding
            )

        options = {
            "phone": phone,
            "fixture": self.fixture,
            "plan": self.plan,
            "app_id": driver.app_id,
            "require_app_identity": require_app_identity,
            "timeout": 30,
            "observations": [],
        }
        options.update(overrides)
        host.exercise_owned_scan(driver, **options)
        return options["observations"]

    @staticmethod
    def rewrite(path: Path, raw: bytes) -> None:
        path.chmod(0o600)
        path.write_bytes(raw)

    def test_real_active_fixture_receipt_phone_locks_and_scan_ui_on_both_platforms(
        self,
    ) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform):
                phone = self.phone(platform)
                driver = self.driver(platform)
                snapshot = {}
                receipt = self.plan.receipt_path.read_bytes()
                journal = phone.path.read_bytes()
                before = self.resources.session.request.call_count
                with (
                    patch.object(
                        state_control.subprocess,
                        "run",
                        side_effect=AssertionError("SDK during scan"),
                    ),
                    patch.object(
                        self.fixture,
                        "start",
                        side_effect=AssertionError("fixture start during scan"),
                    ),
                    patch.object(
                        self.fixture,
                        "stop",
                        side_effect=AssertionError("fixture stop during scan"),
                    ),
                ):
                    observations = self.exercise(
                        phone, driver, startup_snapshot=snapshot
                    )
                self.assertEqual(
                    observations,
                    [
                        "idle",
                        "started",
                        "scan:running",
                        "scan:advertisement-matched",
                        "scan:stopped",
                        "closed",
                    ],
                )
                self.assertEqual(
                    driver.samples,
                    [
                        advertisement(
                            self.plan.contract.local_name,
                            (self.plan.contract.service_uuid,),
                        )
                    ],
                )
                self.assertEqual(driver.page, "Evidence")
                self.assertEqual(snapshot["state"], "poweredOn")
                self.assertEqual(self.plan.receipt_path.read_bytes(), receipt)
                self.assertEqual(phone.path.read_bytes(), journal)
                if platform == "android":
                    self.assertEqual(phone.document["actions"], [])
                    self.assertIsNone(phone.initial_state)
                    self.assertIsNone(phone.last_state)
                    self.assertIsNone(phone.document["initial_state"])
                self.assertTrue(phone.lock.exists())
                self.assertFalse(phone.finished)
                self.assertEqual(self.resources.session.request.call_count, before)
                driver.quit.assert_not_called()

    def test_only_actual_supported_phone_types_and_observation_scope_are_accepted(
        self,
    ) -> None:
        invalid = (
            object(),
            Mock(spec=ios_radio_lease.IosRadioLease),
            Mock(spec=state_control.AndroidRadioController),
            self.phone(scope=ios_radio_lease.SCOPE),
            self.phone(scope=ios_radio_lease.NATIVE_SYSTEM_LOSS_SCOPE),
        )
        for phone in invalid:
            with (
                self.subTest(phone=phone),
                patch.object(run_bleng_ui, "exercise_scan") as scan,
            ):
                with self.assertRaises((TypeError, ValueError, RuntimeError)):
                    self.exercise(phone)
                scan.assert_not_called()

    def test_android_rejects_prior_radio_actions_and_state_before_ui(self) -> None:
        phone = self.phone("android", initial=True)
        driver = self.driver("android")
        with (
            patch.object(
                state_control.subprocess,
                "run",
                side_effect=AssertionError("SDK during scan admission"),
            ),
            self.assertRaisesRegex(RuntimeError, "no radio actions or state"),
        ):
            self.exercise(phone, driver)
        self.assertEqual(driver.trace, [])
        phone = self.phone("android")
        for field, value in (
            ("initial_state", "ON"),
            ("initial_state", "OFF"),
            ("last_state", "ON"),
            ("last_state", "OFF"),
            ("device", "emulator-5554"),
        ):
            with (
                self.subTest(field=field),
                patch.object(phone, field, value),
                patch.object(run_bleng_ui, "exercise_scan") as scan,
            ):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.exercise(phone)
                scan.assert_not_called()
        for value in ("ON", "OFF"):
            with (
                self.subTest(journal_initial_state=value),
                patch.dict(phone.document, initial_state=value),
                patch.object(run_bleng_ui, "exercise_scan") as scan,
            ):
                with self.assertRaisesRegex(RuntimeError, "no radio actions or state"):
                    self.exercise(phone)
                scan.assert_not_called()
        for label in ("initial", "off", "on", "restore", "final", "controller"):
            with (
                self.subTest(label=label),
                patch.dict(phone.document, actions=[{"label": label}]),
                patch.object(run_bleng_ui, "exercise_scan") as scan,
            ):
                with self.assertRaises(RuntimeError):
                    self.exercise(phone)
                scan.assert_not_called()

    def test_android_radio_drift_is_rejected_after_callback_and_during_scan(
        self,
    ) -> None:
        for phase in ("callback", "scan"):
            for field in ("actions", "initial_state", "last_state"):
                with self.subTest(phase=phase, field=field):
                    phone, driver = self.phone("android"), self.driver("android")
                    cut: list[int] = []
                    identity_checks: list[str] = []

                    def drift(
                        *, phone=phone, field=field, driver=driver, cut=cut
                    ) -> None:
                        cut.append(len(driver.trace))
                        if field == "actions":
                            phone.document["actions"].append({"label": "controller"})
                        else:
                            setattr(phone, field, "ON")

                    def identity(
                        *, phase=phase, drift=drift, identity_checks=identity_checks
                    ) -> None:
                        identity_checks.append(phase)
                        if phase == "callback":
                            drift()

                    def after_action(action: str, *, drift=drift) -> None:
                        if action == "click:next-scan-event":
                            drift()

                    if phase == "scan":
                        driver.after_action = after_action
                    journal = phone.path.read_bytes()
                    with (
                        patch.object(
                            state_control.subprocess,
                            "run",
                            side_effect=AssertionError("SDK during scan"),
                        ),
                        self.assertRaisesRegex(
                            RuntimeError, "no radio actions or state"
                        ),
                    ):
                        self.exercise(phone, driver, require_app_identity=identity)
                    self.assertTrue(identity_checks)
                    self.assertEqual([len(driver.trace)], cut)
                    if phase == "callback":
                        self.assertEqual(driver.trace, [])
                    driver.scan_stop.click.assert_not_called()
                    driver.close.click.assert_not_called()
                    driver.quit.assert_not_called()
                    self.assertEqual(phone.path.read_bytes(), journal)
                    self.assertTrue(phone.lock.exists())
                    self.assertFalse(phone.finished)

    def test_run_role_plan_and_receipt_process_must_match_before_any_ui(self) -> None:
        phone = self.phone()
        cases = (
            {
                "plan": replace(
                    self.plan, session_id="11111111-1111-4111-8111-111111111111"
                )
            },
            {"plan": replace(self.plan, port="foreign-port")},
            {
                "plan": replace(
                    self.plan,
                    contract=replace(self.plan.contract, fixture_role="foreign"),
                )
            },
            {"fixture": Mock(spec=direct_fixture.DirectFixture)},
        )
        for options in cases:
            with (
                self.subTest(options=options),
                patch.object(run_bleng_ui, "exercise_scan") as scan,
            ):
                with self.assertRaises((TypeError, ValueError, RuntimeError)):
                    self.exercise(phone, **options)
                scan.assert_not_called()
        original = self.plan.receipt_path.read_bytes()
        for field in ("fixture_pid", "fixture_pgid"):
            document = json.loads(original)
            document[field] += 1
            self.rewrite(self.plan.receipt_path, json.dumps(document).encode())
            with (
                self.subTest(field=field),
                patch.object(run_bleng_ui, "exercise_scan") as scan,
            ):
                with self.assertRaises(fixture_session.FixtureSessionError):
                    self.exercise(phone)
                scan.assert_not_called()
        self.rewrite(self.plan.receipt_path, original)

    def test_existing_receipt_with_unstarted_fixture_is_rejected(self) -> None:
        phone = self.phone()
        with resources([]):
            idle = direct_fixture.DirectFixture(
                self.plan.port, self.plan.profile_path, fixture_session=self.plan
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "not active"):
                    self.exercise(phone, fixture=idle)
            finally:
                idle.stop()

    def test_identity_callback_is_required_and_its_failure_is_not_swallowed(
        self,
    ) -> None:
        phone, driver = self.phone(), self.driver()
        with self.assertRaises(TypeError):
            host.exercise_owned_scan(
                driver,
                phone=phone,
                fixture=self.fixture,
                plan=self.plan,
                app_id=driver.app_id,
                timeout=30,
                observations=[],
            )
        with self.assertRaises(TypeError):
            self.exercise(phone, driver, require_app_identity=None)
        error = RuntimeError("candidate PID/boot changed")
        with self.assertRaises(RuntimeError) as caught:
            self.exercise(phone, driver, require_app_identity=Mock(side_effect=error))
        self.assertIs(caught.exception, error)
        self.assertEqual(driver.trace, [])

    def test_callback_cannot_drift_snapshots_or_drop_ownership_before_ui(self) -> None:
        phone = self.phone()
        paths = (
            self.plan.request_path,
            self.plan.receipt_path,
            self.plan.root / fixture_session.CONTRACT_NAME,
            self.plan.profile_path,
        )
        for path in paths:
            original = path.read_bytes()

            def drift(path=path, original=original) -> None:
                self.rewrite(path, original + b"\n")

            with (
                self.subTest(path=path),
                patch.object(run_bleng_ui, "exercise_scan") as scan,
            ):
                try:
                    with self.assertRaises(RuntimeError):
                        self.exercise(phone, require_app_identity=drift)
                    scan.assert_not_called()
                finally:
                    self.rewrite(path, original)
        for target, field, value in (
            (phone, "owner", None),
            (phone, "device", "00008110-001234567890ABCE"),
            (phone, "scope", ios_radio_lease.SCOPE),
            (phone, "finished", True),
            (phone, "run_id", "11111111-1111-4111-8111-111111111111"),
            (self.fixture, "_started", False),
            (self.broker, "returncode", 7),
        ):
            original = getattr(target, field)

            def revoke(target=target, field=field, value=value) -> None:
                setattr(target, field, value)

            with (
                self.subTest(field=field),
                patch.object(run_bleng_ui, "exercise_scan") as scan,
            ):
                try:
                    if target is self.broker:
                        with patch.object(
                            self.broker,
                            "poll",
                            side_effect=lambda: self.broker.returncode,
                        ):
                            self.broker.returncode = None
                            with self.assertRaises(RuntimeError):
                                self.exercise(phone, require_app_identity=revoke)
                    else:
                        with self.assertRaises(RuntimeError):
                            self.exercise(phone, require_app_identity=revoke)
                    scan.assert_not_called()
                finally:
                    setattr(target, field, original)

    def test_live_ui_loss_stops_without_another_ui_call_or_host_cleanup(self) -> None:
        for fault in ("receipt", "phone", "broker", "app"):
            with self.subTest(fault=fault):
                phone, driver = self.phone(), self.driver()
                original = self.plan.receipt_path.read_bytes()
                owner = phone.owner
                cut: list[int] = []
                app_error = RuntimeError("host App binding lost")

                def after_action(
                    action: str,
                    *,
                    cut=cut,
                    driver=driver,
                    fault=fault,
                    original=original,
                    phone=phone,
                ) -> None:
                    if action != "click:next-scan-event":
                        return
                    cut.append(len(driver.trace))
                    if fault == "receipt":
                        self.rewrite(self.plan.receipt_path, original + b"\n")
                    elif fault == "phone":
                        phone.owner = None
                    elif fault == "broker":
                        self.broker.poll.return_value = 9

                def app_identity(
                    *, cut=cut, fault=fault, app_error=app_error, driver=driver
                ) -> None:
                    if cut and fault == "app":
                        raise app_error
                    self.assertEqual(driver.session_id, "mock-webdriver-session")

                driver.after_action = after_action
                try:
                    with self.assertRaises(RuntimeError):
                        self.exercise(phone, driver, require_app_identity=app_identity)
                    self.assertEqual([len(driver.trace)], cut)
                    driver.scan_stop.click.assert_not_called()
                    driver.close.click.assert_not_called()
                    driver.quit.assert_not_called()
                finally:
                    phone.owner = owner
                    self.broker.poll.return_value = None
                    self.rewrite(self.plan.receipt_path, original)

    def test_guard_order_contract_derivation_and_remaining_total_budget(self) -> None:
        phone, driver = self.phone("android"), self.driver("android")
        order: list[str] = []
        phone_guard, fixture_guard, verify = (
            phone.require_owner,
            self.fixture.require_active_session,
            fixture_session.verify_receipt,
        )

        def phone_check() -> None:
            order.append("phone")
            phone_guard()

        def fixture_check(plan) -> None:
            order.append("fixture")
            fixture_guard(plan)

        def receipt_check(*args, **kwargs):
            order.append("receipt")
            return verify(*args, **kwargs)

        def app_identity() -> None:
            order.append("app")
            self.clock.now += 1

        def scan(driver, **kwargs) -> None:
            self.assertEqual(kwargs["platform"], "android")
            self.assertEqual(
                kwargs["expected_local_name"], self.plan.contract.local_name
            )
            self.assertEqual(
                kwargs["expected_service_uuid"], self.plan.contract.service_uuid
            )
            self.assertEqual(kwargs["timeout"], 30 - self.clock.now)
            self.assertEqual(kwargs["max_pulls"], 3)
            self.assertEqual(kwargs["observation_timeout_ms"], 1000)
            kwargs["require_authority"]()
            self.clock.now += 2

        with (
            patch.object(phone, "require_owner", side_effect=phone_check),
            patch.object(
                self.fixture, "require_active_session", side_effect=fixture_check
            ),
            patch.object(fixture_session, "verify_receipt", side_effect=receipt_check),
            patch.object(run_bleng_ui, "exercise_scan", side_effect=scan) as exercise,
        ):
            self.exercise(
                phone,
                driver,
                require_app_identity=app_identity,
                max_pulls=3,
                observation_timeout_ms=1000,
            )
        exercise.assert_called_once()
        app_positions = [i for i, value in enumerate(order) if value == "app"]
        self.assertEqual(len(app_positions), 3)  # Before UI, injected guard, after UI.
        for index in app_positions:
            self.assertEqual(
                order[index - 3 : index + 4],
                ["phone", "fixture", "receipt", "app", "phone", "fixture", "receipt"],
            )

    def test_post_ui_guard_rejects_lost_ownership_without_finalizing_it(self) -> None:
        phone, driver = self.phone(), self.driver()
        owner = phone.owner

        def scan(*args, **kwargs) -> None:
            kwargs["require_authority"]()
            phone.owner = None

        with patch.object(run_bleng_ui, "exercise_scan", side_effect=scan) as ui:
            try:
                with self.assertRaises(RuntimeError):
                    self.exercise(phone, driver)
                ui.assert_called_once()
                self.assertTrue(phone.lock.exists())
                self.assertFalse(phone.finished)
                driver.quit.assert_not_called()
            finally:
                phone.owner = owner

    def test_invalid_budgets_and_external_identity_overrides_never_touch_ui(
        self,
    ) -> None:
        phone = self.phone()
        for options in (
            {"timeout": 0},
            {"timeout": float("nan")},
            {"timeout": float("inf")},
            {"timeout": True},
            {"max_pulls": 0},
            {"observation_timeout_ms": 30000},
            {"device": phone.device},
            {"platform": "android"},
            {"expected_local_name": "external"},
            {"expected_service_uuid": "external"},
        ):
            driver = self.driver()
            with self.subTest(options=options):
                with self.assertRaises((ValueError, TypeError)):
                    self.exercise(phone, driver, **options)
                self.assertEqual(driver.trace, [])

    def test_every_phase_and_callback_consume_the_entry_deadline(self) -> None:
        phone = self.phone()
        for phase in ("phone", "fixture", "receipt", "app", "ui"):
            with self.subTest(phase=phase):
                self.clock.now = 0

                def expire(*args, **kwargs) -> None:
                    self.clock.now = 31

                driver = ScanDriver("ios")
                target, name = {
                    "phone": (phone, "require_owner"),
                    "fixture": (self.fixture, "require_active_session"),
                    "receipt": (fixture_session, "verify_receipt"),
                    "ui": (run_bleng_ui, "exercise_scan"),
                }.get(phase, (None, None))
                if phase == "app":
                    with self.assertRaises(TimeoutError):
                        self.exercise(phone, driver, require_app_identity=expire)
                else:
                    with (
                        patch.object(target, name, side_effect=expire),
                        self.assertRaises(TimeoutError),
                    ):
                        self.exercise(phone, driver)
                self.assertEqual(driver.trace, [])


if __name__ == "__main__":
    unittest.main()
