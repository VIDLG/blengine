"""The iOS lane's pre-lease checks, exercised without a phone, a lease or an Xcode build.

Every one of these inputs is cheap to verify and expensive to discover late. Today a wrong App,
a manifest that hosts another product, an App that never embedded the test bundle or a missing
relay would first surface after a phone lease, an App install and a fixture startup — so the
lane refuses them before anything is acquired, and this file pins that ordering.
"""

from __future__ import annotations

import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.react_native import bleng_ios_multi_link as lane
from tools.react_native.bleng_ios_native_common import tree_sha256

BUNDLE_ID = "TEAM123456.bleng-rn-host"


class PreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.products = self.root / "Build/Products"
        self.app = self.products / "BleHubBlengHost.app"
        self.bundle = self.app / "PlugIns" / lane.TEST_BUNDLE
        self.bundle.mkdir(parents=True)
        (self.app / "embedded.mobileprovision").write_bytes(b"profile")
        self.write_info(BUNDLE_ID)
        self.manifest = self.products / "BleHubBlengHost_iphoneos27.2-arm64.xctestrun"
        self.write_manifest()
        tools = patch(
            "tools.react_native.bleng_ios_native_common.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        )
        tools.start()
        self.addCleanup(tools.stop)

    def write_info(self, bundle_id: str) -> None:
        (self.app / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": bundle_id})
        )

    def write_manifest(self, **overrides: object) -> None:
        target = {
            "TestHostPath": "__TESTROOT__/BleHubBlengHost.app",
            "TestBundlePath": f"__TESTHOST__/PlugIns/{lane.TEST_BUNDLE}",
            "TestHostBundleIdentifier": BUNDLE_ID,
            "EnvironmentVariables": {},
        }
        target.update(overrides)
        self.manifest.write_bytes(
            plistlib.dumps({"BleHubBlengHostDeviceTests": target})
        )

    def preflight(self, **overrides: object):
        arguments = {
            "app": self.app,
            "app_tree_sha256": "",
            "xctestrun": self.manifest,
        }
        arguments.update(overrides)
        return lane._preflight(**arguments)

    def test_the_prepared_facts_are_the_signed_app_and_its_manifest_target(self):
        prepared = self.preflight()
        self.assertEqual(prepared.bundle_id, BUNDLE_ID)
        self.assertEqual(prepared.tree_sha256, tree_sha256(self.app))
        # The target name comes from the manifest, so `-only-testing` cannot disagree with it.
        self.assertEqual(prepared.test_target, "BleHubBlengHostDeviceTests")

    def test_an_app_of_another_tree_is_refused(self):
        with self.assertRaises(ValueError):
            self.preflight(app_tree_sha256="0" * 64)

    def test_a_manifest_hosting_another_app_is_refused(self):
        self.write_manifest(TestHostPath="__TESTROOT__/Other.app")
        with self.assertRaises(ValueError):
            self.preflight()

    def test_a_manifest_hosting_another_bundle_identifier_is_refused(self):
        self.write_manifest(TestHostBundleIdentifier="TEAM123456.other-host")
        with self.assertRaises(ValueError):
            self.preflight()

    def test_an_app_that_does_not_embed_the_test_bundle_is_refused(self):
        shutil.rmtree(self.bundle)
        with self.assertRaises(ValueError):
            self.preflight()

    def test_an_unsigned_app_is_refused(self):
        (self.app / "embedded.mobileprovision").unlink()
        with self.assertRaises(ValueError):
            self.preflight()

    def test_a_missing_manifest_is_refused(self):
        self.manifest.unlink()
        with self.assertRaises(ValueError):
            self.preflight()

    def test_a_missing_host_tool_is_refused(self):
        with (
            patch(
                "tools.react_native.bleng_ios_native_common.shutil.which",
                return_value=None,
            ),
            self.assertRaises(RuntimeError) as caught,
        ):
            self.preflight()
        self.assertIn("xcrun", str(caught.exception))


class RoundArtifactTests(unittest.TestCase):
    """A repeated scene keeps its documented artifacts for one round, and names each otherwise."""

    def test_a_single_round_keeps_the_documented_file_names(self):
        root = Path("/attempt")
        self.assertEqual(
            lane._result_bundle(root, 0, 1), Path("/attempt/multi-link.xcresult")
        )
        self.assertEqual(lane._round_log(root, 0, 1), Path("/attempt/xcodebuild.log"))

    def test_each_round_gets_its_own_artifacts(self):
        root = Path("/attempt")
        self.assertEqual(
            lane._result_bundle(root, 2, 3), Path("/attempt/multi-link-2.xcresult")
        )
        self.assertEqual(lane._round_log(root, 2, 3), Path("/attempt/xcodebuild-2.log"))


if __name__ == "__main__":
    unittest.main()
