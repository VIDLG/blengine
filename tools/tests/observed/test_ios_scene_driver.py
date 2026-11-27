"""Device-observed regressions for the in-process iOS scene driver.

Each test replays what `xcodebuild` actually printed on a real iPhone run.
"""

from __future__ import annotations

import unittest

from tools.react_native import run_ios_scene_driver as lane

# `xcodebuild.log` of the reproduction, verbatim (its one blank line included).
AUTOMATION_MODE_CONSOLE = (
    "Testing failed:\n"
    "\tBleHubBlengHostUITests-Runner (5077) encountered an error (The test runner "
    "failed to initialize for UI testing. (Underlying Error: Timed out while enabling "
    "automation mode.))\n"
    "\n"
    "** TEST EXECUTE FAILED **\n"
)


class ObservedIphoneSceneDriverTests(unittest.TestCase):
    # Trigger: XCTest timed out during UI runner initialization for the `scan` scene.
    # Symptom: xcodebuild exited 65 before any scene step ran or produced a receipt.
    #   The missing-receipt error hid the timeout. The later diagnostic attributed it
    #   to a phone setting whose state the run had never observed.
    # Provenance: iPhone 14 Plus `00008110-000C61160C39401E`, iOS 27.2.1, 2026-11-24;
    #   `.work/ios-scene-driver/e42a99ac-f793-4851-bc18-d5e854187f19/xcodebuild.log`
    #   (standalone reproduction of `.work/ios-native-sequence/2026-11-24-b/scan`).
    def test_regression_iphone_xctest_automation_mode_timeout_is_named(self) -> None:
        error = lane.runner_initialization_error(AUTOMATION_MODE_CONSOLE, returncode=65)
        self.assertIsInstance(error, RuntimeError)
        text = str(error)
        self.assertIn("The test runner failed to initialize for UI testing.", text)
        self.assertIn("Timed out while enabling automation mode.", text)
        self.assertIn("xcodebuild exit=65", text)
        self.assertIn("xcodebuild_output", text)
        self.assertNotRegex(
            text.casefold(),
            r"did not enable|disabled|settings > developer|toggle|reboot|restart|reset|"
            r"first[- ]use|prompt|usb|unplug|replug",
        )


if __name__ == "__main__":
    unittest.main()
