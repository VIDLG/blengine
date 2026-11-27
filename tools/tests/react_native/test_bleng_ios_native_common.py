"""The iOS marker relay and xctestrun staging, both checked without a device or an Xcode build.

These are the two places an iOS native lane can fail silently: a marker parser that accepts the
wrong line turns "the test never got there" into "the stage happened", and an xctestrun that
carries a stale environment runs a different topology than the one the host declared.
"""

from __future__ import annotations

import json
import plistlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.react_native.bleng_ios_native_common import (
    DECLARATION_VARIABLE,
    MARKER_TAG_VARIABLE,
    SyslogMarkerCapture,
    app_bundle_id,
    require_executable,
    resolve_xctestrun,
    stage_environment,
    staged_xctestrun,
    tree_sha256,
)
from tools.react_native.bleng_multi_link_contract import (
    MARKER_TAG,
    MAXIMUM_PEERS,
    PROBE_TAG,
    parse_peer,
)


class MarkerParsingTests(unittest.TestCase):
    def capture(
        self, lines: list[str], *, tag: str = MARKER_TAG
    ) -> SyslogMarkerCapture:
        capture = SyslogMarkerCapture.__new__(SyslogMarkerCapture)
        capture._tag = tag
        capture._lines = lines
        capture._lock = threading.Lock()
        return capture

    def test_a_tagged_line_becomes_its_stage(self):
        # A real relay line: date, device, process, payload.
        capture = self.capture(
            [
                (
                    "Nov 21 12:00:00 iPhone BleHubBlengHost[4242] <Notice>: "
                    f"{MARKER_TAG}: started"
                ),
                (
                    "Nov 21 12:00:01 iPhone BleHubBlengHost[4242] <Notice>: "
                    f"{MARKER_TAG}: subscribed:0"
                ),
            ]
        )
        self.assertEqual(capture.stages(), ["started", "subscribed:0"])

    def test_a_line_that_merely_mentions_the_tag_is_not_a_stage(self):
        for line in (
            f"prefix {MARKER_TAG}_OTHER: started",
            f"someone wrote {MARKER_TAG} in prose",
            f"{MARKER_TAG}",
            # A timestamp colon is not the marker delimiter.
            f"Nov 21 12:00:00 {MARKER_TAG} started",
        ):
            with self.subTest(line=line):
                self.assertEqual(self.capture([line]).stages(), [])

    def test_stages_keep_their_order_and_are_not_deduplicated(self):
        capture = self.capture(
            [
                f"{MARKER_TAG}: value:0",
                f"{MARKER_TAG}: value:1",
                f"{MARKER_TAG}: value:0",
            ]
        )
        self.assertEqual(capture.stages(), ["value:0", "value:1", "value:0"])


class ProbeChannelTests(unittest.TestCase):
    """The opening probe burst is diagnostics: it must never be read as a scene stage.

    The relay can lose a just-launched process's first lines, so the scene spends that window on
    probes. If a probe could become a stage, the burst would move the loss rather than absorb it.
    """

    def capture(self, lines: list[str]) -> SyslogMarkerCapture:
        capture = SyslogMarkerCapture.__new__(SyslogMarkerCapture)
        capture._tag = MARKER_TAG
        capture._lines = lines
        capture._lock = threading.Lock()
        return capture

    def test_a_probe_line_is_captured_but_is_not_a_stage(self):
        line = (
            f"Nov 21 12:00:00 iPhone BleHubBlengHost[1] <Notice>: {PROBE_TAG}: probe:0"
        )
        capture = self.capture([line])
        self.assertEqual(capture.stages(), [])
        self.assertEqual(capture.lines_containing(PROBE_TAG), 1)

    def test_the_probe_tag_cannot_satisfy_the_marker_tag(self):
        # A probe is the marker tag followed by a word character: not a delimiter, so not a stage.
        self.assertNotEqual(PROBE_TAG, MARKER_TAG)
        self.assertTrue(PROBE_TAG.startswith(MARKER_TAG))


class XctestrunStagingTests(unittest.TestCase):
    def document(self, *, extra: dict[str, str] | None = None) -> dict:
        return {
            "BleHubBlengHostNativeTests": {
                "TestBundlePath": "/products/Debug-iphoneos/BleHubBlengHostNativeTests.xctest",
                "TestHostPath": "/products/Debug-iphoneos/BleHubBlengHost.app",
                "EnvironmentVariables": dict(extra or {}),
            },
            "BleHubBlengHostUITests": {
                "TestBundlePath": "/products/Debug-iphoneos/BleHubBlengHostUITests.xctest",
            },
        }

    def stage(self, root: Path, document: dict, **overrides) -> Path:
        source = root / "source.xctestrun"
        source.write_bytes(plistlib.dumps(document))
        arguments = {
            "test_bundle": "BleHubBlengHostNativeTests.xctest",
            "environment": {
                MARKER_TAG_VARIABLE: "multi-link",
                DECLARATION_VARIABLE: "[]",
            },
        }
        arguments.update(overrides)
        return staged_xctestrun(source, root / "staged.xctestrun", **arguments)

    def test_staging_leaves_no_unexpanded_product_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = self.document()
            document["BleHubBlengHostNativeTests"]["UITargetAppPath"] = (
                "__TESTROOT__/Debug-iphoneos/BleHubBlengHost.app"
            )
            staged = self.stage(root, document)
            self.assertNotIn("__TESTROOT__", staged.read_text())

    def test_the_declaration_lands_on_the_native_target_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = self.stage(root, self.document())
            document = plistlib.loads(staged.read_bytes())
            native = document["BleHubBlengHostNativeTests"]
            self.assertEqual(native["EnvironmentVariables"][DECLARATION_VARIABLE], "[]")
            self.assertNotIn("EnvironmentVariables", document["BleHubBlengHostUITests"])

    def test_an_existing_value_is_never_silently_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = self.document(extra={MARKER_TAG_VARIABLE: "something-else"})
            with self.assertRaises(ValueError):
                self.stage(root, document)

    def test_a_manifest_naming_another_app_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = root / "Other.app"
            other.mkdir()
            with self.assertRaises(ValueError):
                self.stage(root, self.document(), expected_app=other)

    def test_a_manifest_naming_this_app_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = Path("/products/Debug-iphoneos/BleHubBlengHost.app")
            staged = self.stage(root, self.document(), expected_app=app)
            self.assertTrue(staged.is_file())

    def test_placeholders_expand_against_the_products_that_produced_the_manifest(self):
        # A hosted bundle lives at `<host>.app/PlugIns/<bundle>`: expanding `__TESTHOST__` to the
        # host's *directory* points at a path that does not exist, and xcodebuild reports it as a
        # missing test product.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "Debug-iphoneos/BleHubBlengHost.app"
            app.mkdir(parents=True)
            document = {
                "BleHubBlengHostNativeTests": {
                    "TestHostPath": "__TESTROOT__/Debug-iphoneos/BleHubBlengHost.app",
                    "TestBundlePath": "__TESTHOST__/PlugIns/BleHubBlengHostNativeTests.xctest",
                }
            }
            staged = self.stage(root, document, expected_app=app)
            staged_document = plistlib.loads(staged.read_bytes())
            target = staged_document["BleHubBlengHostNativeTests"]
            self.assertEqual(Path(target["TestHostPath"]), app.resolve())
            self.assertEqual(
                Path(target["TestBundlePath"]),
                (app.resolve() / "PlugIns/BleHubBlengHostNativeTests.xctest"),
            )

    def test_a_manifest_without_the_target_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = {"Other": {"TestBundlePath": "/products/Other.xctest"}}
            with self.assertRaises(ValueError):
                self.stage(root, document)

    def test_the_environment_carries_the_opt_in_and_the_topology(self):
        environment = stage_environment(declaration='[{"serviceUuid":"x"}]')
        self.assertEqual(environment[MARKER_TAG_VARIABLE], "multi-link")
        self.assertEqual(environment[DECLARATION_VARIABLE], '[{"serviceUuid":"x"}]')


class XctestrunResolutionTests(unittest.TestCase):
    """One manifest read into the absolute facts a run needs, with no device involved.

    `test-without-building` launches the product the manifest names. So the questions that
    matter before a lease are which target is ours, which App hosts it, and whether the host
    the manifest declares is the App the operator authorized.
    """

    BUNDLE_ID = "TEAM123456.bleng-rn-host"

    def manifest(self, root: Path, **overrides: object) -> Path:
        target = {
            "TestHostPath": "__TESTROOT__/Debug-iphoneos/BleHubBlengHost.app",
            "TestBundlePath": "__TESTHOST__/PlugIns/BleHubBlengHostDeviceTests.xctest",
            "TestHostBundleIdentifier": self.BUNDLE_ID,
            "EnvironmentVariables": {},
        }
        target.update(overrides)
        source = root / "BleHubBlengHost_iphoneos27.2-arm64.xctestrun"
        source.write_bytes(plistlib.dumps({"BleHubBlengHostDeviceTests": target}))
        return source

    def test_a_ui_target_app_path_is_expanded_against_the_source_products(self):
        # A UI test entry names the App under test separately from its runner host. A staged
        # manifest that keeps `__TESTROOT__` there points xcodebuild at the staging directory.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.xctestrun"
            source.write_bytes(
                plistlib.dumps(
                    {
                        "BleHubBlengHostUITests": {
                            "TestHostPath": (
                                "__TESTROOT__/Debug-iphoneos/"
                                "BleHubBlengHostUITests-Runner.app"
                            ),
                            "TestBundlePath": (
                                "__TESTHOST__/PlugIns/BleHubBlengHostUITests.xctest"
                            ),
                            "TestHostBundleIdentifier": self.BUNDLE_ID + ".xctrunner",
                            "UITargetAppPath": (
                                "__TESTROOT__/Debug-iphoneos/BleHubBlengHost.app"
                            ),
                        }
                    }
                )
            )
            resolved = resolve_xctestrun(
                source, test_bundle="BleHubBlengHostUITests.xctest"
            )
            target = resolved.document["BleHubBlengHostUITests"]
            self.assertEqual(
                target["UITargetAppPath"],
                str(root.resolve() / "Debug-iphoneos/BleHubBlengHost.app"),
            )
            self.assertNotIn("__TESTROOT__", json.dumps(target))

    def test_the_target_name_comes_from_the_manifest(self):
        # The xctestrun key is the target name `-only-testing` takes; the bundle is the other
        # spelling. Reading the key back is what keeps the two from drifting apart.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "Debug-iphoneos/BleHubBlengHost.app"
            app.mkdir(parents=True)
            resolved = resolve_xctestrun(
                self.manifest(root),
                test_bundle="BleHubBlengHostDeviceTests.xctest",
                expected_app=app,
                expected_host_bundle_id=self.BUNDLE_ID,
            )
            self.assertEqual(resolved.name, "BleHubBlengHostDeviceTests")
            self.assertEqual(resolved.host_app, app.resolve())
            self.assertEqual(
                resolved.bundle,
                app.resolve() / "PlugIns/BleHubBlengHostDeviceTests.xctest",
            )

    def test_a_manifest_hosting_another_bundle_identifier_is_refused(self):
        # Similar path, different product: the host identity is what the installed App answers to.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "Debug-iphoneos/BleHubBlengHost.app"
            app.mkdir(parents=True)
            with self.assertRaises(ValueError):
                resolve_xctestrun(
                    self.manifest(root),
                    test_bundle="BleHubBlengHostDeviceTests.xctest",
                    expected_app=app,
                    expected_host_bundle_id="TEAM123456.other-host",
                )

    def test_a_hosted_target_without_a_host_path_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "manifest.xctestrun"
            source.write_bytes(
                plistlib.dumps(
                    {
                        "BleHubBlengHostDeviceTests": {
                            "TestBundlePath": (
                                "/products/PlugIns/BleHubBlengHostDeviceTests.xctest"
                            )
                        }
                    }
                )
            )
            with self.assertRaises(ValueError):
                resolve_xctestrun(
                    source, test_bundle="BleHubBlengHostDeviceTests.xctest"
                )


class RelayWarmupTests(unittest.TestCase):
    """A relay is not a channel the instant its first line arrives.

    The scene lost its three opening markers when launched at that instant and kept them when
    launched a moment later, so the warm-up is a named budget rather than an implicit race.
    """

    def relay(self, *, received: int) -> SyslogMarkerCapture:
        capture = SyslogMarkerCapture.__new__(SyslogMarkerCapture)
        capture._lines = []
        capture._received = received
        capture._first_line_at = None
        capture._opened_at = time.monotonic()
        capture._warmup_seconds = 0.0
        capture._lock = threading.Lock()
        return capture

    def test_a_live_relay_is_not_used_until_its_warmup_has_elapsed(self):
        capture = self.relay(received=1)
        started = time.monotonic()
        capture.wait_live(time.monotonic() + 30, warmup=0.05)
        self.assertGreaterEqual(time.monotonic() - started, 0.05)
        self.assertEqual(capture.warmup_seconds, 0.05)

    def test_a_budget_that_cannot_cover_the_warmup_is_refused(self):
        capture = self.relay(received=1)
        with self.assertRaises(TimeoutError):
            capture.wait_live(time.monotonic() + 0.001, warmup=5.0)

    def test_a_relay_that_never_delivered_a_line_reports_no_attach(self):
        capture = self.relay(received=0)
        with self.assertRaises(TimeoutError):
            capture.wait_live(time.monotonic() + 0.01, warmup=0)
        self.assertIsNone(capture.attach_seconds())


class AppTreeDigestTests(unittest.TestCase):
    def test_the_digest_covers_paths_and_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "App.app"
            (app / "PlugIns").mkdir(parents=True)
            (app / "Info.plist").write_bytes(b"one")
            (app / "PlugIns/Tests.xctest").write_bytes(b"two")
            first = tree_sha256(app)
            (app / "PlugIns/Tests.xctest").write_bytes(b"three")
            self.assertNotEqual(first, tree_sha256(app))

    def test_a_symlink_is_refused_rather_than_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "App.app"
            app.mkdir()
            target = root / "outside"
            target.write_bytes(b"x")
            (app / "link").symlink_to(target)
            with self.assertRaises(ValueError):
                tree_sha256(app)


class SignedAppFactsTests(unittest.TestCase):
    def app(self, root: Path, *, bundle_id: str | None) -> Path:
        app = root / "BleHubBlengHost.app"
        app.mkdir()
        info = {} if bundle_id is None else {"CFBundleIdentifier": bundle_id}
        (app / "Info.plist").write_bytes(plistlib.dumps(info))
        return app

    def test_the_identifier_is_read_from_the_signed_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory), bundle_id="TEAM123456.bleng-rn-host")
            self.assertEqual(app_bundle_id(app), "TEAM123456.bleng-rn-host")

    def test_an_app_without_an_identifier_is_refused(self):
        for bundle_id in (None, ""):
            with (
                tempfile.TemporaryDirectory() as directory,
                self.subTest(bundle_id=bundle_id),
            ):
                app = self.app(Path(directory), bundle_id=bundle_id)
                with self.assertRaises(ValueError):
                    app_bundle_id(app)


class HostToolTests(unittest.TestCase):
    def test_a_missing_tool_is_named_before_anything_is_leased(self):
        with (
            patch(
                "tools.react_native.bleng_ios_native_common.shutil.which",
                return_value=None,
            ),
            self.assertRaises(RuntimeError) as caught,
        ):
            require_executable("idevicesyslog")
        self.assertIn("idevicesyslog", str(caught.exception))

    def test_a_resolved_tool_is_returned(self):
        with patch(
            "tools.react_native.bleng_ios_native_common.shutil.which",
            return_value="/usr/local/bin/idevicesyslog",
        ):
            self.assertEqual(
                require_executable("idevicesyslog"), "/usr/local/bin/idevicesyslog"
            )


class PeerDeclarationTests(unittest.TestCase):
    def test_a_declaration_carries_exactly_the_declared_fields(self):
        self.assertEqual(
            parse_peer(
                "port=/dev/cu.usbmodem1,contract=/c.json,profile=/p.json,"
                "identity=jlink:1050252028"
            ),
            {
                "port": "/dev/cu.usbmodem1",
                "contract": "/c.json",
                "profile": "/p.json",
                "identity": "jlink:1050252028",
            },
        )

    def test_a_malformed_declaration_is_refused(self):
        for text in (
            "port=/dev/cu.usbmodem1,contract=/c.json,profile=/p.json",
            "port=/dev/cu.usbmodem1,contract=/c.json,profile=/p.json,identity=x,extra=1",
            "port=/dev/cu.usbmodem1,contract=/c.json,profile=/p.json,identity=x,identity=y",
            "port=/dev/cu.usbmodem1,contract,profile=/p.json,identity=x",
            "port=/dev/cu.usbmodem1,contract=,profile=/p.json,identity=x",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_peer(text)

    def test_the_lane_bound_is_one_constant(self):
        self.assertGreaterEqual(MAXIMUM_PEERS, 2)


if __name__ == "__main__":
    unittest.main()
