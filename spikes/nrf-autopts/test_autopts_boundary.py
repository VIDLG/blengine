from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autopts_boundary import (
    AutoPtsBoundaryError,
    AutoPtsConfig,
    AutoPtsProcess,
    DeviceLock,
    Readiness,
)


class FakeProcess:
    def __init__(self, pid: int, running: bool = True) -> None:
        self.pid = pid
        self.running = running
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return None if self.running else 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        self.running = False
        return 0


class AutoPtsConfigTests(unittest.TestCase):
    def test_config_requires_existing_checkout_and_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(AutoPtsBoundaryError):
                AutoPtsConfig(root / "missing", ("autopts",)).validate()
            with self.assertRaises(AutoPtsBoundaryError):
                AutoPtsConfig(root, ()).validate()

    def test_config_rejects_unbounded_stop(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaises(AutoPtsBoundaryError),
        ):
            AutoPtsConfig(Path(temporary), ("autopts",), stop_timeout=0).validate()


class ReadinessTests(unittest.TestCase):
    def test_process_running_is_not_ready_until_marker(self) -> None:
        readiness = Readiness("BTP_READY ")
        readiness.feed("process started")
        with self.assertRaises(AutoPtsBoundaryError):
            readiness.require_ready()
        readiness.feed("BTP_READY profile=basic")
        readiness.require_ready()

    def test_failure_marker_is_terminal(self) -> None:
        readiness = Readiness("BTP_READY ")
        readiness.feed("BTP_FAILURE transport closed")
        with self.assertRaisesRegex(AutoPtsBoundaryError, "transport closed"):
            readiness.require_ready()
        readiness.feed("BTP_READY profile=basic")
        with self.assertRaisesRegex(AutoPtsBoundaryError, "transport closed"):
            readiness.require_ready()


class DeviceLockTests(unittest.TestCase):
    def test_lock_is_exclusive_and_release_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nrf.lock"
            first = DeviceLock(path)
            second = DeviceLock(path)
            first.acquire()
            with self.assertRaises(AutoPtsBoundaryError):
                second.acquire()
            first.release()
            first.release()
            second.acquire()
            second.release()


class AutoPtsProcessTests(unittest.TestCase):
    def test_start_uses_checkout_and_new_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process = FakeProcess(1234)
            config = AutoPtsConfig(root, ("autopts", "--isolated"))
            with patch(
                "autopts_boundary.subprocess.Popen", return_value=process
            ) as popen:
                self.assertIs(config, AutoPtsProcess(config).config)
                controller = AutoPtsProcess(config)
                self.assertIs(controller.start(), process)
                self.assertEqual(popen.call_args.kwargs["cwd"], root)
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
                self.assertEqual(popen.call_args.args[0], ["autopts", "--isolated"])

    def test_stop_is_idempotent_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            process = FakeProcess(1234)
            controller = AutoPtsProcess(AutoPtsConfig(Path(temporary), ("autopts",)))
            controller.process = process
            with patch("autopts_boundary.os.killpg") as killpg:
                controller.stop()
                controller.stop()
            killpg.assert_called_once_with(1234, unittest.mock.ANY)
            self.assertEqual(process.wait_calls, [5.0])
            self.assertTrue(controller.closed)

    def test_context_manager_closes_after_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            process = FakeProcess(1234)
            with (
                patch("autopts_boundary.subprocess.Popen", return_value=process),
                patch("autopts_boundary.os.killpg"),
            ):
                with AutoPtsProcess(
                    AutoPtsConfig(Path(temporary), ("autopts",))
                ) as controller:
                    self.assertIs(controller.process, process)
                self.assertTrue(controller.closed)


if __name__ == "__main__":
    unittest.main()
