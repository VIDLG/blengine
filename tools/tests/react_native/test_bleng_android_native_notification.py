"""Hardware-free regressions for the native notification scene's host boundaries."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.profile import load_profile
from tests.peripherals.nrf52840.support.session_receipts import write_v2_inputs
from tools.react_native import bleng_android_native_notification as scene


class SceneContractTests(unittest.TestCase):
    def test_every_scene_contract_is_ordered_and_duplicate_free(self):
        for name, (_, stages) in scene.SCENES.items():
            with self.subTest(scene=name):
                self.assertEqual(len(stages), len(set(stages)))
                # Each scene reaches its own first subscription report once.
                self.assertEqual(stages.count("subscribed"), 1)
                self.assertEqual(stages[-1], "done")
                self.assertLess(stages.index("subscribed"), stages.index("done"))

    def test_the_scenes_differ_only_after_the_subscription(self):
        prefix = scene.PREFIX_STAGES
        for name, (_, stages) in scene.SCENES.items():
            with self.subTest(scene=name):
                self.assertEqual(stages[: len(prefix)], prefix)
        # A slow consumer must never claim a matched value; a value scene must never
        # claim an overflow. The two observations cannot both describe one run.
        self.assertIn("overflow-observed", scene.SCENES["overflow"][1])
        self.assertNotIn("values-matched", scene.SCENES["overflow"][1])
        for name in ("single", "sequence"):
            self.assertNotIn("overflow-observed", scene.SCENES[name][1])

    def test_the_overflow_run_exceeds_the_supervisor_value_budget(self):
        self.assertGreater(scene.MAX_SCENE_VALUES, 64)


class SubscriptionTargetTests(unittest.TestCase):
    def plan(self, root: Path):
        contract, profile = write_v2_inputs(root, "a")
        with patch.object(
            fixture_session, "identity_for_port", return_value="jlink:123456"
        ):
            return fixture_session.prepare_session(
                contract,
                profile,
                root / "session",
                port="test-port",
                service_uuid=load_profile(profile).service_uuid,
            )

    def test_target_facts_come_from_the_profile_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = self.plan(Path(temporary))
            profile = load_profile(plan.profile_path)
            for role, expected_mode in (
                ("updates", "notification"),
                ("indications", "indication"),
            ):
                with self.subTest(role=role):
                    uuid, mode, value = scene._subscription_target(plan, role)
                    characteristic = next(
                        item for item in profile.characteristics if item.role == role
                    )
                    self.assertEqual(uuid, characteristic.uuid)
                    self.assertEqual(mode, expected_mode)
                    self.assertEqual(value, profile.notification_value.hex())

    def test_an_unknown_role_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = self.plan(Path(temporary))
            with self.assertRaises(ValueError) as raised:
                scene._subscription_target(plan, "absent")
            self.assertIn("no 'absent' role", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
