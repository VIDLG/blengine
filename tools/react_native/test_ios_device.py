from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tools.react_native import ios_device

UDID = "00008110-000C61160C39401E"
LISTED = f"vision's iPhone {UDID} available (paired) iPhone 14 Plus\n"
LOCK_STATE = """
{
  "result": {
    "passcodeRequired": false,
    "unlockedSinceBoot": true
  }
}
"""


class IosDevicePreconditionTests(unittest.TestCase):
    def test_requires_available_and_unlocked_device(self) -> None:
        with (
            patch(
                "tools.react_native.ios_device.shutil.which",
                return_value="/usr/bin/xcrun",
            ),
            patch(
                "tools.react_native.ios_device.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(["xcrun"], 0, LISTED, ""),
                    subprocess.CompletedProcess(["xcrun"], 0, LOCK_STATE, ""),
                ],
            ) as run,
        ):
            ios_device.require_ready_unlocked_device(UDID, cwd=Path("."))

        self.assertEqual(run.call_count, 2)
        self.assertIn("lockState", cast(list[str], run.call_args_list[1].args[0]))

    def test_rejects_locked_device_before_a_runtime_gate(self) -> None:
        locked = LOCK_STATE.replace(
            '"unlockedSinceBoot": true', '"unlockedSinceBoot": false'
        )
        with (
            patch(
                "tools.react_native.ios_device.shutil.which",
                return_value="/usr/bin/xcrun",
            ),
            patch(
                "tools.react_native.ios_device.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(["xcrun"], 0, LISTED, ""),
                    subprocess.CompletedProcess(["xcrun"], 0, locked, ""),
                ],
            ),
            self.assertRaisesRegex(
                ios_device.IosDevicePreconditionError,
                "is locked",
            ),
        ):
            ios_device.require_ready_unlocked_device(UDID, cwd=Path("."))


if __name__ == "__main__":
    _ = unittest.main()
