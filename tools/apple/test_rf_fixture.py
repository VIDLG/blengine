#!/usr/bin/env python3
"""Pure profile/dispatch regressions. All build, device and RF boundaries are mocked."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from tools.apple import ios_preflight
from tools.apple import test_ios_runtime as ios
from tools.apple import test_macos_runtime as macos
from tools.apple.rf_fixture import load_profile


class IOSRuntimeCliAdmissionTests(unittest.TestCase):
    def test_legacy_ios_rf_cli_rejects_before_environment_build_sdk_or_provider(self):
        cases = (
            ["runtime-rf"],
            ["runtime-rf-android", "io"],
            [
                "runtime-rf-external",
                "notification",
                "--profile",
                "synthetic-profile.json",
                "--coordination-path",
                "synthetic-coordination",
            ],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), ExitStack() as stack:
                stack.enter_context(
                    patch.object(sys, "argv", ["ios-probe", *arguments])
                )
                stack.enter_context(
                    patch.dict(
                        ios.os.environ,
                        {
                            "BLEHUB_APPLE_RF_SERVICE_UUID": "fd600000-0000-4000-8000-000000000000",
                        },
                    )
                )
                stderr = stack.enter_context(redirect_stderr(io.StringIO()))
                forbidden = [
                    stack.enter_context(
                        patch.object(
                            ios,
                            name,
                            side_effect=AssertionError(f"RF rejection reached {name}"),
                        )
                    )
                    for name in (
                        "_environment",
                        "_required_executable",
                        "_generate_project",
                        "_run_xcodegen",
                        "_build_app",
                        "_install_app",
                        "_launch_app",
                        "_run_captured",
                        "_devicectl_json",
                        "_run_ios_android_host_scenario",
                        "_run_ios_nrf_host_scenario",
                        "fixture_from_arguments",
                        "_android_fixture_service_uuid",
                        "runtime",
                    )
                ]
                for name in ("run", "Popen"):
                    forbidden.append(
                        stack.enter_context(
                            patch.object(
                                ios.subprocess,
                                name,
                                side_effect=AssertionError(
                                    "real Apple subprocess forbidden"
                                ),
                            )
                        )
                    )
                with self.assertRaises(SystemExit) as caught:
                    ios.main()
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("rf-native-smoke", stderr.getvalue())
                self.assertIn(
                    "No environment, build, SDK or provider action", stderr.getvalue()
                )
                for action in forbidden:
                    action.assert_not_called()

    def test_non_rf_ios_cli_dispatch_remains_available_without_rf_guard(self):
        for command, target in (
            ("doctor", "doctor"),
            ("runtime", "runtime"),
            ("build-unsigned", "build_unsigned"),
        ):
            with self.subTest(command=command), ExitStack() as stack:
                stack.enter_context(patch.object(sys, "argv", ["ios-probe", command]))
                dispatch = stack.enter_context(
                    patch.object(
                        ios, target, return_value=0 if command == "doctor" else None
                    )
                )
                environment = stack.enter_context(
                    patch.object(
                        ios,
                        "_environment",
                        side_effect=AssertionError(
                            "mock dispatch must not access device"
                        ),
                    )
                )
                run = stack.enter_context(
                    patch.object(
                        ios.subprocess,
                        "run",
                        side_effect=AssertionError("real Apple subprocess forbidden"),
                    )
                )
                popen = stack.enter_context(
                    patch.object(
                        ios.subprocess,
                        "Popen",
                        side_effect=AssertionError("real Apple subprocess forbidden"),
                    )
                )
                self.assertEqual(ios.main(), 0)
                if command == "doctor":
                    dispatch.assert_called_once_with(
                        mode="build-signing", app=None, device=None
                    )
                else:
                    dispatch.assert_called_once_with()
                environment.assert_not_called()
                run.assert_not_called()
                popen.assert_not_called()

    def test_doctor_delegates_without_environment_or_generation(self) -> None:
        cases = (
            ([], "build-signing", None, None, 0),
            (["--mode", "build-signing"], "build-signing", None, None, 0),
            (
                ["--mode", "prebuilt", "--app", "Synthetic.app", "--device", ""],
                "prebuilt",
                "Synthetic.app",
                "",
                0,
            ),
            (
                [
                    "--mode",
                    "prebuilt",
                    "--app",
                    "Synthetic.app",
                    "--device",
                    "synthetic-device",
                ],
                "prebuilt",
                "Synthetic.app",
                "synthetic-device",
                0,
            ),
            ([], "build-signing", None, None, 1),
        )
        for flags, mode, app, device, result in cases:
            with self.subTest(flags=flags, result=result), ExitStack() as stack:
                stack.enter_context(
                    patch.object(sys, "argv", ["ios-probe", "doctor", *flags])
                )
                delegate = stack.enter_context(
                    patch.object(ios_preflight, "run", return_value=result)
                )
                forbidden = [
                    stack.enter_context(
                        patch.object(
                            ios, name, side_effect=AssertionError(f"forbidden {name}")
                        )
                    )
                    for name in (
                        "_environment",
                        "_generate_project",
                        "_regenerate_project",
                        "_run_xcodegen",
                        "_build_app",
                        "_install_app",
                        "_launch_app",
                    )
                ]
                for name in ("run", "Popen"):
                    forbidden.append(
                        stack.enter_context(
                            patch.object(
                                ios.subprocess,
                                name,
                                side_effect=AssertionError(
                                    "real Apple process forbidden"
                                ),
                            )
                        )
                    )
                self.assertEqual(ios.main(), result)
                delegate.assert_called_once_with(mode=mode, app=app, device=device)
                for action in forbidden:
                    action.assert_not_called()

    def test_doctor_flags_rejected_for_every_other_action_before_any_dispatch(
        self,
    ) -> None:
        actions = (
            "runtime",
            "runtime-rf",
            "runtime-rf-android",
            "runtime-rf-external",
            "build-unsigned",
            "unlock-keychain",
            "project-regenerate",
            "project-confirm",
            "signing",
        )
        for action in actions:
            for flags in (["--mode", "build-signing"], ["--app", ""], ["--device", ""]):
                with self.subTest(action=action, flags=flags), ExitStack() as stack:
                    stack.enter_context(
                        patch.object(sys, "argv", ["ios-probe", action, *flags])
                    )
                    forbidden = [
                        stack.enter_context(
                            patch.object(
                                ios,
                                name,
                                side_effect=AssertionError(f"unexpected {name}"),
                            )
                        )
                        for name in (
                            "doctor",
                            "runtime",
                            "build_unsigned",
                            "unlock_login_keychain",
                            "open_project",
                            "_confirm_project_signing",
                            "_environment",
                            "_generate_project",
                            "_regenerate_project",
                            "_run_xcodegen",
                        )
                    ]
                    for name in ("run", "Popen"):
                        forbidden.append(
                            stack.enter_context(
                                patch.object(
                                    ios.subprocess,
                                    name,
                                    side_effect=AssertionError(
                                        "real Apple process forbidden"
                                    ),
                                )
                            )
                        )
                    output = stack.enter_context(redirect_stderr(io.StringIO()))
                    with self.assertRaises(SystemExit) as caught:
                        ios.main()
                    self.assertEqual(caught.exception.code, 2)
                    self.assertIn("only for doctor", output.getvalue())
                    for call in forbidden:
                        call.assert_not_called()


class ExternalFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "external profile.json"
        self.document = json.loads(
            macos.HISTORICAL_NRF_PROFILE_PATH.read_text(encoding="utf-8")
        )
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
        nrf = load_profile(macos.HISTORICAL_NRF_PROFILE_PATH)
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
            # Retain the synthetic routing/evidence regressions behind the live block.
            # Only this test bypasses admission; all external actions stay mocked.
            stack.enter_context(patch.object(macos, "_require_macos_rf_session"))
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
