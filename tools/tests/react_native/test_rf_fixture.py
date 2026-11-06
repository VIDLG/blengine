"""Live-attested fixture preflight and ownership boundaries, independent of CLI."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.peripherals.nrf52840 import fixture_contract, fixture_session
from tools.react_native import rf_fixture as api

ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = ROOT / "tests/conformance/scenarios"


class FixturePreflightTests(unittest.TestCase):
    def test_multifunction_identity_is_derived_and_service_override_must_match(
        self,
    ) -> None:
        for variant in ("a", "b"):
            with (
                self.subTest(variant=variant),
                tempfile.TemporaryDirectory() as directory,
            ):
                from tests.peripherals.nrf52840.support.session_receipts import (
                    write_v2_inputs,
                )

                contract, profile = write_v2_inputs(Path(directory), variant)
                expected = fixture_contract.load_contract(contract).service_uuid
                self.assertEqual(
                    api.preflight_fixture(
                        fixture_contract=contract, fixture_profile=profile
                    ),
                    expected,
                )
                with self.assertRaisesRegex(
                    api.RfFixtureError, "runner service UUID does not match"
                ):
                    api.preflight_fixture(
                        fixture_contract=contract,
                        fixture_profile=profile,
                        service_uuid="12345678-1234-4000-8000-123456789abc",
                    )

    def test_missing_contract_or_profile_fails_before_reading_or_validating_inputs(
        self,
    ) -> None:
        for contract, profile in (
            (None, None),
            (Path("contract"), None),
            (None, Path("profile")),
        ):
            with (
                self.subTest(contract=contract, profile=profile),
                patch.object(fixture_contract, "load_contract") as load,
                patch.object(fixture_session, "validate_fixture_inputs") as validate,
            ):
                with self.assertRaisesRegex(
                    api.RfFixtureError,
                    "explicit --fixture-contract and --fixture-profile",
                ):
                    api.preflight_fixture(
                        fixture_contract=contract, fixture_profile=profile
                    )
                load.assert_not_called()
                validate.assert_not_called()

    def test_legacy_role_is_rejected_and_profile_validation_failure_is_preserved(
        self,
    ) -> None:
        for role in ("normal-rf", "diagnostic-watchdog", "multifunction-rf"):
            with (
                self.subTest(role=role),
                patch.object(
                    fixture_contract,
                    "load_contract",
                    return_value=SimpleNamespace(
                        service_uuid="fd500000-0000-4000-8000-000000000000",
                        fixture_role=role,
                    ),
                ),
                patch.object(fixture_session, "validate_fixture_inputs") as validate,
            ):
                primary = RuntimeError("profile/image/service mismatch")
                validate.side_effect = primary
                with self.assertRaises(api.RfFixtureError) as caught:
                    api.preflight_fixture(
                        fixture_contract=Path("contract"),
                        fixture_profile=Path("selected-profile"),
                    )
                if role == "multifunction-rf":
                    self.assertIs(caught.exception.__cause__, primary)
                    self.assertEqual(
                        validate.call_args.args,
                        (Path("contract"), Path("selected-profile")),
                    )
                else:
                    self.assertIn("requires multifunction-rf", str(caught.exception))
                    validate.assert_not_called()


class FixtureOwnershipTests(unittest.TestCase):
    def test_startup_identity_requires_unique_exact_positive_pid_and_pgid(self) -> None:
        valid = "nrf52840_fixture_pid=1234\nnrf52840_fixture_pgid=5678\n"
        self.assertEqual(api.fixture_identity_from_stdout(valid), (1234, 5678))
        for invalid in (
            "",
            valid + "nrf52840_fixture_pid=1234\n",
            valid.replace("=1234", "=0"),
            valid.replace("=5678", "=-1"),
            valid.replace("=1234", "= 1234"),
            valid.splitlines()[0],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(api.RfFixtureError):
                api.fixture_identity_from_stdout(invalid)

    def test_replaced_or_exited_fixture_never_passes_live_guard(self) -> None:
        owner = (1234, 5678)
        with (
            patch.object(
                api.provider, "_process_identity", return_value=owner
            ) as identity,
            patch.object(
                api.provider,
                "_fixture_state",
                return_value=api.provider._PROCESS_RUNNING,
            ) as state,
        ):
            api.guard_fixture(owner)
            state.assert_called_once_with(*owner)
            identity.return_value = (9999, 9999)
            state.reset_mock()
            with self.assertRaisesRegex(api.RfFixtureError, "identity changed"):
                api.guard_fixture(owner)
            state.assert_not_called()
            identity.return_value = owner
            state.return_value = "EXITED"
            with self.assertRaisesRegex(api.RfFixtureError, "not RUNNING"):
                api.guard_fixture(owner)
