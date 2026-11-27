"""Contract for the Android scene executor: the half `scene_native` cannot know.

The vocabulary (phases, host actions, receipts, derivations) is shared and is pinned through
the iOS executor. What is Android's alone is small and worth stating on its own: the artifact
it pins, the platform it refuses, the fact that a scene is one merged run with in-band host
actions, and the one diagnostic file it publishes. The device run of this lane is a separate,
human-gated step.
"""

from __future__ import annotations

import hashlib
import json
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.android.state_control import AndroidRadioController
from tools.react_native import run_android_scene_driver as lane
from tools.react_native import scene_android_native as native
from tools.react_native.bleng_android_candidate import AndroidCandidateOwner
from tools.react_native.bleng_scan_evidence import (
    NOTIFICATION_SCENE,
    SCAN_SCENE,
)

DEVICE = "3ac5cc09"
APP_ID = "com.blehubblenghost"
ACTIVITY = "com.blehubblenghost.MainActivity"
LOCAL_NAME = "BleHubMultiRfV2DKA"
SERVICE_UUID = "fd600000-0000-4000-8000-000000000000"
CHARACTERISTIC = "fd600000-0000-4000-8000-000000000001"
SUBSCRIPTION = {
    "uuid": CHARACTERISTIC,
    "mode": "notification",
    "value": "0102030405",
}


class AndroidSceneExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.test_apk = self.root / "app-debug-androidTest.apk"
        self.test_apk.write_bytes(b"test-apk-bytes")
        self.output = self.root / "attempt" / "driver"
        self.output.mkdir(parents=True)
        self.run_id = uuid.uuid4()

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _driver(self, *, scene=SCAN_SCENE, subscription=None, activity=ACTIVITY):
        return native.NativeAndroidSceneDriver(
            scene=scene,
            device=DEVICE,
            app_id=APP_ID,
            activity=activity,
            test_apk=self.test_apk,
            output=self.output,
            cwd=self.root,
            expected_local_name=LOCAL_NAME,
            service_filter=SERVICE_UUID,
            subscription=subscription,
        )

    def _options(self) -> dict:
        return {
            "timeout": 120.0,
            "observations": [],
            "require_app_authority": lambda: None,
        }

    # -- construction ---------------------------------------------------------

    def test_refuses_an_activity_that_is_not_fully_qualified(self) -> None:
        # Instrumentation starts a component by name; a bare class name would address
        # whichever package happened to be first in the shell's search order.
        with self.assertRaisesRegex(ValueError, "fully qualified Android activity"):
            self._driver(activity="MainActivity")

    def test_refuses_a_missing_test_apk(self) -> None:
        self.test_apk.unlink()
        with self.assertRaisesRegex(ValueError, "missing test APK"):
            self._driver()

    def test_refuses_a_symlinked_test_apk(self) -> None:
        real = self.root / "elsewhere.apk"
        self.test_apk.rename(real)
        self.test_apk.symlink_to(real)
        with self.assertRaisesRegex(ValueError, "missing test APK"):
            self._driver()

    def test_the_artifact_names_the_bytes_and_the_component(self) -> None:
        self.assertEqual(
            self._driver()._artifact(),
            {
                "test_apk_sha256": hashlib.sha256(b"test-apk-bytes").hexdigest(),
                "test_component": lane.TEST_COMPONENT,
                "test_class": lane.TEST_CLASS,
            },
        )

    def test_the_artifact_is_reproved_after_the_run(self) -> None:
        # The APK is the thing that ran; a file replaced underneath the run makes the
        # receipt describe bytes no device executed.
        driver = self._driver()
        self.test_apk.write_bytes(b"a different apk")
        with self.assertRaisesRegex(RuntimeError, "changed during the run"):
            driver._require_artifact()

    # -- platform seam --------------------------------------------------------

    def test_the_driver_is_android_only(self) -> None:
        driver = self._driver()
        with self.assertRaisesRegex(TypeError, "Android-only"):
            driver._require_platform(platform="ios", candidate=None, phone=None)

    def test_it_requires_the_android_candidate_owner(self) -> None:
        with self.assertRaisesRegex(TypeError, "candidate owner"):
            self._driver()._require_platform(
                platform="android",
                candidate=None,
                phone=object.__new__(AndroidRadioController),
            )

    def test_it_requires_the_android_phone_lease(self) -> None:
        with self.assertRaisesRegex(TypeError, "phone lease"):
            self._driver()._require_platform(
                platform="android",
                candidate=object.__new__(AndroidCandidateOwner),
                phone=None,
            )

    # -- one scene, one merged run --------------------------------------------

    def test_a_phase_runs_the_whole_plan_as_an_attach(self) -> None:
        driver = self._driver()
        driver._run_id = self.run_id
        calls: list[dict] = []

        def run_scene(**kwargs) -> Path:
            calls.append(kwargs)
            result = Path(self.root) / "result.json"
            result.write_text(json.dumps({"instrumentation_returncode": 0}))
            return result

        with patch.object(lane, "run_scene", run_scene):
            result = driver._run_phase(
                driver._phases[0], self.root, self._options(), timeout=30.0
            )

        [call] = calls
        # One process: the host installed and launched this App, so the plan attaches and
        # every phase is a slice of the same receipt.
        self.assertFalse(call["launch"])
        self.assertIsNone(call["require_owner"])
        self.assertEqual(call["plan_name"], "scan")
        self.assertEqual(call["steps"], driver._merged_steps())
        self.assertEqual((call["app_id"], call["activity"]), (APP_ID, ACTIVITY))
        self.assertEqual(
            call["test_apk_sha256"], hashlib.sha256(b"test-apk-bytes").hexdigest()
        )
        self.assertEqual(result["instrumentation_returncode"], 0)

    def test_a_host_action_is_announced_inside_the_run(self) -> None:
        # A repeating/queued host action is a `handoff` step the driver answers, never a
        # second plan: the subscription scene's publish happens between two App steps.
        driver = self._driver(scene=NOTIFICATION_SCENE, subscription=SUBSCRIPTION)
        driver._run_id = self.run_id
        announced: list[str] = []

        def run_scene(**kwargs) -> Path:
            marker = next(
                step for step in kwargs["steps"] if step.get("op") == "handoff"
            )
            kwargs["on_handoff"](marker["action"])
            result = self.root / "result.json"
            result.write_text(json.dumps({"instrumentation_returncode": 0}))
            return result

        with patch.object(lane, "run_scene", run_scene):
            driver._run_scene_plan(
                self.root, self._options(), timeout=30.0, on_handoff=announced.append
            )

        self.assertEqual(announced, ["publish"])

    def test_the_phase_view_reports_the_instrumentation_returncode(self) -> None:
        driver = self._driver()
        self.assertEqual(
            driver._phase_extra({"instrumentation_returncode": -15}),
            {"instrumentation_returncode": -15},
        )

    # -- diagnostics ----------------------------------------------------------

    def test_a_hierarchy_dump_is_published_truncated(self) -> None:
        driver = self._driver()
        driver._run_id = self.run_id
        source = self.root / "scan" / str(self.run_id) / "hierarchy.xml"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"x" * (64 * 1024 + 10))

        driver._publish_diagnostics(self.root, driver._phases[0], {})

        published = self.output / "diagnostic-scan-hierarchy.xml"
        self.assertEqual(published.stat().st_size, 64 * 1024)

    def test_no_hierarchy_means_no_diagnostic_file(self) -> None:
        # A phase that failed before the dump exists says so by the absence, and never by an
        # empty file a reader could mistake for a dump that was taken.
        driver = self._driver()
        driver._run_id = self.run_id
        driver._publish_diagnostics(self.root, driver._phases[0], {})
        self.assertEqual(list(self.output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
