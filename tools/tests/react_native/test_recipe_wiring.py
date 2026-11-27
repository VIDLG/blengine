"""Protect RN recipe arguments via Just dry-runs; never invoke SDKs or devices."""

from __future__ import annotations

import re
import subprocess
import sys
import unittest

from tools.tests.react_native.support.recipes import ROOT, dry_run


class RecipeWiringTests(unittest.TestCase):
    def test_bleng_android_unit_test_is_offline_and_has_no_device_or_setup_step(self):
        self.assertEqual(
            dry_run("bleng-android-unit-test", include_dependencies=True),
            [
                [
                    "pixi",
                    "run",
                    "python",
                    "tools/react_native/run_quiet.py",
                    "--no-tty",
                    "--progress-seconds",
                    "20",
                    ".work/logs/bleng-android-unit-test.log",
                    "--",
                    "pixi",
                    "run",
                    "-e",
                    "android",
                    "gradle",
                    "-p",
                    "integrations/react-native-bleng/android",
                    "testDebugUnitTest",
                    "--offline",
                    "--no-daemon",
                ]
            ],
        )

    def test_tools_discovery_reports_progress_and_filters_without_changing_roots(
        self,
    ) -> None:
        discovery = [
            "python",
            "-m",
            "pytest",
            "tools/tests",
            "-q",
            "-n",
            "auto",
            "--dist",
            "loadfile",
            "--durations=10",
        ]
        for pattern in ("", "*candidate*deadline*"):
            with self.subTest(pattern=pattern):
                arguments = () if not pattern else (pattern,)
                expected = discovery if not pattern else [*discovery, "-k", pattern]
                self.assertEqual(
                    dry_run("tools-test", *arguments, include_dependencies=True),
                    [expected],
                )
        (command,) = dry_run("tools-fixture-test", include_dependencies=True)
        self.assertEqual(command[:2], ["python", "-c"])
        self.assertIn('loader.discover("tools/tests"', command[2])
        self.assertIn('loader.discover("tests/peripherals/nrf52840"', command[2])
        self.assertIn("TextTestRunner(verbosity=2, durations=10)", command[2])
        # The combined entry must read the peripheral tree with unittest: pytest collects
        # its `test_`-named helpers as cases and reports a few hundred false failures.
        self.assertNotIn("pytest", command[2])

    def test_every_tool_test_file_is_reachable(self) -> None:
        """A tool test that nothing runs is not a contract; it either lives under the canonical
        discovery root or a recipe names it.

        One file satisfied neither for months and asserted a `clean_work` policy row that no
        longer existed: green in every review that did not run it, which is every review.
        """
        tools = ROOT / "tools"
        recipes = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "just").rglob("*.just"))
        ) + (ROOT / "justfile").read_text(encoding="utf-8")
        unreachable = []
        for path in sorted(tools.rglob("test_*.py")):
            relative = path.relative_to(tools)
            if relative.parts[0] in {"tests", "archive"}:
                continue
            module = "tools." + ".".join(relative.with_suffix("").parts)
            if module not in recipes:
                unreachable.append(str(relative))
        self.assertEqual(
            unreachable,
            [],
            "move these under tools/tests so discovery runs them, or name them in a recipe",
        )

    def test_no_test_file_outside_a_declared_discovery_root(self) -> None:
        """The same rule must hold outside `tools/`: a test nobody runs is not a contract.

        `tests/android/test_permission_control.py` satisfied neither the canonical tooling
        root nor a recipe name, so `tools-fixture-test` failed on a module path that no
        longer existed while the real file never ran.
        """
        recipes = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "just").rglob("*.just"))
        ) + (ROOT / "justfile").read_text(encoding="utf-8")
        roots = [
            path for path in re.findall(r"discover -s ([\w./-]+)", recipes) if path
        ]
        unreachable = []
        for path in sorted((ROOT / "tests").rglob("test_*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if (
                "archive/" in relative
                or "/consumers/" in relative
                or "/ffi/" in relative
            ):
                continue
            if any(relative.startswith(root.rstrip("/") + "/") for root in roots):
                continue
            module = relative[:-3].replace("/", ".")
            if module not in recipes and relative not in recipes:
                unreachable.append(relative)
        self.assertEqual(
            unreachable,
            [],
            "declare a discovery root in a recipe or move the file under one",
        )

    def test_named_tooling_slices_keep_canonical_discovery_and_scope_filters(
        self,
    ) -> None:
        discovery = [
            "python",
            "-m",
            "pytest",
            "tools/tests",
            "-q",
            "-n",
            "auto",
            "--dist",
            "loadfile",
            "--durations=10",
            "-k",
        ]
        for recipe, pattern in (
            ("tools-test-android-safety", "android_"),
            ("tools-test-lifecycle", "lifecycle"),
            ("tools-test-observed", "regression_"),
        ):
            with self.subTest(recipe=recipe):
                self.assertEqual(
                    dry_run(recipe, include_dependencies=True),
                    [[*discovery, pattern]],
                )

    def test_formal_checks_and_build_do_not_implicitly_install_dependencies(
        self,
    ) -> None:
        for recipe, arguments in (
            ("formal-host-check", ()),
            ("formal-host-test", ()),
            ("formal-host-android-build", ("release",)),
        ):
            with self.subTest(recipe=recipe):
                commands = dry_run(recipe, *arguments, include_dependencies=True)
                self.assertFalse(
                    any("ci" in command or "install" in command for command in commands)
                )
                if recipe != "formal-host-test":
                    self.assertIn(["npm", "run", "typecheck"], commands)
                self.assertFalse(
                    any(
                        "devicectl" in command or "adb" in command
                        for command in commands
                    )
                )
        self.assertEqual(
            dry_run("formal-host-install", include_dependencies=True),
            [["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]],
        )

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
            "com.blehubblenghost.HybridBlengCentralInstrumentedTest",
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

    def test_formal_ios_build_refresh_pods_is_explicit_and_owner_only(self) -> None:
        ordinary = dry_run("formal-host-ios-build", include_dependencies=True)
        self.assertEqual(
            dry_run("formal-host-ios-build", "false", include_dependencies=True),
            ordinary,
        )
        refreshed = dry_run("formal-host-ios-build", "true", include_dependencies=True)
        self.assertEqual(len(refreshed), len(ordinary))
        owners = 0
        for before, after in zip(ordinary, refreshed, strict=True):
            self.assertNotIn("--refresh-pods", before)
            if "tools.react_native.bleng_ios_consumer" in before:
                owners += 1
                self.assertEqual(after, [*before, "--refresh-pods"])
            else:
                self.assertEqual(after, before)
        self.assertEqual(owners, 1)
        self.assertFalse(
            any(
                part in {"pod", "install", "ipc", "ruby", "simctl", "devicectl"}
                for command in refreshed
                for part in command
            )
        )
        native = dry_run(
            "formal-host-ios-native-test", "dry-run-only", include_dependencies=True
        )
        self.assertFalse(any("--refresh-pods" in command for command in native))

    def test_formal_ios_build_rejects_invalid_refresh_pods_values_in_dry_run(
        self,
    ) -> None:
        for value in ("TRUE", "False", "1", "yes", "", "--refresh-pods"):
            with (
                self.subTest(value=value),
                self.assertRaises(subprocess.CalledProcessError) as caught,
            ):
                dry_run("formal-host-ios-build", value)
            self.assertIn("refresh_pods", caught.exception.output)

    def test_formal_ios_ui_gate_uses_ui_only_release_scheme(self) -> None:
        commands = dry_run("formal-host-ios-ui-test", "dry-run-only")
        build = next(command for command in commands if "build-for-testing" in command)
        run = next(
            command for command in commands if "test-without-building" in command
        )
        for command in (build, run):
            self.assertIn("BleHubBlengHostUISimulator", command)
            self.assertEqual(command[command.index("-configuration") + 1], "Release")
            self.assertIn("CODE_SIGNING_ALLOWED=NO", command)
            self.assertNotIn("ENABLE_TESTABILITY=YES", command)
        self.assertIn("generic/platform=iOS Simulator", build)
        self.assertIn("platform=iOS Simulator,id=dry-run-only", run)
        self.assertEqual(
            [part for part in run if part.startswith("-only-testing:")],
            ["-only-testing:BleHubBlengHostUITests"],
        )
        for command in commands:
            self.assertNotIn("-only-testing:BleHubBlengHostNativeTests", command)
            self.assertFalse(
                any(
                    part in {"simctl", "devicectl", "install", "launch", "pod"}
                    for part in command
                )
            )
