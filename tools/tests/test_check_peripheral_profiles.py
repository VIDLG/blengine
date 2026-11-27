from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools import check_peripheral_profiles as checker


class MultifunctionProfileIsolationTests(unittest.TestCase):
    def test_historical_json_at_its_unique_report_path_is_never_live_discovery(
        self,
    ) -> None:
        for name in (
            "blehub-nrf-indication-v2.json",
            "blehub-nrf-indication-v2-b.json",
            "blehub-nrf-multifunction-v1-a.json",
            "blehub-nrf-multifunction-v1-b.json",
            "blehub-nrf-att-read-hold-v1.json",
            "blehub-nrf-watchdog-v1.json",
        ):
            path = checker.SCENARIO_ROOT / name
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "evidence-only"):
                    checker.load_profile(path)
                historical = checker.load_profile(path, evidence_only=True)
                self.assertTrue(historical["profile_id"])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(checker.main(), 0)
        live = [
            line
            for line in output.getvalue().splitlines()
            if line.endswith("status=valid")
        ]
        self.assertEqual(len(live), 2)
        self.assertTrue(all("multifunction-v2-" in line for line in live))

    def test_a_cannot_reuse_b_profile_name_or_service_identity(self) -> None:
        profile_b = checker.load_profile(checker.NRF_MULTIFUNCTION_PATH)
        for field in ("profile_id", "local_name", "service_uuid"):
            profile_a = checker.load_profile(checker.NRF_MULTIFUNCTION_A_PATH)
            if field == "profile_id":
                profile_a["profile_id"] = profile_b["profile_id"]
            elif field == "local_name":
                profile_a["advertising"]["local_name"] = profile_b["advertising"][
                    "local_name"
                ]
            else:
                service_uuid = profile_b["services"][0]["uuid"]
                profile_a["services"][0]["uuid"] = service_uuid
                profile_a["advertising"]["service_uuids"] = [service_uuid]
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "profile-a.json"
                path.write_text(json.dumps(profile_a), encoding="utf-8")
                with (
                    self.subTest(field=field),
                    patch.object(checker, "NRF_MULTIFUNCTION_A_PATH", path),
                    redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(ValueError, "distinct"),
                ):
                    checker.main()

    def test_a_identity_gatt_and_authorization_drift_fail_closed(self) -> None:
        for field in (
            "profile_id",
            "local_name",
            "service_uuid",
            "gatt",
            "authorization",
        ):
            profile_a = checker.load_profile(checker.NRF_MULTIFUNCTION_A_PATH)
            if field == "profile_id":
                profile_a["profile_id"] += "-drift"
            elif field == "local_name":
                profile_a["advertising"]["local_name"] += "-drift"
            elif field == "service_uuid":
                service_uuid = "fd800000-0000-4000-8000-000000000000"
                profile_a["services"][0]["uuid"] = service_uuid
                profile_a["advertising"]["service_uuids"] = [service_uuid]
            elif field == "gatt":
                profile_a["services"][0]["characteristics"][0]["initial_value_hex"] = (
                    "01"
                )
            else:
                profile_a["services"][0]["characteristics"][0]["read_authorization"] = (
                    False
                )
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "profile-a.json"
                path.write_text(json.dumps(profile_a), encoding="utf-8")
                with (
                    self.subTest(field=field),
                    patch.object(checker, "NRF_MULTIFUNCTION_A_PATH", path),
                    redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(
                        ValueError,
                        "evidence-only"
                        if field == "profile_id"
                        else "multifunction profile",
                    ),
                ):
                    checker.main()


if __name__ == "__main__":
    unittest.main()
