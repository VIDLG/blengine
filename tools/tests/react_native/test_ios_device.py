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
                "tools.react_native.ios_device.time.monotonic",
                side_effect=[100.0, 100.0, 101.0, 102.0],
            ),
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
        self.assertEqual(
            [call.kwargs["timeout"] for call in run.call_args_list], [30, 30]
        )
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["cwd"], Path("."))
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(call.kwargs["capture_output"])
            self.assertTrue(call.kwargs["text"])
            self.assertFalse(call.kwargs["check"])

    def test_commands_share_remaining_budget_and_keep_per_command_cap(self) -> None:
        for timeout, times, expected in (
            (5.0, [100.0, 100.25, 102.5, 104.5], [4.75, 2.5]),
            (45, [100.0, 100.0, 125.0, 140.0], [30.0, 20.0]),
        ):
            with (
                self.subTest(timeout=timeout),
                patch(
                    "tools.react_native.ios_device.time.monotonic", side_effect=times
                ),
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
                ios_device.require_ready_unlocked_device(
                    UDID, cwd=Path("."), timeout=timeout
                )
                self.assertEqual(
                    [call.kwargs["timeout"] for call in run.call_args_list], expected
                )

    def test_rejects_invalid_timeout_before_device_commands(self) -> None:
        for timeout in (
            True,
            False,
            0,
            -1,
            float("nan"),
            float("inf"),
            -float("inf"),
            "1",
            None,
        ):
            with (
                self.subTest(timeout=timeout),
                patch("tools.react_native.ios_device.shutil.which") as which,
                patch("tools.react_native.ios_device.subprocess.run") as run,
                self.assertRaisesRegex(
                    ios_device.IosDevicePreconditionError, "finite and positive"
                ),
            ):
                try:
                    ios_device.require_ready_unlocked_device(
                        UDID, cwd=Path("."), timeout=cast(float, timeout)
                    )
                finally:
                    which.assert_not_called()
                    run.assert_not_called()

    def test_does_not_submit_commands_at_or_after_deadline(self) -> None:
        for times, expected_calls in (
            ([100.0, 101.0], 0),
            ([100.0, 101.25], 0),
            ([100.0, 100.0, 101.0], 1),
            ([100.0, 100.0, 101.25], 1),
        ):
            with (
                self.subTest(times=times),
                patch(
                    "tools.react_native.ios_device.time.monotonic", side_effect=times
                ),
                patch(
                    "tools.react_native.ios_device.shutil.which",
                    return_value="/usr/bin/xcrun",
                ),
                patch(
                    "tools.react_native.ios_device.subprocess.run",
                    return_value=subprocess.CompletedProcess(["xcrun"], 0, LISTED, ""),
                ) as run,
            ):
                with self.assertRaisesRegex(
                    ios_device.IosDevicePreconditionError, "overall timeout budget"
                ):
                    ios_device.require_ready_unlocked_device(
                        UDID, cwd=Path("."), timeout=1.0
                    )
                self.assertEqual(run.call_count, expected_calls)

    def test_successful_lock_state_cannot_override_expired_host_budget(self) -> None:
        for finished in (101.0, 101.25):
            with (
                self.subTest(finished=finished),
                patch(
                    "tools.react_native.ios_device.time.monotonic",
                    side_effect=[100.0, 100.0, 100.5, finished],
                ),
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
                with self.assertRaisesRegex(
                    ios_device.IosDevicePreconditionError, "overall timeout budget"
                ):
                    ios_device.require_ready_unlocked_device(
                        UDID, cwd=Path("."), timeout=1.0
                    )
                self.assertEqual(run.call_count, 2)

    def test_subprocess_timeouts_use_precondition_error_without_retry(self) -> None:
        for command_index in (0, 1):
            error = subprocess.TimeoutExpired(["xcrun"], 1.0)
            responses = [
                subprocess.CompletedProcess(["xcrun"], 0, LISTED, "")
            ] * command_index
            with (
                self.subTest(command_index=command_index),
                patch(
                    "tools.react_native.ios_device.time.monotonic", return_value=100.0
                ),
                patch(
                    "tools.react_native.ios_device.shutil.which",
                    return_value="/usr/bin/xcrun",
                ),
                patch(
                    "tools.react_native.ios_device.subprocess.run",
                    side_effect=[*responses, error],
                ) as run,
            ):
                with self.assertRaisesRegex(
                    ios_device.IosDevicePreconditionError, "timed out"
                ) as caught:
                    ios_device.require_ready_unlocked_device(
                        UDID, cwd=Path("."), timeout=1.0
                    )
                self.assertIs(caught.exception.__cause__, error)
                self.assertEqual(run.call_count, command_index + 1)

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
