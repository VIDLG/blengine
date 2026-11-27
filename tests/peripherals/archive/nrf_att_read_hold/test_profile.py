"""Fail-closed diagnostic profile isolation without a connected DK."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.peripherals.nrf52840.profile import ProfileError, load_profile

from .profile import (
    TARGET_PERMISSIONS,
    load_diagnostic_contract,
    load_diagnostic_profile,
)

SCENARIO = (
    Path(__file__).resolve().parents[3]
    / "conformance/scenarios/blehub-nrf-att-read-hold-v1.json"
)


class DiagnosticProfileTests(unittest.TestCase):
    def test_authorization_is_explicit_and_rejected_by_normal_provider(self) -> None:
        profile = load_diagnostic_profile(SCENARIO)
        contract = load_diagnostic_contract()
        self.assertEqual(
            (profile.profile_id, profile.service_uuid, profile.local_name),
            (contract.profile_id, contract.service_uuid, contract.local_name),
        )
        self.assertEqual(profile.characteristics[0].permissions, TARGET_PERMISSIONS)
        with self.assertRaisesRegex(ProfileError, "retired AUTH_READ"):
            load_profile(SCENARIO)

    def test_identity_and_authorization_drift_fail_closed(self) -> None:
        baseline = json.loads(SCENARIO.read_text(encoding="utf-8"))
        for label, change in (
            ("profile_id", lambda doc: doc.update(profile_id="normal")),
            ("local_name", lambda doc: doc["advertising"].update(local_name="other")),
            ("service", lambda doc: doc["services"][0].update(uuid="other")),
            (
                "missing authorization",
                lambda doc: doc["services"][0]["characteristics"][0].pop(
                    "read_authorization"
                ),
            ),
            (
                "second authorization",
                lambda doc: doc["services"][0]["characteristics"][1].update(
                    read_authorization=True
                ),
            ),
            (
                "wrong target",
                lambda doc: doc["services"][0]["characteristics"][0].update(
                    initial_value_hex="01"
                ),
            ),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                document = json.loads(json.dumps(baseline))
                change(document)
                path = Path(directory) / "profile.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(ProfileError):
                    load_diagnostic_profile(path)


if __name__ == "__main__":
    unittest.main()
