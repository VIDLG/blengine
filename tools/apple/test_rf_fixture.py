#!/usr/bin/env python3
"""Pure profile/dispatch regressions. All build, device and RF boundaries are mocked."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tools.apple import test_ios_runtime as ios
from tools.apple import test_macos_runtime as macos
from tools.apple.rf_fixture import load_profile


class ExternalFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "external profile.json"
        self.document = json.loads(macos.NRF_PROFILE_PATH.read_text(encoding="utf-8"))
        self.document["profile_id"] = "external-basic-v1"
        service = "fd000000-0000-4000-8000-000000000000"
        self.document["advertising"]["service_uuids"] = [service]
        self.document["services"][0]["uuid"] = service
        self.document["services"][0]["characteristics"] = [
            c
            for c in self.document["services"][0]["characteristics"]
            if c["role"] != "indication-updates"
        ]
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self.document), encoding="utf-8")

    def test_four_characteristic_one_byte_and_default_android_contracts(self) -> None:
        external = load_profile(self.path, "10", "AB")
        self.assertTrue(external.hasIndicationOnly)
        self.assertEqual(external.writeWithoutResponseHex, "ab")
        self.assertEqual(external.serviceUuid, self.document["services"][0]["uuid"])
        self.assertEqual(
            json.loads(external.app_arguments()[1])["profileId"], "external-basic-v1"
        )
        nrf = load_profile(macos.NRF_PROFILE_PATH)
        self.assertTrue(nrf.hasIndicationOnly)
        self.assertEqual(
            (
                nrf.initialValueHex,
                nrf.writeWithResponseHex,
                nrf.writeWithoutResponseHex,
            ),
            ("00", "102030", "abcd"),
        )

    def test_rejects_unsupported_shape_duplicates_properties_and_payloads(self) -> None:
        original = json.dumps(self.document)
        mutations = [
            lambda d: d.update(schema_version=2),
            lambda d: d["advertising"].update(service_uuids=[]),
            lambda d: d["services"][0].update(primary=False),
            lambda d: d["services"][0]["characteristics"].pop(),
            lambda d: d["services"][0]["characteristics"].append(
                d["services"][0]["characteristics"][0]
            ),
            lambda d: d["services"][0]["characteristics"][0].update(
                properties=["read"]
            ),
            lambda d: d["services"][0]["characteristics"][0].update(
                uuid="0000fff1-0000-1000-8000-00805f9b34fb"
            ),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.document = json.loads(original)
                mutate(self.document)
                self.save()
                with self.assertRaises(ValueError):
                    load_profile(self.path, "10", "ab")
        self.document = json.loads(original)
        self.save()
        for response, no_response in [
            ("0", "ab"),
            ("zz", "ab"),
            ("", "ab"),
            ("10", "10"),
            ("00", "ab"),
            ("10" * 513, "ab"),
        ]:
            with (
                self.subTest(payload=(response, no_response)),
                self.assertRaises(ValueError),
            ):
                load_profile(self.path, response, no_response)

    def test_macos_external_routes_never_touch_android_and_require_profile_evidence(
        self,
    ) -> None:
        fixture = load_profile(self.path, "10", "ab")
        markers = {
            "scan": "scan",
            "connection": "connection",
            "gatt": "gatt",
            "io": "gatt_io",
            "rssi": "rssi",
        }
        with ExitStack() as stack:
            stack.enter_context(patch.object(macos, "WORK_ROOT", self.root))
            forbidden = [
                stack.enter_context(
                    patch.object(macos, name, side_effect=AssertionError(name))
                )
                for name in (
                    "_android_fixture_service_uuid",
                    "_run_notification_coordination",
                    "_run_passive_disconnect_coordination",
                )
            ]
            for scenario in ("scan", "connection", "gatt", "io", "rssi"):

                def launch(
                    command: list[str], *, timeout: int, scenario: str = scenario
                ) -> None:
                    self.assertEqual(timeout, 180)
                    self.assertEqual(
                        command[command.index("--scan-service-uuid") + 1],
                        fixture.serviceUuid,
                    )
                    self.assertIn(fixture.app_arguments()[1], command)
                    result = Path(command[command.index("--result-path") + 1])
                    result.write_text(
                        "\n".join(
                            fixture.evidence()
                            + [f"apple_macos_corebluetooth_{markers[scenario]}=passed"]
                        ),
                        encoding="utf-8",
                    )

                with patch.object(macos, "_run", side_effect=launch):
                    macos._launch("open", macos.RF_SCENARIO_COMMANDS[scenario], fixture)
            with self.assertRaises(RuntimeError):
                macos._launch("open", "notifications-android", fixture)
            for call in forbidden:
                call.assert_not_called()

    def test_ios_external_io_dispatch_and_final_ledger_gate(self) -> None:
        fixture = load_profile(self.path, "10", "ab")
        result = "\n".join(
            fixture.evidence()
            + [
                "apple_ios_probe_run_id=fresh",
                "apple_ios_rf_scenario=io",
                "apple_ios_rf_io=passed",
                "apple_ios_rf_ledger=passed",
                "apple_ios_rf_post_dispose_silence=passed",
                "apple_ios_corebluetooth_rf=passed",
            ]
        )
        with ExitStack() as stack:
            stack.enter_context(patch.dict(ios.os.environ, {}, clear=True))
            stack.enter_context(
                patch.object(
                    ios,
                    "_environment",
                    return_value=(
                        "xcodegen",
                        "xcodebuild",
                        "xcrun",
                        object(),
                        "team",
                        "bundle",
                    ),
                )
            )
            for name in (
                "_print_environment",
                "_generate_project",
                "_build_app",
                "_install_app",
            ):
                stack.enter_context(patch.object(ios, name))
            stack.enter_context(
                patch.object(
                    ios.uuid, "uuid4", return_value=type("Run", (), {"hex": "fresh"})()
                )
            )
            launch = stack.enter_context(patch.object(ios, "_launch_app"))
            android = stack.enter_context(
                patch.object(
                    ios,
                    "_run_ios_android_host_scenario",
                    side_effect=AssertionError("Android control"),
                )
            )
            wait = stack.enter_context(
                patch.object(ios, "_wait_for_runtime_result", return_value=result)
            )
            ios.runtime(fixture=fixture)
            self.assertEqual(
                launch.call_args.args[-3:], (fixture.serviceUuid, "io", fixture)
            )
            android.assert_not_called()
            for missing in ["apple_ios_rf_ledger=passed", fixture.evidence()[0]]:
                wait.return_value = result.replace(missing, "")
                with self.assertRaises(RuntimeError):
                    ios.runtime(fixture=fixture)
            with self.assertRaises(RuntimeError):
                ios.runtime(scenario="notification", fixture=fixture)


if __name__ == "__main__":
    unittest.main()
