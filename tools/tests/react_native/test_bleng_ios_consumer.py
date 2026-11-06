"""Build-only boundaries for the formal RN host and isolated Swift contract."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools.react_native import bleng_ios_consumer as consumer

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
            "FORMAL_PACKAGE_ROOT": host / "node_modules/@blehub/react-native-bleng",
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
            "PODSPEC_LOG": self.root / "logs/podspec.log",
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
            ios / "Podfile",
            consumer.FORMAL_PODSPEC,
            consumer.SMOKE_ROOT / "LifecycleCompileSmoke.swift",
        ):
            self.put(path)
        for name in consumer.PLATFORM_SOURCES:
            self.put(consumer.FORMAL_PACKAGE_ROOT / "ios" / name)
        for relative in consumer.STAGED_FILES:
            self.put(consumer.RN_STAGED_ROOT / relative)
        self.put(consumer.SPEC, SPEC_SOURCE.read_text(encoding="utf-8"))
        self.spec: Any = {
            "dependencies": {"BleHubApple": [], "BleHubUniFfi": [], "NitroModules": []},
            "source_files": ["ios/HybridBlengSessionOwner.swift"],
        }
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.fail: str | None = None
        self.omit: str | None = None
        self.run = self.stack.enter_context(
            patch.object(consumer.subprocess, "run", side_effect=self.subprocess_run)
        )

    @staticmethod
    def put(path: Path, text: str = "fixture\n") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def subprocess_run(
        self, command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        output = kwargs["stdout"]
        if command[1:3] == ["ipc", "spec"]:
            if isinstance(self.spec, (dict, str)):
                output.write("pod warning before JSON\n")
            output.write(
                self.spec if isinstance(self.spec, str) else json.dumps(self.spec)
            )
        else:
            output.write("build output\n")
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
        if "install" in command and self.omit != "workspace":
            consumer.WORKSPACE.mkdir(parents=True, exist_ok=True)
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
            (consumer.FORMAL_PODSPEC, "formal-host-check"),
            (
                consumer.FORMAL_PACKAGE_ROOT / "ios/ApplePlatformSession.swift",
                "formal-host-check",
            ),
            (consumer.NATIVE_TEST_SOURCE, "native contract"),
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
            ({**valid, "source_files": ["ios/Other.swift"]}, "HybridBlengSessionOwner"),
        ):
            with self.subTest(invalid=invalid):
                self.calls.clear()
                self.spec = invalid
                with self.assertRaisesRegex(consumer.BlengIOSConsumerError, message):
                    consumer.build()
                self.assertEqual(len(self.calls), 1)
                command = self.calls[0][0]
                self.assertEqual(command[1:3], ["ipc", "spec"])
                self.assertEqual(command[-1], str(consumer.FORMAL_PODSPEC))
                self.generator.assert_not_called()
        self.spec = valid
        self.fail = "ipc"
        with self.assertRaisesRegex(consumer.BlengIOSConsumerError, "exit code 42"):
            consumer.build()
        self.assertIn("exact compiler diagnostic", consumer.PODSPEC_LOG.read_text())

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
        self.assertEqual(len(commands), 5)
        self.assertEqual(commands[0][1:3], ["ipc", "spec"])
        self.assertEqual(commands[1][1:3], ["generate", "--spec"])
        self.assertEqual(commands[2][-1], "build")
        self.assertEqual(commands[3], ["pod", "install", "--no-repo-update"])
        self.assertEqual(commands[4][-1], "build-for-testing")
        direct, host = commands[2], commands[4]
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
                    "cargo",
                }
                & set(command)
            )
        pod_kwargs = self.calls[3][1]
        self.assertEqual(pod_kwargs["cwd"], consumer.IOS_ROOT)
        self.assertEqual(
            pod_kwargs["env"]["BLEHUB_APPLE_BLENG_STAGING_ROOT"],
            str(consumer.RN_STAGED_ROOT),
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
            self.assertEqual(self.calls[-1][0][1:3], ["ipc", "spec"])
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

    def test_contract_compile_failure_never_reaches_pod_install(self) -> None:
        self.fail = "build"
        with self.assertRaisesRegex(
            consumer.BlengIOSConsumerError, "exact compiler diagnostic"
        ):
            consumer.build()
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn(
            "install", [part for command, _ in self.calls for part in command]
        )


if __name__ == "__main__":
    unittest.main()
