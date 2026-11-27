"""Device-observed regressions for the formal iOS App release.

Each test replays what an iPhone actually answered to `devicectl`, through the same
validator the lane uses. The phone stands in through `support/coredevice_fake.py`.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.react_native import ios_app_cleanup as cleanup
from tools.react_native import ios_sdk
from tools.tests.react_native.support.coredevice_fake import (
    BUNDLE,
    DEVICE,
    Device,
    app,
    release,
)

# What `devicectl device info apps --timeout 8` wrote on the device, verbatim except for the
# two fields a receipt carries about the host rather than about the phone: its environment
# entry and its own output path.
UNANSWERED_APPS_QUERY = {
    "info": {
        "arguments": [
            "devicectl",
            "device",
            "info",
            "apps",
            "--device",
            DEVICE,
            "--quiet",
            "--timeout",
            "8",
            "--json-output",
            "<the receipt's own path>",
        ],
        "commandType": "devicectl.device.info.apps",
        "outcome": "success",
    },
    "result": {
        "apps": [],
        "defaultAppsIncluded": False,
        "deviceIdentifier": "130256F7-7150-5A81-A906-6806AAFBFADA",
        "hiddenAppsIncluded": False,
        "internalAppsIncluded": False,
        "removableAppsIncluded": True,
    },
}


class ObservedIphoneAppReleaseTests(unittest.TestCase):
    # Trigger: the first `devicectl device info apps` after a new CoreDevice tunnel, asked
    #   with an 8 s budget, while this very App was installed and running (PID 4647).
    # Symptom: the lane refused to start a sequence with "device reports 0 installations".
    #   Read the other way, the same answer would have released nothing while the App ran.
    # Provenance: iPhone 14 Plus `00008110-000C61160C39401E`, iOS 27.2.1, 2026-11-24;
    #   `.work/ios-native-sequence/2026-11-24-a/scan/release/01-apps.json`.
    def test_regression_iphone_coredevice_empty_app_inventory_is_no_answer(
        self,
    ) -> None:
        receipt = (
            Path(self.enterContext(tempfile.TemporaryDirectory())) / "01-apps.json"
        )
        receipt.write_text(json.dumps(UNANSWERED_APPS_QUERY))
        # A valid, successful official answer ...
        result = ios_sdk.official_result(receipt, "devicectl.device.info.apps", DEVICE)
        self.assertEqual(result["apps"], [])
        # ... that says nothing: it is refused, never read as "nothing is installed".
        with self.assertRaisesRegex(cleanup.FormalAppCleanupError, "has not answered"):
            cleanup.installation_directory(result["apps"], BUNDLE)

    # Trigger: that empty answer on the first ask, a real inventory on the second (nine Apps,
    #   ours among them).
    # Symptom: none after the fix. Before it a sequence could not start on this phone at all.
    # Provenance: iPhone 14 Plus `00008110-000C61160C39401E`, iOS 27.2.1, 2026-11-24; the
    #   second query, run by hand straight after the first, returned nine Apps.
    def test_regression_iphone_coredevice_first_query_is_asked_again(self) -> None:
        # The two empty process inventories are the answers the device gave in this run; the
        # lane settles on two consecutive clean ones before it reports absence.
        device = Device(apps=[[], [app()]], processes=[[], []])
        receipt = release(
            device, Path(self.enterContext(tempfile.TemporaryDirectory())) / "release"
        )
        self.assertEqual((receipt["action"], receipt["status"]), ("absent", "passed"))
        self.assertEqual(
            device.kinds,
            ["devicectl.device.info.apps"] * 2
            + ["devicectl.device.info.processes"] * 2,
        )


if __name__ == "__main__":
    unittest.main()
