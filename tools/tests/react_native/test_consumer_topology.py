"""Prevent formal package tests from drifting back into the legacy probe host."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONSUMERS = ROOT / "tests/consumers"
FORMAL = CONSUMERS / "react-native-bleng"
LEGACY = CONSUMERS / "react-native-internal"
PACKAGE = ROOT / "integrations/react-native-bleng"


class ConsumerTopologyTests(unittest.TestCase):
    def test_formal_and_legacy_hosts_have_separate_npm_autolinking_graphs(self) -> None:
        for host, expected, forbidden in (
            (FORMAL, "@blehub/react-native-bleng", "@blehub/react-native-internal"),
            (LEGACY, "@blehub/react-native-internal", "@blehub/react-native-bleng"),
        ):
            with self.subTest(host=host.name):
                package = json.loads(
                    (host / "package.json").read_text(encoding="utf-8")
                )
                self.assertIn(expected, package["dependencies"])
                self.assertNotIn(forbidden, package["dependencies"])
                lock = json.loads(
                    (host / "package-lock.json").read_text(encoding="utf-8")
                )
                self.assertNotIn(forbidden, lock["packages"][""]["dependencies"])
                self.assertNotIn(f"node_modules/{forbidden}", lock["packages"])

    def test_apple_formal_native_contract_is_owned_by_formal_consumer(self) -> None:
        tests = (
            FORMAL / "ios/BleHubBlengHostNativeTests/BleHubBlengHostNativeTests.swift"
        ).read_text(encoding="utf-8")
        self.assertIn("@testable import BleHubReactNativeBleng", tests)
        self.assertIn(
            "testFormalRustSupervisorOwnerStartsAndClosesWithoutProbeSurface", tests
        )
        self.assertIn(
            "testNativeNitroPromisesResolveAndRejectWithoutProbeSurface", tests
        )
        self.assertNotIn("XCTAssertTrue(true)", tests)
        podfile = (FORMAL / "ios/Podfile").read_text(encoding="utf-8")
        native_target = podfile.split('target "BleHubBlengHostNativeTests" do', 1)[1]
        self.assertIn('pod "BleHubReactNativeBleng"', native_target)

        legacy_tests = (
            LEGACY / "ios/BleHubNitroHostNativeTests/BleHubNitroHostNativeTests.swift"
        ).read_text(encoding="utf-8")
        self.assertNotIn("BleHubReactNativeBleng", legacy_tests)
        self.assertNotIn("testFormalRustSupervisorOwner", legacy_tests)
        legacy_podfile = (LEGACY / "ios/Podfile").read_text(encoding="utf-8")
        self.assertNotIn('pod "BleHubReactNativeBleng"', legacy_podfile)
        self.assertIn('pod "BleHubUniFfi"', legacy_podfile)

    def test_formal_ios_contract_does_not_require_a_second_consumer_source_root(
        self,
    ) -> None:
        contract = FORMAL / "ios/PlatformContract"
        self.assertTrue((contract / "project.yml").is_file())
        self.assertTrue((contract / "Sources/LifecycleCompileSmoke.swift").is_file())
        retired = CONSUMERS / "apple/bleng-rn-ios"
        for manifest in ("project.yml", "nitro-project.yml", "package.json", "Podfile"):
            self.assertFalse((retired / manifest).exists())
        retired_owner = ROOT / "tools/react_native/bleng_ios_artifact_consumer.py"
        self.assertFalse(retired_owner.exists())

    def test_android_formal_bridge_tests_use_consumer_not_library_test_image(
        self,
    ) -> None:
        native_tests = FORMAL / "android/app/src/androidTest"
        sources = list(native_tests.rglob("*.kt"))
        text = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        self.assertIn("HybridBlengSessionOwner()", text)
        self.assertIn("SupervisorFfiException.Platform", text)
        self.assertNotIn("HybridBleHubProbe", text)
        self.assertNotIn("BuildConfig.APPLICATION_ID", text)
        app = (FORMAL / "android/app/build.gradle").read_text(encoding="utf-8")
        self.assertIn(
            'testInstrumentationRunner "androidx.test.runner.AndroidJUnitRunner"', app
        )
        self.assertFalse(list((PACKAGE / "android/src/androidTest").rglob("*.kt")))
        library = (PACKAGE / "android/build.gradle").read_text(encoding="utf-8")
        self.assertNotIn("androidTestImplementation", library)
        self.assertNotIn("testInstrumentationRunner", library)
        legacy_sources = ROOT / "integrations/react-native-internal/android/src"
        self.assertFalse(list((legacy_sources / "androidTest").rglob("*.kt")))
        recipes = (ROOT / "just/react-native/mod.just").read_text(encoding="utf-8")
        self.assertNotIn("\nandroid-test-build:", recipes)


if __name__ == "__main__":
    unittest.main()
