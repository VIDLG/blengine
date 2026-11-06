from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path

from tools.react_native import run_ios_radio_restore as api


class StageRestoreXctestrunTests(unittest.TestCase):
    def make_source(self, root: Path, target: dict[str, object]) -> Path:
        source = root / "source.xctestrun"
        source.write_bytes(
            plistlib.dumps(
                {
                    "BleHubNitroHostUITests": {
                        "TestBundlePath": "runner.xctest",
                        **target,
                    }
                }
            )
        )
        return source

    def test_injects_the_single_settings_test_into_build_for_testing_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source(
                root,
                {"EnvironmentVariables": {"EXISTING": "value"}},
            )
            staged = api._stage_restore_xctestrun(source)
            target = plistlib.loads(staged.read_bytes())["BleHubNitroHostUITests"]
            self.assertEqual(
                target["OnlyTestIdentifiers"],
                ["BleHubNitroHostUITests/testBluetoothSettingsOffAndRestore"],
            )
            self.assertEqual(target["SkipTestIdentifiers"], [])
            self.assertEqual(target["EnvironmentVariables"]["EXISTING"], "value")
            self.assertEqual(target["EnvironmentVariables"][api.RESTORE_FLAG], "1")
            self.assertEqual(target["EnvironmentVariables"][api.INSPECT_FLAG], "0")

    def test_rejects_a_foreign_selected_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_source(
                Path(directory),
                {
                    "OnlyTestIdentifiers": ["BleHubNitroHostUITests/otherTest"],
                    "EnvironmentVariables": {},
                },
            )
            with self.assertRaisesRegex(api.RadioRestoreError, "selected method drift"):
                api._stage_restore_xctestrun(source)


if __name__ == "__main__":
    unittest.main()
