"""Build-only boundaries for the formal RN host and isolated Swift contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from builtins import BaseExceptionGroup, ExceptionGroup
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools.react_native import bleng_ios_consumer as consumer
from tools.react_native import ios_pod_preparation, ios_pod_refresh

SPEC_SOURCE = (
    Path(__file__).resolve().parents[3]
    / "tests/consumers/react-native-bleng/ios/PlatformContract/project.yml"
)


class BlengIOSConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        host = self.root / "formal host"
        ios = host / "ios"
        contract = ios / "PlatformContract"
        work = self.root / ".work/react-native-bleng-ios-consumer/PlatformContract"
        derived = work / "DerivedData"
        products = derived / "Build/Products/Debug-iphonesimulator"
        paths = {
            "ROOT": self.root,
            "HOST_ROOT": host,
            "IOS_ROOT": ios,
            "FORMAL_PACKAGE_ROOT": host / "node_modules/@blengine/react-native-bleng",
            "RN_STAGED_ROOT": self.root / "staged combined",
            "SPEC": contract / "project.yml",
            "SMOKE_ROOT": contract / "Sources",
            "WORK_ROOT": work,
            "RENDERED_SPEC": work / "project.yml",
            "PROJECT": work / "BlengPlatformContract.xcodeproj",
            "DERIVED_DATA": derived,
            "PRODUCT": products / "libBlengPlatformContract.a",
            "SWIFT_MODULE": products / "BlengPlatformContract.swiftmodule",
            "HOST_PROJECT": ios / "BleHubBlengHost.xcodeproj/project.pbxproj",
            "WORKSPACE": ios / "BleHubBlengHost.xcworkspace",
            "HOST_DERIVED_DATA": ios / "build/simulator-derived",
            "NATIVE_TEST_SOURCE": ios
            / "BleHubBlengHostNativeTests/BleHubBlengHostNativeTests.swift",
            "NATIVE_TEST_ENTRY": ios / "BleHubBlengHostNativeTests/BlengHostReload.js",
            "NATIVE_TEST_BUNDLE": ios
            / "build/native-test-resources/BlengHostReload.jsbundle",
            "RN_CLI": host / "node_modules/react-native/cli.js",
            "BUNDLE_LOG": self.root / "logs/native-test-bundle.log",
            "POD_RECEIPT": self.root / "preparation/receipt.json",
            "BUILD_LOCK": self.root / "locks/build.lock",
            "XCODEGEN_LOG": self.root / "logs/xcodegen.log",
            "CONTRACT_LOG": self.root / "logs/platform-contract.log",
            "POD_LOG": self.root / "logs/pod-install.log",
            "HOST_LOG": self.root / "logs/native-test-build.log",
        }
        paths["FORMAL_PODSPEC"] = (
            paths["FORMAL_PACKAGE_ROOT"] / "BleHubReactNativeBleng.podspec"
        )
        for name, value in paths.items():
            self.stack.enter_context(patch.object(consumer, name, value))
        self.stack.enter_context(
            patch.object(consumer.platform, "system", return_value="Darwin")
        )
        self.stack.enter_context(
            patch.object(consumer.shutil, "which", side_effect=lambda name: name)
        )
        self.generator = self.stack.enter_context(
            patch.object(
                consumer.xcodegen_toolchain, "verify", return_value=Path("xcodegen")
            )
        )
        for path in (
            consumer.HOST_PROJECT,
            consumer.NATIVE_TEST_SOURCE,
            consumer.NATIVE_TEST_ENTRY,
            consumer.RN_CLI,
            host / "metro.config.js",
            host / "babel.config.js",
            consumer.FORMAL_PACKAGE_ROOT / "src/index.ts",
            ios / "Podfile",
            ios / "BleHubBlengHost/AppDelegate.swift",
            ios / "BleHubBlengHost/FormalReactNativeDelegate.h",
            ios / "BleHubBlengHost/FormalReactNativeDelegate.mm",
            ios / "BleHubBlengHost/BleHubBlengHost-Bridging-Header.h",
            ios / "BleHubBlengHostNativeTests/BlengHostReloadHarness.h",
            ios / "BleHubBlengHostNativeTests/BlengHostReloadHarness.mm",
            consumer.FORMAL_PODSPEC,
            consumer.SMOKE_ROOT / "LifecycleCompileSmoke.swift",
        ):
            self.put(path)
        for name in (
            "HybridBlengCentral.swift",
            *consumer.PLATFORM_SOURCES,
            *consumer.LIFECYCLE_SOURCES,
        ):
            self.put(consumer.FORMAL_PACKAGE_ROOT / "ios" / name)
        for relative in consumer.STAGED_FILES:
            self.put(consumer.RN_STAGED_ROOT / relative)
        self.put(consumer.SPEC, SPEC_SOURCE.read_text(encoding="utf-8"))
        self.spec: Any = {
            "dependencies": {"BleHubApple": [], "BleHubUniFfi": [], "NitroModules": []},
            "source_files": [
                "ios/HybridBlengCentral.swift",
                "ios/BlengEngineLifecycle.h",
                "ios/BlengEngineLifecycle.mm",
            ],
        }
        self.installed_podspec = (
            ios / "Pods/Local Podspecs/BleHubReactNativeBleng.podspec.json"
        )
        self.put(self.installed_podspec, json.dumps(self.spec))
        self.preparation = self.stack.enter_context(
            patch.object(consumer, "_prepare_pods", side_effect=self.prepare_pods)
        )
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.fail: str | None = None
        self.timeout: str | None = None
        self.omit: str | None = None
        self.run = self.stack.enter_context(
            patch.object(consumer.subprocess, "run", side_effect=self.subprocess_run)
        )

    @staticmethod
    def put(path: Path, text: str = "fixture\n") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def prepare_pods(
        self,
        *,
        refresh_pods: bool,
        environment: dict[str, str],
        allow_podfile_change: bool = False,
    ) -> None:
        if self.omit != "workspace":
            consumer.WORKSPACE.mkdir(parents=True, exist_ok=True)

    def subprocess_run(
        self, command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        output = kwargs["stdout"]
        output.write("build output\n")
        if self.timeout and self.timeout in command:
            output.write("exact timeout diagnostic\n")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if self.fail and self.fail in command:
            output.write("exact compiler diagnostic\n")
            return subprocess.CompletedProcess(command, 42)
        if "generate" in command and self.omit != "project":
            self.put(consumer.PROJECT / "project.pbxproj")
        if "build" in command:
            if self.omit != "product":
                self.put(consumer.PRODUCT)
            if self.omit != "swiftmodule":
                consumer.SWIFT_MODULE.mkdir(parents=True, exist_ok=True)

        if "bundle" in command and self.omit != "bundle":
            self.put(
                consumer.NATIVE_TEST_BUNDLE,
                "" if self.omit == "empty-bundle" else "bundled JS\n",
            )
        if "build-for-testing" in command:
            products = (
                consumer.HOST_DERIVED_DATA / "Build/Products/Debug-iphonesimulator"
            )
            if self.omit != "host-app":
                self.put(products / "BleHubBlengHost.app/BleHubBlengHost")
            if self.omit != "native-test":
                self.put(
                    products
                    / "BleHubBlengHostNativeTests.xctest/BleHubBlengHostNativeTests"
                )
            if self.omit != "resource":
                resource = (
                    products
                    / "BleHubBlengHostNativeTests.xctest"
                    / consumer.NATIVE_TEST_BUNDLE.name
                )
                self.put(
                    resource,
                    "" if self.omit == "empty-resource" else "bundled JS\n",
                )
                if self.omit == "mismatched-resource":
                    self.put(resource, "bundled XX\n")
        return subprocess.CompletedProcess(command, 0)

    def test_admission_rejects_before_any_subprocess_or_generation(self) -> None:
        with (
            patch.object(consumer.platform, "system", return_value="Linux"),
            self.assertRaisesRegex(consumer.BlengIOSConsumerError, "macOS"),
        ):
            consumer.build()
        for missing, message in (
            (
                consumer.RN_STAGED_ROOT
                / "BleHubUniFfi/Sources/BleHubUniFfi/bleng_supervisor.swift",
                "stage-apple-bleng",
            ),
            (
                consumer.RN_STAGED_ROOT
                / "BleHubUniFfi/BleHubUniFfiNative.xcframework/ios-arm64_x86_64-simulator/Headers/bleng_supervisorFFI.h",
                "stage-apple-bleng",
            ),
            (consumer.FORMAL_PODSPEC, "formal-host-install"),
            (
                consumer.FORMAL_PACKAGE_ROOT / "ios/ApplePlatformSession.swift",
                "formal-host-install",
            ),
            (
                consumer.FORMAL_PACKAGE_ROOT / "ios/HybridBlengCentral.swift",
                "formal-host-install",
            ),
            (
                consumer.FORMAL_PACKAGE_ROOT / "ios/BlengEngineLifecycle.h",
                "formal-host-install",
            ),
            (
                consumer.FORMAL_PACKAGE_ROOT / "ios/BlengEngineLifecycle.mm",
                "formal-host-install",
            ),
            (consumer.NATIVE_TEST_SOURCE, "native contract"),
            (consumer.NATIVE_TEST_ENTRY, "native contract"),
            (consumer.RN_CLI, "formal-host-install"),
            (consumer.FORMAL_PACKAGE_ROOT / "src/index.ts", "formal-host-install"),
            (consumer.HOST_ROOT / "metro.config.js", "native contract"),
            (consumer.HOST_ROOT / "babel.config.js", "native contract"),
        ):
            with self.subTest(missing=missing):
                contents = missing.read_text(encoding="utf-8")
                missing.unlink()
                try:
                    with self.assertRaisesRegex(
                        consumer.BlengIOSConsumerError, message
                    ):
                        consumer.build()
                finally:
                    self.put(missing, contents)
        self.run.assert_not_called()
        self.generator.assert_not_called()
        self.assertFalse(consumer.WORK_ROOT.exists())

    def test_shared_host_and_reload_harness_inputs_are_admitted_before_any_work(
        self,
    ) -> None:
        for relative in (
            "BleHubBlengHost/AppDelegate.swift",
            "BleHubBlengHost/FormalReactNativeDelegate.h",
            "BleHubBlengHost/FormalReactNativeDelegate.mm",
            "BleHubBlengHost/BleHubBlengHost-Bridging-Header.h",
            "BleHubBlengHostNativeTests/BlengHostReloadHarness.h",
            "BleHubBlengHostNativeTests/BlengHostReloadHarness.mm",
        ):
            with self.subTest(missing=relative):
                missing = consumer.IOS_ROOT / relative
                contents = missing.read_text(encoding="utf-8")
                missing.unlink()
                try:
                    with self.assertRaisesRegex(
                        consumer.BlengIOSConsumerError, "native contract inputs"
                    ) as caught:
                        consumer.build()
                    self.assertIn(str(missing), str(caught.exception))
                    self.run.assert_not_called()
                    self.generator.assert_not_called()
                    self.assertFalse(consumer.WORK_ROOT.exists())
                finally:
                    self.put(missing, contents)

    def test_app_and_reload_harness_share_the_formal_delegate(self) -> None:
        ios = SPEC_SOURCE.parent.parent
        app = (ios / "BleHubBlengHost/AppDelegate.swift").read_text(encoding="utf-8")
        harness = (
            ios / "BleHubBlengHostNativeTests/BlengHostReloadHarness.mm"
        ).read_text(encoding="utf-8")
        shared = (ios / "BleHubBlengHost/FormalReactNativeDelegate.mm").read_text(
            encoding="utf-8"
        )
        self.assertRegex(app, r"\bdelegate\s*=\s*FormalReactNativeDelegate\s*\(")
        self.assertRegex(
            harness,
            r"@interface\s+BlengHostReloadDelegate\s*:\s*FormalReactNativeDelegate\b",
        )
        self.assertIn("configurePredecessor:", shared)
        self.assertNotIn("configurePredecessor", harness)

    def test_missing_node_is_rejected_before_any_subprocess_or_generation(self) -> None:
        with (
            patch.object(
                consumer.shutil,
                "which",
                side_effect=lambda name: None if name == "node" else name,
            ),
            self.assertRaisesRegex(consumer.BlengIOSConsumerError, "executable: node"),
        ):
            consumer.build()
        self.run.assert_not_called()
        self.generator.assert_not_called()
        self.assertFalse(consumer.WORK_ROOT.exists())

    def test_invalid_installed_podspec_stops_before_generation_pod_install_or_build(
        self,
    ) -> None:
        valid = self.spec
        for invalid, message in (
            ("not JSON", "JSON"),
            ([], "object"),
            ({**valid, "dependencies": {"NitroModules": []}}, "dependencies"),
            (
                {**valid, "dependencies": {"BleHubApple": [], "BleHubUniFfi": []}},
                "dependencies",
            ),
            ({**valid, "source_files": ["ios/Other.swift"]}, "HybridBlengCentral"),
        ):
            with self.subTest(invalid=invalid):
                self.calls.clear()
                self.put(
                    self.installed_podspec,
                    invalid if isinstance(invalid, str) else json.dumps(invalid),
                )
                with self.assertRaisesRegex(consumer.BlengIOSConsumerError, message):
                    consumer.build()
                self.assertEqual(self.calls, [])
                self.generator.assert_not_called()

    def test_installed_podspec_is_read_directly_without_ruby_or_pod_ipc(self) -> None:
        consumer.validate_formal_podspec()
        self.run.assert_not_called()
        self.preparation.assert_not_called()
        self.generator.assert_not_called()

    def test_missing_unreadable_or_oversized_installed_podspec_fails_closed(
        self,
    ) -> None:
        for invalid in (None, b"\xff", b" " * (17 * 1024 * 1024)):
            with self.subTest(invalid="missing" if invalid is None else len(invalid)):
                self.installed_podspec.unlink(missing_ok=True)
                if invalid is not None:
                    self.installed_podspec.write_bytes(invalid)
                with self.assertRaises(consumer.BlengIOSConsumerError):
                    consumer.validate_formal_podspec()
                self.run.assert_not_called()
                self.generator.assert_not_called()

    def test_preparation_precedes_installed_podspec_validation_and_generation(
        self,
    ) -> None:
        self.installed_podspec.unlink()

        def prepare(
            *,
            refresh_pods: bool,
            environment: dict[str, str],
            allow_podfile_change: bool = False,
        ) -> None:
            self.assertTrue(refresh_pods)
            self.assertEqual(self.calls, [])
            self.generator.assert_not_called()
            self.put(self.installed_podspec, json.dumps(self.spec))
            self.prepare_pods(refresh_pods=refresh_pods, environment=environment)

        self.preparation.side_effect = prepare
        consumer.build(refresh_pods=True)
        self.preparation.assert_called_once()
        self.assertEqual(len(self.calls), 4)

    def test_preparation_failure_never_validates_generates_or_builds(self) -> None:
        self.preparation.side_effect = consumer.BlengIOSConsumerError("stale receipt")
        with (
            patch.object(consumer, "validate_formal_podspec") as validate,
            self.assertRaisesRegex(consumer.BlengIOSConsumerError, "stale receipt"),
        ):
            consumer.build()
        validate.assert_not_called()
        self.run.assert_not_called()
        self.generator.assert_not_called()
        self.assertFalse(consumer.WORK_ROOT.exists())

    def test_build_holds_lock_through_success_and_failure(self) -> None:
        for refresh_pods in (False, True):
            for failure in (False, True):
                with self.subTest(refresh_pods=refresh_pods, failure=failure):
                    events: list[str] = []

                    def run_build(
                        *,
                        refresh_pods: bool,
                        allow_podfile_change: bool = False,
                        locked_events: list[str] = events,
                        build_failure: bool = failure,
                    ) -> None:
                        # The host never invents a Podfile change: it is always explicit.
                        self.assertFalse(allow_podfile_change)
                        self.assertEqual(locked_events, ["enter"])
                        if build_failure:
                            raise consumer.BlengIOSConsumerError("build failed")

                    with (
                        patch.object(consumer, "_build_lock") as lock,
                        patch.object(
                            consumer, "_build", side_effect=run_build
                        ) as build,
                    ):
                        lock.return_value.__enter__.side_effect = lambda events=events: (
                            events.append("enter")
                        )
                        lock.return_value.__exit__.side_effect = (
                            lambda *args, events=events: events.append("exit")
                        )
                        if failure:
                            with self.assertRaisesRegex(
                                consumer.BlengIOSConsumerError, "build failed"
                            ):
                                consumer.build(refresh_pods=refresh_pods)
                        else:
                            consumer.build(refresh_pods=refresh_pods)
                        lock.assert_called_once_with()
                        build.assert_called_once_with(
                            refresh_pods=refresh_pods, allow_podfile_change=False
                        )
                        self.assertEqual(events, ["enter", "exit"])
                        self.assertEqual(
                            lock.return_value.__exit__.call_args.args[0],
                            consumer.BlengIOSConsumerError if failure else None,
                        )

    def test_main_defaults_to_verify_and_refresh_is_explicit(self) -> None:
        for arguments, refresh_pods in (([], False), (["--refresh-pods"], True)):
            with (
                self.subTest(arguments=arguments),
                patch.object(consumer, "build") as build,
            ):
                self.assertEqual(consumer.main(arguments), 0)
                build.assert_called_once_with(
                    refresh_pods=refresh_pods, allow_podfile_change=False
                )

    def test_main_rejects_unknown_or_valued_refresh_flag_without_build(self) -> None:
        for arguments in (["--unknown"], ["--refresh-pods", "false"], ["true"]):
            with (
                self.subTest(arguments=arguments),
                patch.object(consumer, "build") as build,
                patch("sys.stderr"),
                self.assertRaises(SystemExit) as caught,
            ):
                consumer.main(arguments)
            self.assertEqual(caught.exception.code, 2)
            build.assert_not_called()

    def test_podspec_requires_exact_owner_and_lifecycle_source_entries(self) -> None:
        valid = self.spec
        for required in (
            "ios/HybridBlengCentral.swift",
            "ios/BlengEngineLifecycle.h",
            "ios/BlengEngineLifecycle.mm",
        ):
            for replacement in (
                None,
                Path(required).name,
                f"other/{Path(required).name}",
                "ios/*",
            ):
                with self.subTest(required=required, replacement=replacement):
                    sources = [
                        path for path in valid["source_files"] if path != required
                    ]
                    if replacement is not None:
                        sources.append(replacement)
                    self.put(
                        self.installed_podspec,
                        json.dumps({**valid, "source_files": sources}),
                    )
                    self.calls.clear()
                    with self.assertRaisesRegex(
                        consumer.BlengIOSConsumerError, required
                    ):
                        consumer.build()
                    self.assertEqual(self.calls, [])
                    self.generator.assert_not_called()
                    self.assertFalse(consumer.WORK_ROOT.exists())
        self.spec = valid

    def test_build_is_isolated_swift_then_existing_formal_workspace_native_only(
        self,
    ) -> None:
        # Existing generated state must survive; the owner never cleans or retries.
        for path in (
            consumer.IOS_ROOT / "Pods/keep",
            consumer.HOST_DERIVED_DATA / "keep",
        ):
            self.put(path)
        previous_env = dict(os.environ)
        consumer.build()
        self.assertEqual(dict(os.environ), previous_env)
        commands = [command for command, _ in self.calls]
        self.assertEqual(len(commands), 4)
        self.assertEqual(commands[0][1:3], ["generate", "--spec"])
        self.assertEqual(commands[1][-1], "build")
        self.assertEqual(
            commands[2],
            [
                "node",
                str(consumer.RN_CLI),
                "bundle",
                "--config",
                "metro.config.js",
                "--platform",
                "ios",
                "--dev",
                "false",
                "--minify",
                "false",
                "--max-workers",
                "1",
                "--entry-file",
                "ios/BleHubBlengHostNativeTests/BlengHostReload.js",
                "--bundle-output",
                "ios/build/native-test-resources/BlengHostReload.jsbundle",
                "--assets-dest",
                "ios/build/native-test-resources",
            ],
        )
        self.assertEqual(self.calls[2][1]["cwd"], consumer.HOST_ROOT)
        self.assertEqual(self.calls[2][1]["timeout"], 180)
        self.assertEqual(self.calls[2][1]["stdout"].name, str(consumer.BUNDLE_LOG))
        self.assertEqual(commands[3][-1], "build-for-testing")
        direct, host = commands[1], commands[3]
        self.assertEqual(direct[direct.index("-project") + 1], str(consumer.PROJECT))
        self.assertNotIn("-workspace", direct)
        self.assertEqual(host[host.index("-workspace") + 1], str(consumer.WORKSPACE))
        self.assertNotIn("-project", host)
        self.assertIn("-only-testing:BleHubBlengHostNativeTests", host)
        self.assertEqual(host[host.index("-scheme") + 1], "BleHubBlengHost")
        self.assertEqual(
            host[host.index("-derivedDataPath") + 1], str(consumer.HOST_DERIVED_DATA)
        )
        for command in (direct, host):
            self.assertIn("generic/platform=iOS Simulator", command)
            self.assertIn("iphonesimulator", command)
            self.assertIn("Debug", command)
            self.assertIn("CODE_SIGNING_ALLOWED=NO", command)
            self.assertIn("CODE_SIGNING_REQUIRED=NO", command)
        for command, kwargs in self.calls:
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertGreater(kwargs["timeout"], 0)
            self.assertFalse(
                {
                    "devicectl",
                    "simctl",
                    "launch",
                    "test",
                    "test-without-building",
                    "clean",
                    "npm",
                    "npx",
                    "start",
                    "run-ios",
                    "cargo",
                    "pod",
                    "ruby",
                    "ipc",
                    "install",
                }
                & set(command)
            )
        self.preparation.assert_called_once()
        self.assertFalse(self.preparation.call_args.kwargs["refresh_pods"])
        self.assertEqual(
            self.preparation.call_args.kwargs["environment"],
            dict(
                previous_env,
                BLEHUB_APPLE_BLENG_STAGING_ROOT=str(consumer.RN_STAGED_ROOT),
                RCT_USE_RN_DEP="1",
                RCT_USE_PREBUILT_RNCORE="1",
            ),
        )
        self.assertEqual(
            self.calls[3][1]["env"],
            self.preparation.call_args.kwargs["environment"],
        )
        self.assertTrue((consumer.IOS_ROOT / "Pods/keep").is_file())
        self.assertTrue((consumer.HOST_DERIVED_DATA / "keep").is_file())
        rendered = consumer.RENDERED_SPEC.read_text(encoding="utf-8")
        self.assertNotIn("__BLEHUB_", rendered)
        for path in (
            consumer.RN_STAGED_ROOT,
            consumer.FORMAL_PACKAGE_ROOT,
            consumer.SMOKE_ROOT,
        ):
            self.assertIn(str(path), rendered)
        self.assertIn("type: library.static", rendered)
        self.assertIn("package: BleHubApple", rendered)
        self.assertNotIn("package: BleHubUniFfi", rendered)
        for forbidden in (
            "Nitro",
            "React",
            "integrations/",
            "apple/bleng-rn-ios",
            "type: application",
        ):
            self.assertNotIn(forbidden, rendered)
        for name in consumer.PLATFORM_SOURCES:
            self.assertIn(name, rendered)
        for name in ("HybridBlengCentral.swift", *consumer.LIFECYCLE_SOURCES):
            self.assertNotIn(name, rendered)
        self.assertFalse(
            (consumer.SPEC.parent / "BlengPlatformContract.xcodeproj").exists()
        )

    def test_missing_render_token_or_build_output_stops_subsequent_steps(self) -> None:
        template = consumer.SPEC.read_text(encoding="utf-8")
        for token in (
            consumer.STAGING_TOKEN,
            consumer.PACKAGE_TOKEN,
            consumer.SMOKE_TOKEN,
        ):
            self.put(consumer.SPEC, template.replace(token, "missing"))
            with self.assertRaisesRegex(consumer.BlengIOSConsumerError, "token"):
                consumer.build()
            self.assertEqual(self.calls, [])
        self.put(consumer.SPEC, template)
        for omitted, message in (
            ("project", "XcodeGen"),
            ("product", "static product"),
            ("swiftmodule", "Swift module"),
            ("workspace", "workspace"),
        ):
            with self.subTest(omitted=omitted):
                self.omit = omitted
                self.calls.clear()
                (consumer.PROJECT / "project.pbxproj").unlink(missing_ok=True)
                consumer.PRODUCT.unlink(missing_ok=True)
                for directory in (consumer.SWIFT_MODULE, consumer.WORKSPACE):
                    if directory.exists():
                        directory.rmdir()
                with self.assertRaisesRegex(consumer.BlengIOSConsumerError, message):
                    consumer.build()
                self.assertFalse(
                    any("build-for-testing" in command for command, _ in self.calls)
                )

    def test_bundle_failure_and_timeout_keep_logs_and_never_build_native_tests(
        self,
    ) -> None:
        for timed_out in (False, True):
            with self.subTest(timed_out=timed_out):
                self.calls.clear()
                self.fail = None if timed_out else "bundle"
                self.timeout = "bundle" if timed_out else None
                with self.assertRaises(consumer.BlengIOSConsumerError) as caught:
                    consumer.build()
                self.assertEqual(len(self.calls), 3)
                self.assertIn("bundle", self.calls[-1][0])
                self.assertEqual(self.calls[-1][1]["timeout"], 180)
                self.assertFalse(
                    any("build-for-testing" in command for command, _ in self.calls)
                )
                message = str(caught.exception)
                self.assertIn(str(consumer.BUNDLE_LOG), message)
                self.assertIn(
                    "timed out after 180s" if timed_out else "exit code 42", message
                )
                self.assertIn(
                    "exact timeout diagnostic"
                    if timed_out
                    else "exact compiler diagnostic",
                    consumer.BUNDLE_LOG.read_text(encoding="utf-8"),
                )

    def test_successful_bundle_exit_requires_fresh_nonempty_output(self) -> None:
        for omitted in ("bundle", "empty-bundle"):
            with self.subTest(omitted=omitted):
                self.calls.clear()
                self.omit = omitted
                self.put(consumer.NATIVE_TEST_BUNDLE, "stale bundle\n")
                with self.assertRaisesRegex(
                    consumer.BlengIOSConsumerError,
                    "missing or empty native-test JS bundle",
                ):
                    consumer.build()
                self.assertEqual(len(self.calls), 3)
                self.assertFalse(
                    any("build-for-testing" in command for command, _ in self.calls)
                )

    def test_copied_native_test_resource_is_required_nonempty_and_hash_matched(
        self,
    ) -> None:
        resource = (
            consumer.HOST_DERIVED_DATA
            / "Build/Products/Debug-iphonesimulator/BleHubBlengHostNativeTests.xctest"
            / consumer.NATIVE_TEST_BUNDLE.name
        )
        for omitted, message in (
            ("resource", "missing or empty bundled native-test JS resource"),
            ("empty-resource", "missing or empty bundled native-test JS resource"),
            ("mismatched-resource", "native-test JS resource SHA256 mismatch"),
        ):
            with self.subTest(omitted=omitted):
                self.calls.clear()
                self.omit = omitted
                resource.unlink(missing_ok=True)
                with self.assertRaisesRegex(consumer.BlengIOSConsumerError, message):
                    consumer.build()
                self.assertEqual(len(self.calls), 4)
                self.assertEqual(self.calls[-1][0][-1], "build-for-testing")

    def test_successful_xcode_exit_without_host_or_test_binary_is_rejected(
        self,
    ) -> None:
        products = consumer.HOST_DERIVED_DATA / "Build/Products/Debug-iphonesimulator"
        for omitted, path in (
            ("host-app", products / "BleHubBlengHost.app/BleHubBlengHost"),
            (
                "native-test",
                products
                / "BleHubBlengHostNativeTests.xctest/BleHubBlengHostNativeTests",
            ),
        ):
            with self.subTest(omitted=omitted):
                self.omit = omitted
                path.unlink(missing_ok=True)
                with self.assertRaisesRegex(
                    consumer.BlengIOSConsumerError, "host/test executable"
                ):
                    consumer.build()

    def test_failure_and_timeout_keep_full_logs_and_detail_without_retry(self) -> None:
        log = self.root / "logs/failure.log"
        for timed_out in (False, True):

            def fail(
                command: list[str], *, timeout_failure: bool = timed_out, **kwargs: Any
            ) -> subprocess.CompletedProcess[str]:
                kwargs["stdout"].write(
                    "\n".join(f"compiler line {n}" for n in range(100))
                    + "\nexact final diagnostic\n"
                )
                if timeout_failure:
                    raise subprocess.TimeoutExpired(command, kwargs["timeout"])
                return subprocess.CompletedProcess(command, 23)

            with patch.object(consumer.subprocess, "run", side_effect=fail) as run:
                with self.assertRaises(consumer.BlengIOSConsumerError) as caught:
                    consumer._run(["xcodebuild"], cwd=self.root, log=log, timeout=7)
                run.assert_called_once()
            message = str(caught.exception)
            self.assertIn(str(log), message)
            self.assertIn("exact final diagnostic", message)
            self.assertIn("7" if timed_out else "exit code 23", message)
            self.assertNotIn("compiler line 0\n", message)
            self.assertIn("compiler line 0\n", log.read_text(encoding="utf-8"))

    def test_contract_compile_failure_never_reaches_bundle_or_native_build(
        self,
    ) -> None:
        self.fail = "build"
        with self.assertRaisesRegex(
            consumer.BlengIOSConsumerError, "exact compiler diagnostic"
        ):
            consumer.build()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1][0][-1], "build")
        self.assertFalse(
            {"install", "bundle", "build-for-testing"}
            & {part for command, _ in self.calls for part in command}
        )


class BlengIOSPodfileGuardTests(unittest.TestCase):
    ruby: str
    PODFILE = SPEC_SOURCE.parent.parent / "Podfile"
    RUBY_STUB = r"""
$react_native_configured = false
$events = []

module Pod
  module Executable
    def self.execute_command(command, arguments)
      raise "unexpected command" unless command == "node"
      $events << "resolve"
      "/stub/react_native_pods.rb"
    end
  end

  class Config
    def self.instance
      new
    end

    def installation_root
      "/stub/ios"
    end
  end
end

module ReactNativeDependenciesUtils
  def self.build_react_native_deps_from_source
    raise "dependencies checked before use_react_native!" unless $react_native_configured
    $events << "deps-check"
    ENV.fetch("STUB_DEPS_SOURCE") == "1"
  end
end

module ReactNativeCoreUtils
  def self.build_rncore_from_source
    raise "core checked before use_react_native!" unless $react_native_configured
    $events << "core-check"
    ENV.fetch("STUB_CORE_SOURCE") == "1"
  end
end

class StubPodfile
  def require(path)
    raise "unexpected require" unless path == "/stub/react_native_pods.rb"
    $events << "require-stub"
    false
  end

  def platform(name, version)
    raise "unexpected platform" unless name == :ios
  end

  def prepare_react_native_project!
  end

  def target(name)
    $events << name
    yield
  end

  def use_native_modules!
    { :reactNativePath => "/stub/react-native" }
  end

  def pod(name, **options)
  end

  def use_react_native!(**options)
    $react_native_configured = true
    $events << "use-react-native"
  end

  def post_install
    $events << "post-install"
    yield Object.new
  end

  def react_native_post_install(installer, path, **options)
    $events << "react-native-post-install"
  end

  def inherit!(mode)
    raise "unexpected inheritance" unless [:complete, :search_paths].include?(mode)
    $events << "inherit-#{mode}"
  end
end

path = ARGV.fetch(0)
StubPodfile.new.instance_eval(File.read(path), path, 1)
raise "RN setup not reached" unless $react_native_configured
raise "require not stubbed exactly once" unless $events.count("require-stub") == 1
raise "native test target not evaluated" unless $events.include?("BleHubBlengHostNativeTests")
raise "device test target not evaluated" unless $events.include?("BleHubBlengHostDeviceTests")
# A device test runs inside the App and must not link a second copy of the Nitro pod.
raise "device target must inherit search paths only" unless $events.include?("inherit-search_paths")
raise "post-install not evaluated" unless $events.include?("react-native-post-install")
puts "podfile-evaluated"
puts $events.join(",")
"""

    @classmethod
    def setUpClass(cls) -> None:
        ruby = shutil.which("ruby")
        if ruby is None:
            raise unittest.SkipTest(
                "Ruby is unavailable for the stubbed Podfile regression"
            )
        cls.ruby = ruby

    def evaluate_podfile(
        self,
        *,
        deps_mode: str,
        core_mode: str,
        deps_source: bool,
        core_source: bool,
    ) -> subprocess.CompletedProcess[str]:
        # A clean environment excludes Ruby startup hooks and developer Pod flags.
        return subprocess.run(
            [self.ruby, "--disable-gems", "-e", self.RUBY_STUB, str(self.PODFILE)],
            cwd=self.PODFILE.parent,
            env={
                "PATH": os.defpath,
                "RCT_USE_RN_DEP": deps_mode,
                "RCT_USE_PREBUILT_RNCORE": core_mode,
                "STUB_DEPS_SOURCE": "1" if deps_source else "0",
                "STUB_CORE_SOURCE": "1" if core_source else "0",
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def test_podfile_rejects_requested_prebuilt_dependencies_source_fallback(
        self,
    ) -> None:
        result = self.evaluate_podfile(
            deps_mode="1", core_mode="1", deps_source=True, core_source=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Requested prebuilt RN dependencies unavailable", result.stderr)
        self.assertIn("no source fallback", result.stderr)
        self.assertNotIn("podfile-evaluated", result.stdout)

    def test_podfile_rejects_requested_prebuilt_core_source_fallback(self) -> None:
        result = self.evaluate_podfile(
            deps_mode="1", core_mode="1", deps_source=False, core_source=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Requested prebuilt RN core unavailable", result.stderr)
        self.assertIn("no source fallback", result.stderr)
        self.assertNotIn("podfile-evaluated", result.stdout)

    def test_podfile_allows_explicit_source_modes_including_mixed_modes(self) -> None:
        for deps_mode, core_mode in (("0", "0"), ("0", "1"), ("1", "0")):
            with self.subTest(deps_mode=deps_mode, core_mode=core_mode):
                result = self.evaluate_podfile(
                    deps_mode=deps_mode,
                    core_mode=core_mode,
                    deps_source=deps_mode == "0",
                    core_source=core_mode == "0",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("podfile-evaluated", result.stdout)
                self.assertEqual("deps-check" in result.stdout, deps_mode == "1")
                self.assertEqual("core-check" in result.stdout, core_mode == "1")

    def test_podfile_allows_successful_requested_prebuilt_modes(self) -> None:
        result = self.evaluate_podfile(
            deps_mode="1", core_mode="1", deps_source=False, core_source=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("podfile-evaluated", result.stdout)
        self.assertIn("deps-check", result.stdout)
        self.assertIn("core-check", result.stdout)


class BlengIOSBuildLockTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lock = self.root / "owner/build.lock"
        patcher = patch.object(consumer, "BUILD_LOCK", self.lock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_child_lock_status(self, expected: int) -> None:
        probe = (
            "import fcntl, sys\n"
            "with open(sys.argv[1], 'a') as stream:\n"
            "    try:\n"
            "        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    except BlockingIOError:\n"
            "        sys.exit(23)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe, str(self.lock)],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        self.assertEqual(result.returncode, expected, result.stderr)

    def test_real_build_lock_rejects_contention_and_releases_after_success(
        self,
    ) -> None:
        with consumer._build_lock():
            self.assert_child_lock_status(23)
            inode = self.lock.stat().st_ino
            with (
                self.assertRaisesRegex(
                    consumer.BlengIOSConsumerError, "already active"
                ),
                consumer._build_lock(),
            ):
                self.fail("contending owner acquired the build lock")
            self.assert_child_lock_status(23)
        self.assert_child_lock_status(0)
        with consumer._build_lock():
            self.assertEqual(self.lock.stat().st_ino, inode)
            self.assert_child_lock_status(23)
        self.assert_child_lock_status(0)

    def test_real_build_lock_releases_after_failure_without_replacing_lock_file(
        self,
    ) -> None:
        self.lock.parent.mkdir(parents=True)
        self.lock.write_text("existing lock file\n", encoding="utf-8")
        inode = self.lock.stat().st_ino
        with (
            self.assertRaisesRegex(RuntimeError, "owned build failed"),
            consumer._build_lock(),
        ):
            self.assert_child_lock_status(23)
            raise RuntimeError("owned build failed")
        self.assert_child_lock_status(0)
        with consumer._build_lock():
            self.assertEqual(self.lock.stat().st_ino, inode)
            self.assert_child_lock_status(23)
        self.assert_child_lock_status(0)
        self.assertEqual(self.lock.read_text(encoding="utf-8"), "existing lock file\n")


class BlengIOSPodPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, path in {
            "HOST_ROOT": self.root / "formal host",
            "IOS_ROOT": self.root / "formal host/ios",
            "RN_STAGED_ROOT": self.root / "staged combined",
            "POD_RECEIPT": self.root / "preparation/receipt.json",
            "POD_LOG": self.root / "logs/pod-install.log",
        }.items():
            self.stack.enter_context(patch.object(consumer, name, path))
        self.environment = {"PATH": "/fixture/bin", "USE_HERMES": "1"}
        self.selected_environment = dict(self.environment, GEM_HOME="/fixture/gems")
        self.transport_environment = dict(
            self.selected_environment, CURL_HOME="/fixture/curl"
        )
        self.pod = str(self.root / "toolchain/pod")
        self.ruby = str(self.root / "toolchain/ruby")
        self.select_toolchain = consumer._pod_toolchain
        self.toolchain = self.stack.enter_context(
            patch.object(
                consumer,
                "_pod_toolchain",
                return_value=(self.pod, self.ruby, self.selected_environment),
            )
        )
        self.begin = self.stack.enter_context(
            patch.object(
                ios_pod_preparation, "begin_preparation", return_value={"before": True}
            )
        )
        self.seal = self.stack.enter_context(
            patch.object(ios_pod_preparation, "seal_preparation")
        )
        self.verify = self.stack.enter_context(
            patch.object(ios_pod_preparation, "verify_preparation")
        )
        self.curl = self.stack.enter_context(
            patch.object(ios_pod_refresh, "curl_environment")
        )
        self.curl.return_value.__enter__.return_value = self.transport_environment
        self.logged = self.stack.enter_context(
            patch.object(ios_pod_refresh, "run_logged")
        )
        self.snapshot = self.stack.enter_context(
            patch.object(
                ios_pod_refresh, "run_snapshot", return_value='{"snapshot": true}'
            )
        )
        self.run = self.stack.enter_context(
            patch.object(
                consumer.subprocess,
                "run",
                side_effect=AssertionError("unexpected subprocess"),
            )
        )

    def assert_preparation_context(self, kwargs: dict[str, Any]) -> None:
        self.assertEqual(kwargs["ios_root"], consumer.IOS_ROOT)
        self.assertEqual(kwargs["host_root"], consumer.HOST_ROOT)
        self.assertEqual(kwargs["staging_root"], consumer.RN_STAGED_ROOT)
        self.assertEqual(kwargs["environment"], self.selected_environment)
        callback = kwargs["run"]
        command = ["ruby", "helper.rb", "installed"]
        self.assertEqual(
            callback(command, cwd=consumer.IOS_ROOT, timeout=120),
            '{"snapshot": true}',
        )
        self.assertEqual(command, ["ruby", "helper.rb", "installed"])
        self.snapshot.assert_called_with(
            [self.ruby, "helper.rb", "installed"],
            cwd=consumer.IOS_ROOT,
            timeout=120,
            environment=self.selected_environment,
        )

    def test_direct_pod_shebang_selects_same_absolute_ruby_without_execution(
        self,
    ) -> None:
        pod = self.root / "bin/pod"
        ruby = self.root / "ruby/bin/ruby"
        ruby.parent.mkdir(parents=True)
        ruby.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        ruby.chmod(0o755)
        pod.parent.mkdir(parents=True)
        pod.write_text(f"#!{ruby}\n# fixture gem launcher\n", encoding="utf-8")
        pod.chmod(0o755)
        original = dict(self.environment)
        with patch.object(consumer.shutil, "which", return_value=str(pod)):
            selected_pod, selected_ruby, environment = self.select_toolchain(
                self.environment
            )
        self.assertEqual(Path(selected_pod), pod.resolve())
        self.assertEqual(Path(selected_ruby), ruby.resolve())
        self.assertEqual(environment, original)
        self.assertEqual(self.environment, original)
        self.run.assert_not_called()
        self.snapshot.assert_not_called()

    def test_env_ruby_shebang_resolves_ruby_using_effective_path_without_execution(
        self,
    ) -> None:
        pod = self.root / "bin/pod"
        ruby = self.root / "selected ruby/bin/ruby"
        pod.parent.mkdir(parents=True)
        pod.write_text(
            "#!/usr/bin/env ruby\n# fixture gem launcher\n", encoding="utf-8"
        )
        pod.chmod(0o755)
        ruby.parent.mkdir(parents=True)
        ruby.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        ruby.chmod(0o755)
        effective = dict(self.environment, PATH=str(ruby.parent))
        original = dict(effective)
        with patch.object(consumer, "_require_executable", return_value=str(pod)):
            selected_pod, selected_ruby, environment = self.select_toolchain(effective)
        self.assertEqual(Path(selected_pod), pod.resolve())
        self.assertEqual(Path(selected_ruby), ruby.resolve())
        self.assertEqual(environment, original)
        self.assertEqual(effective, original)
        self.run.assert_not_called()
        self.snapshot.assert_not_called()

    def test_exact_homebrew_wrapper_selects_inner_ruby_and_gem_home_without_execution(
        self,
    ) -> None:
        pod = self.root / "homebrew/bin/pod"
        gem_home = self.root / "homebrew Cellar/cocoapods/1.16.2/libexec"
        script = gem_home / "bin/pod"
        ruby = self.root / "homebrew/opt/ruby/bin/ruby"
        for path in (pod, script, ruby):
            path.parent.mkdir(parents=True, exist_ok=True)
        ruby.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        script.write_text(f"#!{ruby}\n# fixture gem launcher\n", encoding="utf-8")
        pod.write_text(
            f'#!/bin/bash\nGEM_HOME="{gem_home}" exec "{script}"  "$@"\n',
            encoding="utf-8",
        )
        for path in (pod, script, ruby):
            path.chmod(0o755)
        effective = dict(
            self.environment, GEM_HOME="/prior/gems", GEM_PATH="/prior/gem-path"
        )
        original = dict(effective)
        with patch.object(consumer, "_require_executable", return_value=str(pod)):
            selected_pod, selected_ruby, environment = self.select_toolchain(effective)
        self.assertEqual(Path(selected_pod), pod.resolve())
        self.assertEqual(Path(selected_ruby), ruby.resolve())
        self.assertEqual(environment, dict(original, GEM_HOME=str(gem_home)))
        self.assertEqual(effective, original)
        self.run.assert_not_called()
        self.snapshot.assert_not_called()

    def test_unsupported_or_missing_pod_ruby_is_rejected_without_execution(
        self,
    ) -> None:
        pod = self.root / "bin/pod"
        pod.parent.mkdir(parents=True)
        for launcher in (
            "#!/usr/bin/env -S ruby\n",
            f"#!{self.root / 'missing/bin/ruby'}\n",
            "#!/bin/sh\nexec arbitrary-wrapper\n",
            '#!/bin/bash\nGEM_HOME="/some/gems" exec "/some/pod" "$@"\n',
            '#!/bin/bash\nGEM_HOME="/some/gems" exec "/some/pod"  "$@"\necho extra\n',
        ):
            with self.subTest(launcher=launcher):
                pod.write_text(launcher, encoding="utf-8")
                pod.chmod(0o755)
                with (
                    patch.object(consumer.shutil, "which", return_value=str(pod)),
                    self.assertRaises(consumer.BlengIOSConsumerError),
                ):
                    self.select_toolchain(self.environment)
                self.run.assert_not_called()
                self.snapshot.assert_not_called()

    def test_ordinary_preparation_verifies_only_and_uses_selected_ruby_for_snapshot(
        self,
    ) -> None:
        original = dict(self.environment)
        consumer._prepare_pods(refresh_pods=False, environment=self.environment)
        self.toolchain.assert_called_once_with(self.environment)
        self.verify.assert_called_once()
        self.assertEqual(self.verify.call_args.args, (consumer.POD_RECEIPT,))
        self.assert_preparation_context(self.verify.call_args.kwargs)
        self.begin.assert_not_called()
        self.curl.assert_not_called()
        self.logged.assert_not_called()
        self.seal.assert_not_called()
        self.run.assert_not_called()
        self.assertEqual(self.environment, original)
        self.assertFalse(consumer.POD_LOG.parent.exists())

    def test_explicit_refresh_orders_invalidation_install_and_seal_with_unique_logs(
        self,
    ) -> None:
        events: list[str] = []
        self.begin.side_effect = lambda *args, **kwargs: (
            events.append("begin") or {"before": True}
        )
        self.curl.return_value.__enter__.side_effect = lambda: (
            events.append("enter") or self.transport_environment
        )
        self.curl.return_value.__exit__.side_effect = lambda *args: events.append(
            "exit"
        )
        logs: list[Path] = []

        def install(command: list[str], **kwargs: Any) -> None:
            events.append("install")
            self.assertEqual(
                command,
                [
                    self.pod,
                    "install",
                    "--no-repo-update",
                    "--clean-install",
                    "--deployment",
                ],
            )
            self.assertEqual(kwargs["cwd"], consumer.IOS_ROOT)
            self.assertEqual(kwargs["environment"], self.transport_environment)
            self.assertGreater(kwargs["timeout"], 0)
            log = kwargs["log_path"]
            self.assertEqual(log.parent.parent, consumer.POD_LOG.parent)
            self.assertTrue(log.parent.is_dir())
            self.assertEqual(log.parent.stat().st_mode & 0o777, 0o700)
            self.assertNotIn(log.parent, [previous.parent for previous in logs])
            self.assertEqual(log.name, consumer.POD_LOG.name)
            self.assertNotEqual(log, consumer.POD_LOG)
            self.assertNotIn(log, logs)
            self.assertFalse(log.exists())
            self.assertEqual(list(log.parent.iterdir()), [])
            log.write_text("refresh output\n", encoding="utf-8")
            logs.append(log)

        self.logged.side_effect = install
        self.seal.side_effect = lambda *args, **kwargs: events.append("seal")
        consumer.POD_LOG.parent.mkdir(parents=True)
        consumer.POD_LOG.write_text("previous log\n", encoding="utf-8")
        for _ in range(2):
            events.clear()
            consumer._prepare_pods(refresh_pods=True, environment=self.environment)
            self.assertEqual(events[0], "begin")
            self.assertLess(events.index("begin"), events.index("enter"))
            self.assertLess(events.index("enter"), events.index("install"))
            self.assertLess(events.index("install"), events.index("exit"))
            self.assertLess(events.index("install"), events.index("seal"))
            self.assertEqual(events.count("seal"), 1)
            self.assertEqual(self.begin.call_args.args, (consumer.POD_RECEIPT,))
            self.assert_preparation_context(self.begin.call_args.kwargs)
            self.assertEqual(self.seal.call_args.args, (consumer.POD_RECEIPT,))
            self.assertEqual(self.seal.call_args.kwargs["before"], {"before": True})
            self.assertIs(self.seal.call_args.kwargs["refresh_succeeded"], True)
            self.assert_preparation_context(self.seal.call_args.kwargs)
            self.curl.assert_called_with(self.selected_environment)
        self.assertEqual(len(logs), 2)
        self.assertEqual(consumer.POD_LOG.read_text(encoding="utf-8"), "previous log\n")
        for log in logs:
            self.assertEqual(log.read_text(encoding="utf-8"), "refresh output\n")
        self.verify.assert_not_called()
        self.run.assert_not_called()

    def test_stale_or_missing_receipt_never_falls_back_to_refresh(self) -> None:
        for reason in ("missing receipt", "stale preparation"):
            with self.subTest(reason=reason):
                self.verify.side_effect = ios_pod_preparation.PodPreparationError(
                    reason
                )
                with self.assertRaises(consumer.BlengIOSConsumerError):
                    consumer._prepare_pods(
                        refresh_pods=False, environment=self.environment
                    )
                self.begin.assert_not_called()
                self.curl.assert_not_called()
                self.logged.assert_not_called()
                self.seal.assert_not_called()
                self.run.assert_not_called()

    def test_grouped_refresh_failure_preserves_deadline_cleanup_and_cause_without_sealing(
        self,
    ) -> None:
        deadline = ios_pod_refresh.PodRefreshError("refresh process deadline exceeded")
        cleanup = ios_pod_refresh.PodRefreshError("owned refresh group cleanup failed")
        failure = ExceptionGroup("refresh and cleanup failed", [deadline, cleanup])
        self.logged.side_effect = failure

        with self.assertRaises(consumer.BlengIOSConsumerError) as caught:
            consumer._prepare_pods(refresh_pods=True, environment=self.environment)

        self.assertIn(str(deadline), str(caught.exception))
        self.assertIn(str(cleanup), str(caught.exception))
        self.assertIs(caught.exception.__cause__, failure)
        self.begin.assert_called_once()
        self.logged.assert_called_once()
        self.curl.return_value.__exit__.assert_called_once()
        self.assertIs(self.curl.return_value.__exit__.call_args.args[1], failure)
        self.seal.assert_not_called()
        self.verify.assert_not_called()
        self.run.assert_not_called()

    def test_grouped_keyboard_interrupt_propagates_without_sealing(self) -> None:
        interrupted = KeyboardInterrupt("refresh interrupted")
        cleanup = ios_pod_refresh.PodRefreshError("owned refresh group cleanup failed")
        failure = BaseExceptionGroup(
            "refresh and cleanup failed", [interrupted, cleanup]
        )
        self.logged.side_effect = failure

        with self.assertRaises(BaseExceptionGroup) as caught:
            consumer._prepare_pods(refresh_pods=True, environment=self.environment)

        self.assertIs(caught.exception, failure)
        self.assertIs(caught.exception.exceptions[0], interrupted)
        self.assertIs(caught.exception.exceptions[1], cleanup)
        self.begin.assert_called_once()
        self.logged.assert_called_once()
        self.curl.return_value.__exit__.assert_called_once()
        self.assertIs(self.curl.return_value.__exit__.call_args.args[1], failure)
        self.seal.assert_not_called()
        self.verify.assert_not_called()
        self.run.assert_not_called()

    def test_failed_refresh_does_not_seal_retry_or_adopt_existing_preparation(
        self,
    ) -> None:
        for step in ("begin", "transport", "install", "seal"):
            with self.subTest(step=step):
                for mocked in (
                    self.begin,
                    self.curl,
                    self.logged,
                    self.seal,
                    self.verify,
                ):
                    mocked.reset_mock(side_effect=True)
                self.curl.return_value.__enter__.side_effect = None
                failure = ios_pod_preparation.PodPreparationError("refresh failed")
                if step == "begin":
                    self.begin.side_effect = failure
                elif step == "transport":
                    self.curl.return_value.__enter__.side_effect = (
                        ios_pod_refresh.PodRefreshError("transport failed")
                    )
                elif step == "install":
                    self.logged.side_effect = ios_pod_refresh.PodRefreshError(
                        "install failed"
                    )
                else:
                    self.seal.side_effect = failure
                with self.assertRaises(consumer.BlengIOSConsumerError):
                    consumer._prepare_pods(
                        refresh_pods=True, environment=self.environment
                    )
                self.begin.assert_called_once()
                self.verify.assert_not_called()
                self.run.assert_not_called()
                if step in ("begin", "transport"):
                    self.logged.assert_not_called()
                else:
                    self.logged.assert_called_once()
                    self.curl.return_value.__exit__.assert_called_once()
                if step != "seal":
                    self.seal.assert_not_called()
                else:
                    self.seal.assert_called_once()


class DeviceSigningBundleIdTests(unittest.TestCase):
    """A device build names itself from the profile it signs with, not from a convention.

    A provisioning profile authorises a pattern. Treating `<team>.*` as if it were an
    explicit id (or an explicit id as if it needed a suffix) produces a bundle the profile
    refuses, and the failure would surface at install time instead of here.
    """

    def test_a_team_wildcard_is_extended_with_the_apps_own_suffix(self) -> None:
        self.assertEqual(
            consumer.signed_bundle_id({"application_identifier": "FWVYQ287W5.*"}),
            f"FWVYQ287W5.{consumer.DEVICE_BUNDLE_SUFFIX}",
        )

    def test_an_explicit_identifier_is_used_exactly(self) -> None:
        self.assertEqual(
            consumer.signed_bundle_id(
                {"application_identifier": "dev.blehub.bleng-rn-host"}
            ),
            "dev.blehub.bleng-rn-host",
        )

    def test_a_nested_wildcard_is_also_extended(self) -> None:
        self.assertEqual(
            consumer.signed_bundle_id({"application_identifier": "dev.blehub.*"}),
            f"dev.blehub.{consumer.DEVICE_BUNDLE_SUFFIX}",
        )


class AppleBackendStagingTests(unittest.TestCase):
    """The signed product links the staged copy, so staging must match the source it claims."""

    def build(self, root: Path, *, source: str, staged: str | None) -> None:
        source_root = root / "platforms/apple/Sources/BleHubApple"
        source_root.mkdir(parents=True)
        (source_root / "AppleBleBackend.swift").write_text(source, encoding="utf-8")
        # A real staging always carries `Sources`; only the file under test may be absent.
        staged_root = root / "staged/BleHubApple/Sources/BleHubApple"
        staged_root.mkdir(parents=True)
        if staged is not None:
            (staged_root / "AppleBleBackend.swift").write_text(staged, encoding="utf-8")
        with (
            patch.object(consumer, "APPLE_BACKEND_SOURCES", source_root),
            patch.object(consumer, "RN_STAGED_ROOT", root / "staged"),
        ):
            consumer.require_current_apple_backend()

    def test_a_matching_staged_backend_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.build(Path(directory), source="fix", staged="fix")

    def test_a_staged_backend_from_another_source_is_refused(self) -> None:
        # mtimes agree and the file exists: only content comparison catches this.
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(consumer.BlengIOSConsumerError),
        ):
            self.build(Path(directory), source="fix", staged="previous")

    def test_a_file_missing_from_the_staged_backend_is_refused(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(consumer.BlengIOSConsumerError),
        ):
            self.build(Path(directory), source="fix", staged=None)


if __name__ == "__main__":
    unittest.main()
