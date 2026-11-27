"""Scene targets are derived from the fixture profile, never declared by a scene."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.fixture_contract import load_contract
from tests.peripherals.nrf52840.profile import PROPERTY_BITS
from tools.react_native.scene_targets import (
    io_target,
    read_write_target,
    subscription_target,
)

ROOT = Path(__file__).resolve().parents[3]
PROFILE = (
    ROOT / "tests/peripherals/nrf52840/firmware/profiles/multifunction-rf-v2-a.json"
)
CONTRACT = (
    ROOT / "tests/peripherals/nrf52840/firmware/contracts/multifunction-rf-v2-a.json"
)


class SceneTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        # The derivations are pure profile reads, so the plan is the two inputs they use:
        # a parsed contract and the exact profile bytes frozen for this session.
        self.plan = SimpleNamespace(
            contract=load_contract(CONTRACT), profile_path=PROFILE
        )

    def test_subscription_target_reports_the_profile_mode_and_value(self) -> None:
        uuid, mode, value = subscription_target(self.plan, "updates")
        self.assertRegex(uuid, r"^[0-9a-f-]{36}$")
        self.assertIn(mode, {"notification", "indication"})
        self.assertRegex(value, r"^[0-9a-f]+$")

    def test_subscription_target_refuses_an_unknown_role(self) -> None:
        with self.assertRaisesRegex(ValueError, "no 'absent' role"):
            subscription_target(self.plan, "absent")

    def test_read_write_target_follows_the_provisioned_length(self) -> None:
        uuid, value = read_write_target(self.plan, "read-write")
        self.assertRegex(uuid, r"^[0-9a-f-]{36}$")
        self.assertTrue(value)

    def test_io_write_payload_differs_from_the_provisioned_value(self) -> None:
        # The read-back is only proof if the bytes changed, and the length is the fixture's
        # decision: a write longer than the stored value is refused by the tester.
        uuid, provisioned = read_write_target(self.plan, "read-write")
        payload_uuid, payload = io_target(self.plan, "read-write")
        self.assertEqual(uuid, payload_uuid)
        self.assertNotEqual(payload, provisioned)
        self.assertEqual(len(payload), len(provisioned))
        self.assertTrue(payload.decode("ascii").isprintable())

    def test_read_write_target_rejects_a_role_that_is_not_writable(self) -> None:
        plan = SimpleNamespace(contract=load_contract(CONTRACT), profile_path=PROFILE)
        profile = fixture_session.load_session_profile(plan.contract, plan.profile_path)
        notify = next(
            item for item in profile.characteristics if item.role == "indications"
        )
        self.assertFalse(
            notify.properties
            & (PROPERTY_BITS["write"] | PROPERTY_BITS["write_without_response"])
        )
        with self.assertRaisesRegex(ValueError, "not writable"):
            read_write_target(plan, "indications")


class SceneTargetFailureTests(unittest.TestCase):
    """A profile that cannot serve the scene is a profile defect, not something to guess."""

    def test_provisioned_value_longer_than_the_write_input_is_refused(self) -> None:
        plan = SimpleNamespace(contract=load_contract(CONTRACT), profile_path=PROFILE)
        with (
            patch(
                "tools.react_native.scene_targets.read_write_target",
                return_value=("uuid", b"x" * 17),
            ),
            self.assertRaisesRegex(ValueError, "bounded to 16 bytes"),
        ):
            io_target(plan, "read-write")


if __name__ == "__main__":
    unittest.main()
