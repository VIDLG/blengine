"""Hardware-free contract tests for the Android scene driver's device protocol.

The lane's contract with its driver is a set of files in the App's external files directory:
the handoff request/ack exchange, the receipt, the hierarchy dump and the done file that is
published last. Its logcat markers are diagnostics. These tests drive `run_scene` against one
fake device (`support/android_scene_device.py`), so the protocol is pinned without hardware.
The failures that were observed on a real device live in `tools/tests/observed/`.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from tools.react_native import run_android_scene_driver as driver
from tools.tests.react_native.support.android_scene_device import (
    Phone,
    Process,
    SceneHarness,
    publish_passing_scene,
)


class SceneDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.harness = self.enterContext(SceneHarness(self.root))
        self.phone = self.harness.phone

    def scene(self, **changes) -> dict:
        return self.harness.scene(**changes)

    def instrumentations(self) -> list[list[str]]:
        return self.harness.instrumentations()

    def test_the_driver_is_told_where_to_publish_each_of_its_files(self) -> None:
        self.assertEqual(self.scene()["status"], "passed")
        (command,) = self.instrumentations()
        for flag, name in (
            ("receipt", driver.RECEIPT_FILE),
            ("hierarchy", driver.HIERARCHY_FILE),
            ("done", driver.DONE_FILE),
        ):
            self.assertEqual(command[command.index(flag) + 1], name)

    def test_a_resident_runner_is_stopped_once_its_done_file_exists(self) -> None:
        self.phone.resident = True
        result = self.scene()
        self.assertEqual(result["status"], "passed", result["primary_error"])
        self.assertTrue(result["instrumentation_lingered"])
        self.assertEqual(result["instrumentation_returncode"], -15)

    def test_a_failed_step_still_publishes_a_receipt_that_names_it(self) -> None:
        self.phone.behaviour = lambda phone, process: publish_passing_scene(
            phone, process, stop_after=1
        )
        with self.assertRaisesRegex(RuntimeError, "step 1 .* refused"):
            self.scene()
        result = json.loads(next(self.root.glob("*/result.json")).read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["receipt"]["steps"]), 1)
        self.assertEqual(result["instrumentation_returncode"], 1)

    def test_a_stale_done_file_stops_the_scene_before_anything_is_driven(self) -> None:
        # A leftover done file would be obeyed at once as this run's completion.
        self.phone.files[driver.DONE_FILE] = b"passed\n"
        self.phone.survive_removal.add(driver.DONE_FILE)
        with self.assertRaisesRegex(RuntimeError, "previous run's files"):
            self.scene()
        self.assertEqual(self.instrumentations(), [])

    def test_a_handoff_is_answered_by_file_and_then_consumed_by_the_driver(
        self,
    ) -> None:
        seen: list[str] = []

        def handoff_scene(phone: Phone, process: Process) -> None:
            request = "handoff-request-hold-arm"
            phone.files[request] = b""
            deadline = time.monotonic() + 5
            while "handoff-ack-hold-arm" not in phone.files:
                if time.monotonic() > deadline:
                    process.finish(1, b"never acknowledged\n")
                    return
                time.sleep(0.005)
            del phone.files[request]
            publish_passing_scene(phone, process)

        self.phone.behaviour = handoff_scene
        result = self.scene(on_handoff=seen.append)
        self.assertEqual(result["status"], "passed", result["primary_error"])
        self.assertEqual(result["handoffs"], ["hold-arm"])
        self.assertEqual(seen, ["hold-arm"])

    def test_nothing_of_the_run_is_left_in_the_drivers_directory(self) -> None:
        self.phone.resident = True
        self.assertEqual(self.scene()["status"], "passed")
        # Receipt, hierarchy, done and the scratch names beside them are all removed.
        self.assertEqual(self.phone.files, {})

    def test_a_stale_done_file_recreated_behind_a_successful_removal_is_caught(
        self,
    ) -> None:
        # `rm` answered success and the file is still there: only the readback can refuse.
        self.phone.files[driver.DONE_FILE] = b"passed\n"
        self.phone.survive_removal.add(driver.DONE_FILE)
        with self.assertRaisesRegex(RuntimeError, "previous run's files"):
            self.scene()
        self.assertEqual(self.instrumentations(), [])

    def test_a_cleanup_failure_fails_the_scene_instead_of_passing_silently(
        self,
    ) -> None:
        self.phone.fail_removal = True
        with self.assertRaisesRegex(RuntimeError, "exit=1"):
            self.scene()
        result = json.loads(next(self.root.glob("*/result.json")).read_text())
        self.assertEqual(result["status"], "failed")
        self.assertIn("exit=1", result["primary_error"])


if __name__ == "__main__":
    unittest.main()
