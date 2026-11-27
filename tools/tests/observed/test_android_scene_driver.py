"""Device-observed regressions for the Android scene driver's device protocol.

Each test replays what a OnePlus PHB110 actually did to the lane. The phone stands in through
`support/android_scene_device.py`, whose logcat replay pauses before its last markers exactly
as the device's did.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.android import adb
from tools.react_native import run_android_scene_driver as driver
from tools.tests.react_native.support.android_scene_device import (
    APP_ID,
    SERIAL,
    SceneHarness,
    marker,
)

# The markers logcat delivered after the host had declared the replay settled, in order: the
# previous scene's last six, then the new scene's first. Paths are the device's own.
PREVIOUS_SCENE_TAIL = (
    marker("step:59:wait", pid=9001),
    marker("step:60:read", pid=9001),
    marker("step:61:read", pid=9001),
    marker(
        "receipt:/storage/emulated/0/Android/data/com.blehubblenghost/files/scene-receipt.json",
        pid=9001,
    ),
    marker(
        "hierarchy:/storage/emulated/0/Android/data/com.blehubblenghost/files/scene-hierarchy.xml",
        pid=9001,
    ),
    marker("result:passed", pid=9001),
)
# `adb exec-out cat` on a file that does not exist: the shell's complaint, and exit 0.
MISSING_RECEIPT_ANSWER = (
    "cat: /sdcard/Android/data/com.blehubblenghost/files/scene-receipt.json: "
    "No such file or directory\n"
)


class ObservedAndroidSceneDriverTests(unittest.TestCase):
    def harness(self) -> SceneHarness:
        return SceneHarness(Path(self.enterContext(tempfile.TemporaryDirectory())))

    # Trigger: `write-reject` run right after `cancellation` in one sequence. Logcat's replay
    #   of the previous scene paused for a tenth of a second three markers before its end.
    # Symptom: that scene's `result:passed` was taken as this scene's completion. The driver
    #   was stopped 3.2 s after it started, no receipt existed, and the failure surfaced as
    #   `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`.
    # Provenance: OnePlus PHB110 `3ac5cc09`, Android 16, 2026-11-24;
    #   `.work/android-native-sequence/2026-11-24-a/write-reject/`
    #   `8178a98a-0f45-4107-b85a-bf491e1403fa/driver/scene-write-reject.json` (`markers`,
    #   `instrumentation_returncode` -15, `instrumentation_seconds` 3.197). The plan's steps
    #   play no part in the failure, so the harness's two-step plan stands in for the scene.
    def test_regression_android_scene_replayed_marker_ends_the_next_scene(self) -> None:
        with self.harness() as harness:
            phone = harness.phone
            for line in PREVIOUS_SCENE_TAIL:
                phone.log(line)
            result = harness.scene()
            self.assertEqual(result["status"], "passed", result["primary_error"])
            self.assertEqual(result["markers"][0], "scene:tap")
            # Exactly one completion was reported, and it was this scene's own.
            self.assertEqual(result["markers"].count("result:passed"), 1)
            self.assertEqual(result["markers"][-1], "result:passed")
            self.assertEqual(
                (
                    result["instrumentation_returncode"],
                    result["instrumentation_lingered"],
                ),
                (0, False),
            )

    # Trigger: the driver had published nothing, so the receipt did not exist when the host
    #   read it back.
    # Symptom: the lane decoded the shell's sentence as JSON and reported `JSONDecodeError`.
    # Provenance: same run, `driver/record.json` (`primary_error`). The device's answer was
    #   then read by hand on 2026-11-24: exit 0 and the text below on stdout.
    def test_regression_android_scene_execout_missing_receipt_is_exit_zero(
        self,
    ) -> None:
        with self.harness() as harness:
            phone = harness.phone
            # The device fact itself, as the boundary returns it.
            answer = adb.run_command(
                adb.adb_command(
                    SERIAL,
                    "exec-out",
                    "cat",
                    driver._files_path(APP_ID, driver.RECEIPT_FILE),
                ),
                timeout=5,
            )
            self.assertEqual(
                (answer.returncode, answer.stdout), (0, MISSING_RECEIPT_ANSWER)
            )

            def quits(scene_phone, process) -> None:
                process.finish(1, b"INSTRUMENTATION_ABORTED\n")

            phone.behaviour = quits
            with self.assertRaisesRegex(
                RuntimeError, "no readable scene-receipt.json"
            ) as raised:
                harness.scene()
            # The failure carries the device's own words, not a decoder's.
            self.assertIn("No such file or directory", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
