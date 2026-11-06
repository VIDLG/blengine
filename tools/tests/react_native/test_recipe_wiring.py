"""Protect RN recipe arguments via Just dry-runs; never invoke SDKs or devices."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HOST = Path("tests/consumers/react-native-internal")


def dry_run(
    recipe: str,
    *arguments: str,
    module: str = "react-native",
    include_dependencies: bool = False,
) -> list[list[str]]:
    """Expand recipes without executing any command, optionally including dependencies."""
    command = ["just", "--dry-run"]
    if not include_dependencies:
        command.append("--no-deps")
    result = subprocess.run(
        [*command, module, recipe, *arguments],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=True,
        timeout=15,
    )
    return [shlex.split(line) for line in result.stdout.splitlines() if line.strip()]


class RecipeWiringTests(unittest.TestCase):
    def test_formal_native_contract_gates_target_only_formal_consumer(self) -> None:
        android = dry_run("formal-host-android-native-test", "dry-run-only")
        run = next(
            command
            for command in android
            if "tools.android.run_instrumentation" in command
        )
        self.assertEqual(run[run.index("--serial") + 1], "dry-run-only")
        self.assertEqual(
            run[run.index("--test-class") + 1],
            "com.blehubblenghost.HybridBlengSessionOwnerInstrumentedTest",
        )
        self.assertEqual(
            run[run.index("--test-component") + 1],
            "com.blehubblenghost.test/androidx.test.runner.AndroidJUnitRunner",
        )
        ios_build = dry_run("formal-host-ios-build")
        build = next(
            command
            for command in ios_build
            if "tools.react_native.bleng_ios_consumer" in command
        )
        self.assertIn("--no-tty", build)
        self.assertIn("apple-ci", build)
        self.assertNotIn("test-without-building", build)
        ios = dry_run("formal-host-ios-native-test", "dry-run-only")
        run = next(command for command in ios if "xcodebuild" in command)
        self.assertIn("--no-tty", run)
        self.assertIn("test-without-building", run)
        self.assertIn("platform=iOS Simulator,id=dry-run-only", run)
        self.assertIn("-only-testing:BleHubBlengHostNativeTests", run)
        self.assertEqual(run[run.index("-collect-test-diagnostics") + 1], "never")
        for command in android + ios_build + ios:
            self.assertFalse(any("react-native-internal" in part for part in command))

    def test_formal_ui_gate_has_one_shared_owner_and_no_install_dependencies(
        self,
    ) -> None:
        for platform, activity, team in (
            ("android", ".MainActivity", ""),
            ("ios", "", "MOCKTEAM"),
        ):
            with self.subTest(platform=platform):
                commands = dry_run(
                    "formal-host-ui",
                    platform,
                    "dry-run-only",
                    "example.formal",
                    "path with spaces/appium.js",
                    activity,
                    team,
                    include_dependencies=True,
                )
                self.assertEqual(
                    commands,
                    [
                        [
                            "pixi",
                            "run",
                            "python",
                            "-m",
                            "tools.react_native.run_bleng_ui",
                            "--platform",
                            platform,
                            "--device",
                            "dry-run-only",
                            "--app-id",
                            "example.formal",
                            "--appium-entry",
                            "path with spaces/appium.js",
                            "--activity",
                            activity,
                            "--team",
                            team,
                        ]
                    ],
                )

    @unittest.skipUnless(sys.platform == "darwin", "Apple recipes run on the real Mac")
    def test_ios_doctor_has_no_hidden_build_or_setup_dependencies(self) -> None:
        for mode, app in (
            ("build-signing", ""),
            ("prebuilt", "path with spaces/Probe.app"),
        ):
            with self.subTest(mode=mode):
                self.assertEqual(
                    dry_run(
                        "ios-device-doctor",
                        mode,
                        app,
                        "",
                        module="apple",
                        include_dependencies=True,
                    ),
                    [
                        [
                            "pixi",
                            "run",
                            "-e",
                            "apple-ci",
                            "python",
                            "-m",
                            "tools.apple.ios_preflight",
                            "--mode",
                            mode,
                            "--app",
                            app,
                            "--device",
                            "",
                            "--xctestrun",
                            "",
                        ]
                    ],
                )

    @unittest.skipUnless(sys.platform == "darwin", "Apple recipes run on the real Mac")
    def test_native_success_app_policy_is_explicit_and_defaults_to_keep(self) -> None:
        arguments = (
            "ios",
            "synthetic-device",
            "synthetic-port",
            "contract.json",
            "profile.json",
            "Probe.app",
            "io",
        )
        for policy in (None, "close-owned"):
            with self.subTest(policy=policy):
                commands = dry_run(
                    "rf-native-smoke",
                    *arguments,
                    *(() if policy is None else (policy,)),
                    module="apple",
                    include_dependencies=True,
                )
                self.assertEqual(len(commands), 1)
                self.assertIn("tools.apple.native_hil", commands[0])
                self.assertEqual(commands[0][-2:], ["--success-app", policy or "keep"])

    def test_formal_ios_build_has_one_owner_and_generates_locked_glue_first(
        self,
    ) -> None:
        commands = dry_run("formal-host-ios-build", include_dependencies=True)
        owners = [
            index
            for index, command in enumerate(commands)
            if "tools.react_native.bleng_ios_consumer" in command
        ]
        self.assertEqual(len(owners), 1)
        glue = [
            index
            for index, command in enumerate(commands)
            if command
            == ["npm", "--prefix", "integrations/react-native-bleng", "run", "check"]
        ]
        self.assertEqual(len(glue), 1)
        self.assertLess(glue[0], owners[0])
        self.assertFalse(any("devicectl" in command for command in commands))
        self.assertFalse(any("xcodegen" in command for command in commands))
        recipes = (ROOT / "just/react-native/mod.just").read_text(encoding="utf-8")
        for retired in ("bleng-ios-artifact-build", "bleng-ios-nitro-bridge-build"):
            self.assertNotIn(f"\n{retired}:", recipes)
        self.assertNotIn("bleng_ios_artifact_consumer", recipes)

    def test_ios_device_capture_and_checker_share_explicit_host_log(self) -> None:
        commands = dry_run("host-ios-device-runtime", "dry-run-only", "dry-run-only")
        capture = next(
            command
            for command in commands
            if "tools.react_native.run_ios_device_runtime" in command
        )
        checker = next(
            command
            for command in commands
            if "tools/react_native/check_runtime_log.py" in command
        )
        module = capture.index("tools.react_native.run_ios_device_runtime")
        expected = (HOST / "ios/build/device-runtime.log").as_posix()
        self.assertEqual(capture[:module], ["pixi", "run", "python", "-m"])
        self.assertEqual(
            capture[module + 1 :],
            ["dry-run-only", expected, "--seconds", "20", "--keep-app"],
        )
        self.assertEqual(
            checker,
            [
                "pixi",
                "run",
                "python",
                "tools/react_native/check_runtime_log.py",
                expected,
            ],
        )
        self.assertFalse(any("PYTHONPATH=" in part for part in capture))

    def test_ios_native_lifecycle_selects_whole_native_target_not_ui(self) -> None:
        commands = dry_run("host-ios-native-lifecycle-test", "dry-run-only")
        run = next(
            command for command in commands if "test-without-building" in command
        )
        self.assertIn("xcodebuild", run)
        self.assertEqual(
            [part for part in run if part.startswith("-only-testing:")],
            ["-only-testing:BleHubNitroHostNativeTests"],
        )
        self.assertFalse(any(part.startswith("-skip-testing:") for part in run))
        self.assertIn("platform=iOS Simulator,id=dry-run-only", run)
        self.assertFalse(any("UITests" in part or "RADIO" in part for part in run))
        self.assertEqual(run[run.index("-parallel-testing-enabled") + 1], "NO")
        self.assertEqual(run[run.index("-collect-test-diagnostics") + 1], "never")
        source = (
            ROOT
            / HOST
            / "ios/BleHubNitroHostNativeTests/BleHubNitroHostNativeTests.swift"
        ).read_text(encoding="utf-8")
        classes = set(re.findall(r"final class (\w+): XCTestCase", source))
        self.assertTrue(
            {
                "BleHubNitroHostNativeTests",
                "FullRecoveryBeginGateTests",
                "PendingReloadNativeTests",
            }.issubset(classes),
            "The native target's maintained multi-class coverage must remain discoverable",
        )

    def test_host_jest_total_uses_discovery_once_and_keeps_targeted_scripts(
        self,
    ) -> None:
        package = json.loads((ROOT / HOST / "package.json").read_text(encoding="utf-8"))
        scripts = package["scripts"]
        self.assertEqual(shlex.split(scripts["test"]), ["jest", "--runInBand"])
        self.assertEqual(
            dry_run("host-test"), [["npm", "--prefix", HOST.as_posix(), "test"]]
        )
        for name in (
            "test:ui",
            "test:write-reject",
            "test:ccc-enable-reject",
            "test:ccc-disable-reject",
        ):
            with self.subTest(script=name):
                command = shlex.split(scripts[name])
                self.assertEqual(command[:2], ["jest", "--runInBand"])
                self.assertGreater(len(command), 2)
                for path in command[2:]:
                    self.assertTrue((ROOT / HOST / path).is_file(), path)
