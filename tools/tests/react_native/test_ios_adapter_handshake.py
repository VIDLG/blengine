"""Hardware-free boundaries for the non-RF iOS XCTest handshake staging."""

from __future__ import annotations

import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.react_native import ios_adapter_handshake as handshake
from tools.react_native.rf_evidence_bundle import EvidenceError


class IosAdapterHandshakeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / "Debug-iphoneos/BleHubNitroHost.app"
        self.app.mkdir(parents=True)
        (self.app / "BleHubNitroHost").write_bytes(b"signed app placeholder")
        self.source = self.root / "input.xctestrun"
        self.target = {
            "IsUITestBundle": True,
            "TestHostPath": "__TESTROOT__/Debug-iphoneos/BleHubNitroHostUITests-Runner.app",
            "UITargetAppPath": "__TESTROOT__/Debug-iphoneos/BleHubNitroHost.app",
            "DependentProductPaths": [
                "__TESTROOT__/Debug-iphoneos/BleHubNitroHost.app",
                "__TESTROOT__/Debug-iphoneos/BleHubNitroHostUITests-Runner.app",
            ],
            "EnvironmentVariables": {"TERM": "dumb"},
            "TestingEnvironmentVariables": {"XCODE_SCHEME_NAME": "host"},
            "CommandLineArguments": [],
            "UITargetAppCommandLineArguments": [],
        }
        self.source.write_bytes(
            plistlib.dumps(
                {
                    handshake.TARGET: self.target,
                    "BleHubNitroHostNativeTests": {"IsTestBundle": True},
                    "__xctestrun_metadata__": {"FormatVersion": 1},
                }
            )
        )

    def test_stage_restricts_to_handshake_method_and_exact_gate(self) -> None:
        destination = self.root / "staged.xctestrun"
        digest = handshake.stage_test_run(
            self.source,
            destination,
            self.app,
            run_id="12345678-1234-4123-8123-123456789abc",
        )
        document = plistlib.loads(destination.read_bytes())
        target = document[handshake.TARGET]
        self.assertEqual(target["OnlyTestIdentifiers"], [handshake.METHOD])
        self.assertEqual(target["SkipTestIdentifiers"], [])
        self.assertEqual(target["CommandLineArguments"], [])
        self.assertEqual(target["UITargetAppCommandLineArguments"], [])
        self.assertEqual(
            target["EnvironmentVariables"]["BLEHUB_IOS_ADAPTER_HANDSHAKE_GATE"], "1"
        )
        self.assertEqual(len(digest), 64)
        self.assertNotIn("BLEHUB", target["TestingEnvironmentVariables"])

    def test_stage_rejects_existing_output_and_repetition_options(self) -> None:
        destination = self.root / "staged.xctestrun"
        destination.write_bytes(b"occupied")
        with self.assertRaises(EvidenceError):
            handshake.stage_test_run(
                self.source,
                destination,
                self.app,
                run_id="12345678-1234-4123-8123-123456789abc",
            )
        destination.unlink()
        mutated = dict(self.target)
        mutated["TestExecutionOrdering"] = "retry-tests-on-failure"
        self.source.write_bytes(
            plistlib.dumps(
                {
                    handshake.TARGET: mutated,
                    "__xctestrun_metadata__": {"FormatVersion": 1},
                }
            )
        )
        with self.assertRaises(EvidenceError):
            handshake.stage_test_run(
                self.source,
                destination,
                self.app,
                run_id="12345678-1234-4123-8123-123456789abc",
            )

    def test_staging_rejects_stale_gates_in_every_environment(self) -> None:
        index = 0
        for field in (
            "EnvironmentVariables",
            "TestingEnvironmentVariables",
            "UITargetAppEnvironmentVariables",
        ):
            for environment in (
                {"BLEHUB_IOS_RADIO_CONTROL_GATE": "1"},
                {"BLEHUB_IOS_ADAPTER_STARTUP_GATE": "1"},
                {"BLEHUB_IOS_ADAPTER_HANDSHAKE_GATE": "1"},
                {"TERM": ["not", "a", "string"]},
                ["not a mapping"],
            ):
                with self.subTest(field=field, environment=environment):
                    target = {**self.target, field: environment}
                    self.source.write_bytes(plistlib.dumps({handshake.TARGET: target}))
                    index += 1
                    destination = self.root / f"rejected-{index}.xctestrun"
                    with self.assertRaises(EvidenceError):
                        handshake.stage_test_run(
                            self.source,
                            destination,
                            self.app,
                            run_id="12345678-1234-4123-8123-123456789abc",
                        )
                    self.assertFalse(destination.exists())

    def test_failed_runner_classification_requires_positive_own_device_summary(
        self,
    ) -> None:
        device = "00000000-0000000000000001"
        original_write = Path.write_text
        for kind in (
            "initialization",
            "unknown",
            "foreign-device",
            "coerced-counts",
            "malformed-failure",
            "export-failure",
            "publish-failure",
        ):
            with self.subTest(kind=kind):
                log = self.root / (kind + ".log")
                commands = []
                summary = {
                    "result": "Failed",
                    "totalTestCount": 1,
                    "passedTests": 0,
                    "failedTests": 1,
                    "devicesAndConfigurations": [{"device": {"deviceId": device}}],
                    "testFailures": [
                        {
                            "targetName": handshake.TARGET,
                            "testIdentifierString": handshake.TARGET
                            + "-Runner (123) encountered an error",
                            "failureText": "The test runner failed to initialize for UI testing. (Underlying Error: Timed out while enabling automation mode.)",
                        }
                    ],
                }
                if kind == "unknown":
                    summary["testFailures"] = []
                if kind == "foreign-device":
                    summary["devicesAndConfigurations"][0]["device"]["deviceId"] = (
                        "foreign"
                    )
                if kind == "coerced-counts":
                    summary.update(
                        totalTestCount=True, passedTests=False, failedTests=True
                    )
                if kind == "malformed-failure":
                    summary["testFailures"][0]["failureText"] = {
                        "message": summary["testFailures"][0]["failureText"]
                    }

                def sdk(
                    command, _commands=commands, _kind=kind, _summary=summary, **kwargs
                ):
                    _commands.append(command)
                    self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                    self.assertLessEqual(kwargs["timeout"], 60)
                    if command[1:4] == ["devicectl", "list", "devices"]:
                        return subprocess.CompletedProcess(
                            command, 0, device + " available"
                        )
                    if "lockState" in command:
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            json.dumps(
                                {
                                    "result": {
                                        "passcodeRequired": False,
                                        "unlockedSinceBoot": True,
                                    }
                                }
                            ),
                        )
                    self.assertEqual(
                        command[:5],
                        ["xcrun", "xcresulttool", "get", "test-results", "summary"],
                    )
                    if _kind == "export-failure":
                        raise subprocess.CalledProcessError(1, command)
                    kwargs["stdout"].write(json.dumps(_summary).encode())
                    return subprocess.CompletedProcess(command, 0)

                def start(command, _log=log, **kwargs):
                    self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                    self.assertIn("test-without-building", command)
                    self.assertIn("-only-testing:" + handshake.IDENTIFIER, command)
                    _log.with_suffix(".xcresult").mkdir()
                    process = Mock()
                    process.poll.return_value = 65
                    return process

                def write(path, *args, _kind=kind, _log=log, **kwargs):
                    if _kind == "publish-failure" and path == _log.with_suffix(
                        ".capture.json"
                    ):
                        raise OSError("synthetic capture disk full")
                    return original_write(path, *args, **kwargs)

                with (
                    patch.object(Path, "write_text", new=write),
                    patch(
                        "tools.react_native.ios_device.shutil.which",
                        return_value="xcrun",
                    ),
                    patch.object(handshake.subprocess, "run", side_effect=sdk),
                    patch.object(
                        handshake.subprocess, "Popen", side_effect=start
                    ) as launch,
                    self.assertRaisesRegex(
                        EvidenceError, "without live handshake marker"
                    ) as failure,
                ):
                    handshake.run(
                        device, app=self.app, test_run=self.source, log=log, timeout=5
                    )
                launch.assert_called_once()
                if kind == "publish-failure":
                    self.assertFalse(log.with_suffix(".capture.json").exists())
                    self.assertTrue(
                        any(
                            "handshake result publication" in note
                            for note in failure.exception.__notes__
                        )
                    )
                    continue
                report = json.loads(log.with_suffix(".capture.json").read_bytes())
                self.assertEqual(report["status"], "failed")
                self.assertFalse(
                    report["radio_mutated"]
                    or report["fixture_started"]
                    or report["probe_started"]
                )
                if kind == "initialization":
                    self.assertEqual(
                        report["failure"]["stage"], "ui-runner-initialization"
                    )
                    self.assertIs(report["test_body_started"], False)
                else:
                    self.assertEqual(report["failure"]["stage"], "ui-handshake")
                    self.assertIsNone(report["test_body_started"])
                self.assertIn("undetermined", report["failure"]["root_cause"])
                self.assertIn("without live handshake marker", report["error"])
                self.assertEqual(
                    bool(report["diagnostic_errors"]), kind == "export-failure"
                )
                self.assertEqual(len(commands), 3)

    def test_marker_requires_independent_exact_schema_and_identity(self) -> None:
        value = handshake._validate_marker(
            {
                "schema_version": 1,
                "run_id": "12345678-1234-4123-8123-123456789abc",
                "nonce": "12345678-1234-4123-8123-123456789abc",
                "source": "ios-ui-handshake",
                "test_process_id": 123,
            },
            "12345678-1234-4123-8123-123456789abc",
        )
        self.assertEqual(value["source"], "ios-ui-handshake")
        for key, replacement in (("run_id", "foreign"), ("source", "rf")):
            invalid = dict(value, **{key: replacement})
            with self.subTest(key=key), self.assertRaises(EvidenceError):
                handshake._validate_marker(
                    invalid, "12345678-1234-4123-8123-123456789abc"
                )


if __name__ == "__main__":
    unittest.main()
