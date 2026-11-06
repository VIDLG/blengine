from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.react_native import run_android_multi_rf as runner
from tools.react_native.run_android_multi_rf import _advance_fixture_coordination


class AndroidMultiCoordinationTests(unittest.TestCase):
    def test_logcat_capture_keeps_only_runtime_markers(self) -> None:
        process = MagicMock()
        process.stdout = io.StringIO(
            "D/Other(1): noise\nI/ReactNativeJS(1): BLEHUB_NITRO_RUNTIME marker\n"
        )
        process.poll.return_value = 0
        with (
            patch(
                "tools.react_native.run_android_multi_rf.android_sdk.adb_command",
                return_value=["adb", "logcat"],
            ),
            patch(
                "tools.react_native.run_android_multi_rf.subprocess.Popen",
                return_value=process,
            ),
        ):
            capture = runner._LogcatCapture("device")
            capture.close()
            self.assertEqual(
                capture.snapshot(), "I/ReactNativeJS(1): BLEHUB_NITRO_RUNTIME marker"
            )

    def test_subscription_marker_only_advances_matching_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path_a = root / "dk_a"
            path_b = root / "dk_b"
            path_a.mkdir()
            path_b.mkdir()
            log = (
                "rf-multi:step:advertisement:passed:serviceUuid=fd000000-0000-4000-8000-000000000000\n"
                "rf-multi:step:advertisement:passed:serviceUuid=fd200000-0000-4000-8000-000000000000\n"
                "rf-multi:step:indication-enable:passed:serviceUuid=fd200000-0000-4000-8000-000000000000"
            )
            _advance_fixture_coordination(
                log,
                {
                    "fd000000-0000-4000-8000-000000000000": path_a,
                    "fd200000-0000-4000-8000-000000000000": path_b,
                },
                "indication",
            )
            self.assertFalse((path_a / "subscription-enabled").exists())
            self.assertTrue((path_b / "subscription-enabled").exists())

    def test_cycles_stop_on_first_failure_and_keep_suffixes(self) -> None:
        with (
            patch.object(
                runner,
                "run",
                side_effect=[Path("cycle-1.log"), runner.AndroidMultiRfError("boom")],
            ) as run,
            self.assertRaisesRegex(runner.AndroidMultiRfError, "boom"),
        ):
            runner.run_cycles("device", 3)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].kwargs["log_suffix"], "-cycle-1")
        self.assertEqual(run.call_args_list[1].kwargs["log_suffix"], "-cycle-2")

    def test_cycles_are_bounded(self) -> None:
        for cycles in (0, 21):
            with (
                self.subTest(cycles=cycles),
                self.assertRaisesRegex(runner.AndroidMultiRfError, "between 1 and 20"),
            ):
                runner.run_cycles("device", cycles)

    def test_fixture_exit_is_fail_fast(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "runtime.log"
            controller = MagicMock()
            controller.check_health.side_effect = (
                runner.multi_fixture.MultiFixtureError(
                    "dk_b fixture exited with status 2: subscription timeout"
                )
            )
            with self.assertRaisesRegex(runner.AndroidMultiRfError, "fixture failed"):
                runner._wait_for_runtime(
                    "device",
                    log_path,
                    "fd000000-0000-4000-8000-000000000000,fd200000-0000-4000-8000-000000000000",
                    10,
                    {},
                    "notification",
                    controller=controller,
                )
            controller.check_health.assert_called_once_with()

    def test_host_exit_after_launch_is_fail_fast(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "runtime.log"
            content = (
                "BLEHUB_NITRO_RUNTIME rf-multi:listener:registered-before-initialize"
            )
            with (
                patch.object(runner.android_sdk, "logcat", return_value=content),
                patch.object(runner.android_sdk, "runtime_alive", return_value=False),
                self.assertRaisesRegex(runner.AndroidMultiRfError, "host exited"),
            ):
                runner._wait_for_runtime(
                    "device",
                    log_path,
                    "fd000000-0000-4000-8000-000000000000,fd200000-0000-4000-8000-000000000000",
                    10,
                    {},
                    "notification",
                )
            self.assertEqual(log_path.read_text(encoding="utf-8"), content)


if __name__ == "__main__":
    unittest.main()
