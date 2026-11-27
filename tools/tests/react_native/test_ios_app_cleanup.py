"""Hardware-free contract tests for the formal iOS App release used before a run is admitted.

The device stands in for the phone (`support/coredevice_fake.py`). The one answer that was
observed on a real iPhone, and what the lane must do with it, lives in
`tools/tests/observed/test_ios_app_cleanup.py`.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.react_native import ios_app_cleanup as cleanup
from tools.react_native import ios_sdk
from tools.tests.react_native.support.coredevice_fake import (
    BUNDLE,
    DEVICE,
    DIRECTORY,
    Device,
    app,
    release,
    running,
)


class InstallationDirectoryTests(unittest.TestCase):
    def test_the_devices_own_installation_is_the_only_attribution(self) -> None:
        apps = [
            app(
                bundle="com.apple.Preferences",
                url="file:///Applications/Preferences.app/",
            ),
            app(),
        ]
        self.assertEqual(cleanup.installation_directory(apps, BUNDLE), DIRECTORY)

    def test_an_answered_inventory_without_this_app_has_nothing_to_release(
        self,
    ) -> None:
        others = [
            app(
                bundle="com.apple.Preferences",
                url="file:///Applications/Preferences.app/",
            )
        ]
        self.assertIsNone(cleanup.installation_directory(others, BUNDLE))

    def test_several_installations_are_refused(self) -> None:
        with self.assertRaisesRegex(cleanup.FormalAppCleanupError, "refusing to guess"):
            cleanup.installation_directory([app(), app()], BUNDLE)

    def test_an_installation_without_a_directory_is_refused(self) -> None:
        with self.assertRaises(cleanup.FormalAppCleanupError):
            cleanup.installation_directory([app(url="file:///nope")], BUNDLE)


class ReleaseTests(unittest.TestCase):
    def output(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory())) / "release"

    def test_an_app_that_is_not_installed_has_nothing_to_release(self) -> None:
        # A clean iPhone must be able to start a sequence; it used to be refused.
        device = Device(apps=[[app(bundle="com.apple.Preferences")]])
        receipt = release(device, self.output())
        self.assertEqual(
            (receipt["action"], receipt["status"]), ("not-installed", "passed")
        )
        # One answered question; no process inventory is looked at for an absent installation.
        self.assertEqual(device.kinds, ["devicectl.device.info.apps"])

    def test_a_device_that_never_answers_is_refused_and_says_so(self) -> None:
        device = Device(apps=[[], [], []])
        receipt = release(device, self.output())
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("has not answered", receipt["error"])
        # Bounded: three asks, and never a conclusion drawn from an empty one.
        self.assertEqual(device.kinds, ["devicectl.device.info.apps"] * 3)

    def test_a_running_app_is_terminated_once_and_then_stays_absent(self) -> None:
        device = Device(apps=[[app()]], processes=[[running(7)], [], []])
        receipt = release(device, self.output())
        self.assertEqual(
            (receipt["action"], receipt["terminations"]), ("terminated", 1)
        )
        self.assertEqual((receipt["pid"], receipt["status"]), (7, "passed"))
        self.assertEqual(device.kinds.count("devicectl.device.process.terminate"), 1)

    def test_the_official_receipt_shape_the_device_stands_in_for_is_the_real_one(
        self,
    ) -> None:
        # The stand-in must produce what the real validator accepts, or these tests prove
        # nothing about the lane.
        argv = ios_sdk.devicectl_device_apps(DEVICE, Path("unused.json"), timeout=8)
        self.assertEqual(".".join(argv[1:5]), "devicectl.device.info.apps")
        processes = ios_sdk.devicectl_processes(DEVICE, Path("unused.json"))
        self.assertEqual(".".join(processes[1:5]), "devicectl.device.info.processes")


class ProcessesInsideTests(unittest.TestCase):
    def test_only_the_installation_itself_matches(self) -> None:
        processes = [
            running(7),
            # A different container of the same bundle is not this installation.
            {
                "executable": "file:///private/var/containers/Bundle/Application/9999/candidate.app/BleHubBlengHost",
                "processIdentifier": 8,
            },
            # A nested executable inside the App is not the App's own process.
            {
                "executable": DIRECTORY + "/PlugIns/Widget.appex/Widget",
                "processIdentifier": 9,
            },
            {
                "executable": "file:///Applications/Preferences.app/Preferences",
                "processIdentifier": 10,
            },
        ]
        self.assertEqual(cleanup.processes_inside(processes, DIRECTORY), [7])

    def test_a_malformed_inventory_is_a_refusal(self) -> None:
        for processes in (
            ["not-a-row"],
            [{"executable": DIRECTORY + "/BleHubBlengHost", "processIdentifier": 0}],
            [{"executable": DIRECTORY + "/BleHubBlengHost"}],
        ):
            with (
                self.subTest(processes=processes),
                self.assertRaises(cleanup.FormalAppCleanupError),
            ):
                cleanup.processes_inside(processes, DIRECTORY)


class ContractTests(unittest.TestCase):
    def test_the_module_never_claims_more_than_it_proves(self) -> None:
        self.assertIn("never claims resource-zero", cleanup.__doc__)
        self.assertIn("at most one process", cleanup.__doc__)
        self.assertEqual(cleanup.DEFAULT_BUNDLE, BUNDLE)


if __name__ == "__main__":
    unittest.main()
