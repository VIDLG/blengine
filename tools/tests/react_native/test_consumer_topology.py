"""Keep the formal package as the only RN consumer; the legacy probe host is retired."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONSUMERS = ROOT / "tests/consumers"
FORMAL = CONSUMERS / "react-native-bleng"
RETIRED_CONSUMER = CONSUMERS / "react-native-internal"
RETIRED_INTEGRATION = ROOT / "integrations/react-native-internal"
PACKAGE = ROOT / "integrations/react-native-bleng"


class ConsumerTopologyTests(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("node") and (FORMAL / "node_modules/jest/package.json").is_file(),
        "explicit formal-host-install and managed Node are required for real Jest discovery",
    )
    def test_formal_component_discovery_excludes_device_e2e_bootstraps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = {root / "src/App.test.tsx", root / "src/nested/scan.test.ts"}
            device = {root / "e2e/idle.test.js", root / "e2e/detox/navigation.test.ts"}
            for path in expected | device:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "throw new Error('discovery must never execute a fixture');\n",
                    encoding="utf-8",
                )
            script = """
const config = require(process.argv[1]);
const {runCLI} = require('jest');
runCLI({
  config: JSON.stringify({
    rootDir: process.argv[2],
    testMatch: config.testMatch,
    cacheDirectory: process.argv[2] + '/cache',
    cache: false,
    watchman: false,
    testEnvironment: 'node',
  }),
  listTests: true, runInBand: true, json: true,
}, [process.argv[2]]).catch(error => { console.error(error); process.exitCode = 1; });
"""
            result = subprocess.run(
                ["node", "-e", script, str(FORMAL / "jest.config.js"), str(root)],
                cwd=FORMAL,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            )
            discovered = {Path(path) for path in json.loads(result.stdout)}
            self.assertEqual(discovered, expected)
            self.assertFalse(discovered & device)

    def test_the_legacy_probe_host_and_integration_are_gone(self) -> None:
        self.assertFalse(RETIRED_CONSUMER.exists())
        self.assertFalse(RETIRED_INTEGRATION.exists())
        recipes = (ROOT / "just/react-native/mod.just").read_text(encoding="utf-8")
        self.assertNotIn("integrations/react-native-internal", recipes)
        self.assertNotIn("tests/consumers/react-native-internal", recipes)
        # The only surviving mention is the formal autolinking leak guard.
        self.assertEqual(recipes.count("react-native-internal"), 1, recipes)
        self.assertIn('config.dependencies["@blehub/react-native-internal"]', recipes)
        self.assertNotIn("\nhost-install:", recipes)
        self.assertNotIn("\nhost-android-build:", recipes)
        self.assertNotIn("\nhost-ios-pods:", recipes)

    def test_the_formal_host_has_no_internal_dependency(self) -> None:
        package = json.loads((FORMAL / "package.json").read_text(encoding="utf-8"))
        self.assertIn("@blengine/react-native-bleng", package["dependencies"])
        self.assertNotIn("@blehub/react-native-internal", package["dependencies"])
        lock = json.loads((FORMAL / "package-lock.json").read_text(encoding="utf-8"))
        self.assertNotIn(
            "@blehub/react-native-internal", lock["packages"][""]["dependencies"]
        )
        self.assertNotIn("node_modules/@blehub/react-native-internal", lock["packages"])

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
        self.assertIn("HybridBlengCentral()", text)
        # The bridge test must assert a *platform detail*, not only the generic outcome: that is
        # what tells the consumer image apart from a library-only smoke test. `PlatformErrorDetails`
        # is the type every backend failure carries those facts in.
        self.assertIn("PlatformErrorDetails", text)
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
        self.assertFalse(RETIRED_INTEGRATION.exists())
        recipes = (ROOT / "just/react-native/mod.just").read_text(encoding="utf-8")
        self.assertNotIn("\nandroid-test-build:", recipes)


if __name__ == "__main__":
    unittest.main()
