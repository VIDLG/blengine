"""Host-only ownership and rollback tests for the RN iPhone RF gate."""

from __future__ import annotations

import io
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import final
from unittest.mock import ANY, Mock, patch

from tools.react_native import run_ios_rf_scan as gate


@final
class RfScanRunnerTests(unittest.TestCase):
    temp: tempfile.TemporaryDirectory[str]
    work: Path
    pid: Path
    lock: Path
    build: Path
    runtime_exit: Mock | None = None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name) / "nrf52840"
        self.pid = self.work / "fixture.pid"
        self.lock = self.work / "rn-ios-scan.lock"
        self.build = Path(self.temp.name) / "build"
        self.build.mkdir()
        for target, name, value in (
            (gate, "HOST_BUILD", self.build),
            (gate.provider, "WORK_ROOT", self.work),
            (gate.provider, "PID_FILE", self.pid),
            (gate.provider, "LOG_FILE", self.work / "fixture.log"),
            (gate, "LOCK", self.lock),
        ):
            replacement = patch.object(target, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        legacy_start = patch.object(
            gate.provider,
            "direct_start",
            side_effect=AssertionError("runner must start the fixture via subprocess"),
        )
        legacy_start.start()
        self.addCleanup(legacy_start.stop)
        runtime_exit = patch.object(gate, "wait_for_runtime_exit", return_value=None)
        self.runtime_exit = runtime_exit.start()
        self.addCleanup(runtime_exit.stop)

    def assert_fixture_command(
        self, args: list[str], kwargs: dict[str, object], action: str
    ) -> None:
        self.assertEqual(
            args,
            [gate.sys.executable, "-m", "tests.peripherals.nrf52840.provider", action],
        )
        self.assertEqual(
            kwargs,
            {
                "cwd": gate.PROJECT_ROOT,
                "check": True,
                "timeout": (
                    gate.provider.DEFAULT_READY_TIMEOUT + 15
                    if action == "direct-start"
                    else 25
                ),
            },
        )

    def test_reads_provider_pid_and_pgid_marker(self) -> None:
        self.work.mkdir()
        self.pid.write_text("1234 1234\n", encoding="utf-8")
        self.assertEqual(gate.fixture_pid(), 1234)

    def test_existing_pid_even_if_stale_is_never_started_or_stopped(self) -> None:
        previous_handler = signal.getsignal(signal.SIGTERM)
        self.work.mkdir()
        self.pid.write_text("999999\n", encoding="utf-8")
        with (
            patch.object(gate.subprocess, "run") as command,
            self.assertRaisesRegex(RuntimeError, "even if stale"),
        ):
            gate.run("device")
        command.assert_not_called()
        self.assertEqual(self.pid.read_text(encoding="utf-8"), "999999\n")
        self.assertFalse(self.lock.exists())
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_sigterm_with_existing_pid_never_stops_user_fixture(self) -> None:
        previous_handler = signal.getsignal(signal.SIGTERM)
        self.work.mkdir()
        self.pid.write_text("999999\n", encoding="utf-8")
        original_exists = Path.exists

        def interrupted_exists(path: Path) -> bool:
            if path == self.pid:
                self.assertIs(
                    signal.getsignal(signal.SIGTERM), gate.interrupt_on_sigterm
                )
                signal.raise_signal(signal.SIGTERM)
            return original_exists(path)

        with (
            patch.object(Path, "exists", interrupted_exists),
            patch.object(gate.subprocess, "run") as command,
            self.assertRaisesRegex(InterruptedError, "SIGTERM"),
        ):
            gate.run("device")
        command.assert_not_called()
        self.assertEqual(self.pid.read_text(encoding="utf-8"), "999999\n")
        self.assertFalse(self.lock.exists())
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_sigterm_during_install_stops_owned_fixture_and_restores_handler(
        self,
    ) -> None:
        previous_handler = signal.getsignal(signal.SIGTERM)
        commands: list[list[str]] = []

        def command(args: list[str], **kwargs: object) -> None:
            self.assertTrue(self.lock.is_dir())
            commands.append(args)
            if args[-1] == "direct-start":
                self.assert_fixture_command(args, kwargs, "direct-start")
                self.pid.write_text("1234", encoding="utf-8")
            elif args[-1] == "stop":
                self.assert_fixture_command(args, kwargs, "stop")
                self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
                self.pid.unlink()
            else:
                self.assertIn("devicectl", args)
                handler = signal.getsignal(signal.SIGTERM)
                self.assertIs(handler, gate.interrupt_on_sigterm)
                signal.raise_signal(signal.SIGTERM)

        with (
            patch.object(gate.subprocess, "run", side_effect=command),
            self.assertRaisesRegex(InterruptedError, "SIGTERM"),
        ):
            gate.run("device")
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[0][-1], "direct-start")
        self.assertIn("devicectl", commands[1])
        self.assertEqual(commands[2][-1], "stop")
        self.assertFalse(self.lock.exists())
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_partial_start_cleans_up_only_its_pid_and_preserves_failure(self) -> None:
        def command(args: list[str], **kwargs: object) -> None:
            self.assertTrue(self.lock.is_dir())
            if args[-1] == "direct-start":
                self.assert_fixture_command(args, kwargs, "direct-start")
                self.pid.write_text("1234", encoding="utf-8")
                raise subprocess.CalledProcessError(2, args)
            self.assert_fixture_command(args, kwargs, "stop")
            self.pid.unlink()

        with (
            patch.object(gate.subprocess, "run", side_effect=command) as run,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            gate.run("device")
        self.assertEqual(
            [call.args[0][-1] for call in run.call_args_list], ["direct-start", "stop"]
        )
        self.assertFalse(self.lock.exists())

    def test_success_runs_install_rf_launch_checker_then_bounded_stop(self) -> None:
        commands: list[list[str]] = []

        def command(args: list[str], **kwargs: object) -> None:
            self.assertTrue(self.lock.is_dir())
            commands.append(args)
            if args[-1] == "direct-start":
                self.assert_fixture_command(args, kwargs, "direct-start")
                self.pid.write_text("1234", encoding="utf-8")
                return
            self.assertTrue(self.pid.exists())
            if args[-1] == "stop":
                self.assert_fixture_command(args, kwargs, "stop")
                self.pid.unlink()
            elif "tools.react_native.run_ios_device_runtime" in " ".join(args):
                (self.build / "device-rf-scan.attempt.log").write_text(
                    "validated RF result", encoding="utf-8"
                )

        with patch.object(gate.subprocess, "run", side_effect=command):
            gate.run("iphone-udid")
        self.assertEqual(len(commands), 5)
        self.assertEqual(commands[0][-1], "direct-start")
        self.assertIn("devicectl", commands[1])
        self.assertIn("device-rf-install.json", " ".join(commands[1]))
        self.assertIn("--seconds", commands[2])
        self.assertIn("210", commands[2])
        self.assertIn(gate.SERVICE_UUID, commands[2])
        self.assertEqual(
            commands[2][1:3], ["-m", "tools.react_native.run_ios_device_runtime"]
        )
        self.assertIn("check_runtime_log.py", commands[3][1])
        self.assertIn(str(self.build / "device-rf-scan.attempt.log"), commands[2])
        self.assertIn(str(self.build / "device-rf-scan.attempt.log"), commands[3])
        self.assertEqual(
            (self.build / "device-rf-scan.log").read_text(encoding="utf-8"),
            "validated RF result",
        )
        self.assertFalse((self.build / "device-rf-scan.attempt.log").exists())
        self.assertEqual(commands[4][-1], "stop")
        self.assertFalse(self.lock.exists())

    def test_connect_uses_separate_log_and_mode(self) -> None:
        commands: list[list[str]] = []

        def command(args: list[str], **kwargs: object) -> None:
            commands.append(args)
            if args[-1] == "direct-start":
                self.assert_fixture_command(args, kwargs, "direct-start")
                self.pid.write_text("1234", encoding="utf-8")
            elif args[-1] == "stop":
                self.assert_fixture_command(args, kwargs, "stop")
                self.pid.unlink()
            elif "tools.react_native.run_ios_device_runtime" in " ".join(args):
                (self.build / "device-rf-connect.attempt.log").write_text(
                    "connection evidence", encoding="utf-8"
                )

        with patch.object(gate.subprocess, "run", side_effect=command):
            gate.run("iphone-udid", connect=True)
        self.assertEqual(len(commands), 5)
        self.assertEqual(commands[0][-1], "direct-start")
        self.assertIn("--rf-connect-service-uuid", commands[2])
        self.assertIn("--rf-connect-service-uuid", commands[3])
        self.assertIn(str(self.build / "device-rf-connect.attempt.log"), commands[2])
        self.assertEqual(commands[4][-1], "stop")
        self.assertEqual(
            (self.build / "device-rf-connect.log").read_text(encoding="utf-8"),
            "connection evidence",
        )
        self.assertFalse((self.build / "device-rf-scan.log").exists())
        self.assertFalse(self.lock.exists())

    def test_rssi_uses_separate_mode_and_checks_before_publishing_log(self) -> None:
        commands: list[list[str]] = []

        def command(args: list[str], **kwargs: object) -> None:
            commands.append(args)
            if args[-1] == "direct-start":
                self.assert_fixture_command(args, kwargs, "direct-start")
                self.pid.write_text("1234", encoding="utf-8")
            elif args[-1] == "stop":
                self.assert_fixture_command(args, kwargs, "stop")
                self.pid.unlink()
            elif "tools.react_native.run_ios_device_runtime" in " ".join(args):
                (self.build / "device-rf-rssi.attempt.log").write_text(
                    "RSSI evidence", encoding="utf-8"
                )

        with patch.object(gate.subprocess, "run", side_effect=command):
            gate.run("iphone-udid", rssi=True)
        self.assertEqual(len(commands), 5)
        self.assertIn("--rf-rssi-service-uuid", commands[2])
        self.assertIn("--rf-rssi-service-uuid", commands[3])
        self.assertEqual(
            (self.build / "device-rf-rssi.log").read_text(encoding="utf-8"),
            "RSSI evidence",
        )
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.pid.exists())

    def test_rf_reload_waits_for_fixture_disconnect_before_second_generation(
        self,
    ) -> None:
        commands: list[list[str]] = []
        steps: list[str] = []
        attempt = self.build / "device-rf-reload.attempt.log"
        runtime = Mock()
        runtime.wait.return_value = 0
        runtime.poll.return_value = None

        def command(args: list[str], **_kwargs: object) -> None:
            commands.append(args)
            if "direct-start" in args:
                self.pid.write_text("1234 1234\n", encoding="utf-8")
            elif args[-1] == "stop":
                self.pid.unlink()

        def marker(
            _log: Path,
            value: str,
            _runtime: object,
            _timeout: float,
            occurrence: int = 1,
            **_kwargs: object,
        ) -> None:
            steps.append(f"{value}:{occurrence}")
            if value == "rf-reload:active:" and occurrence == 2:
                attempt.write_text(
                    "BLEHUB_NITRO_RUNTIME rf-reload:notification-enable:passed:first\n"
                    "BLEHUB_NITRO_RUNTIME rf-reload:notification-enable:passed:second\n",
                    encoding="utf-8",
                )

        def fixture(path: Path, _timeout: float, **_kwargs: object) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("ready\n", encoding="ascii")
            steps.append(path.name)

        with (
            patch.object(gate.subprocess, "run", side_effect=command),
            patch.object(gate.subprocess, "Popen", return_value=runtime) as launch,
            patch.object(gate, "wait_for_log_marker", side_effect=marker),
            patch.object(gate, "wait_for_coordination_marker", side_effect=fixture),
        ):
            gate.run("iphone-udid", rf_reload=True)
        self.assertIn("host-close", commands[0])
        self.assertIn("--rf-reload-service-uuid", launch.call_args.args[0])
        self.assertIn("--rf-reload-service-uuid", commands[-2])
        self.assertLess(
            steps.index("host-disconnected"),
            steps.index("rf-reload:notification-enable:passed::2"),
        )
        published = (self.build / "device-rf-reload.log").read_text(encoding="utf-8")
        self.assertIn(
            "rf-reload:notification-enable:passed:first\nBLEHUB_NITRO_RUNTIME rf-reload:fixture-host-disconnected\nBLEHUB_NITRO_RUNTIME rf-reload:notification-enable:passed:second",
            published,
        )
        self.assertTrue(
            published.endswith("BLEHUB_NITRO_RUNTIME rf-reload:host-complete:passed\n")
        )
        self.assertFalse(self.pid.exists())
        self.assertFalse(self.lock.exists())

    def test_ten_cycle_recovery_uses_separate_fixture_and_waits_for_all_cycles(
        self,
    ) -> None:
        steps: list[str] = []
        attempt = self.build / "device-rf-recovery-ten.attempt.log"
        runtime = Mock()
        runtime.wait.return_value = 0
        runtime.poll.return_value = None

        def command(args: list[str], **_kwargs: object) -> None:
            if "direct-start" in args:
                self.assertIn("recovery-ten", args)
                self.pid.write_text("1234 1234\n", encoding="utf-8")
            elif args[-1] == "stop":
                self.pid.unlink()
            elif "check_runtime_log.py" in " ".join(args):
                self.assertIn("--rf-recovery-ten-service-uuid", args)

        def marker(
            _log: Path, value: str, _runtime: object, _timeout: float, **_kwargs: object
        ) -> None:
            steps.append(value)
            if value == "rf-recovery-ten:cycle:9:cycle:passed:":
                attempt.write_text("verified", encoding="utf-8")

        def fixture(path: Path, _timeout: float, **_kwargs: object) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("ready\n", encoding="ascii")
            steps.append(path.name)

        with (
            patch.object(gate.subprocess, "run", side_effect=command),
            patch.object(gate.subprocess, "Popen", return_value=runtime) as launch,
            patch.object(gate, "wait_for_log_marker", side_effect=marker),
            patch.object(gate, "wait_for_coordination_marker", side_effect=fixture),
        ):
            gate.run("iphone-udid", recovery=True, recovery_cycles=10)
        self.assertIn("--rf-recovery-ten-service-uuid", launch.call_args.args[0])
        assert self.runtime_exit is not None
        self.runtime_exit.assert_called_once_with(runtime, 925, progress=ANY)
        self.assertEqual(steps.count("fixture-ready"), 10)
        self.assertEqual(steps.count("recovery-ready"), 10)
        self.assertEqual(
            (self.build / "device-rf-recovery-ten.log").read_text(encoding="utf-8"),
            "verified",
        )
        self.assertFalse(self.pid.exists())
        self.assertFalse(self.lock.exists())

    def test_recovery_coordinates_three_cycles_before_publishing_log(self) -> None:
        commands: list[list[str]] = []
        steps: list[str] = []
        coordination = self.build / "device-rf-recovery-coordination"
        attempt = self.build / "device-rf-recovery.attempt.log"
        runtime = Mock()
        runtime.wait.return_value = 0
        runtime.poll.return_value = None

        def command(args: list[str], **_kwargs: object) -> None:
            commands.append(args)
            if "direct-start" in args:
                self.pid.write_text("1234 1234\n", encoding="utf-8")
            elif args[-1] == "stop":
                self.pid.unlink()

        def marker(
            _log: Path, value: str, _runtime: object, _timeout: float, **_kwargs: object
        ) -> None:
            steps.append(value)
            if value.endswith(":cycle:passed:") and value.startswith(
                "rf-recovery:cycle:2:"
            ):
                attempt.write_text("verified", encoding="utf-8")

        def fixture(path: Path, _timeout: float, **_kwargs: object) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("ready\n", encoding="ascii")
            steps.append(path.name)

        with (
            patch.object(gate.subprocess, "run", side_effect=command),
            patch.object(gate.subprocess, "Popen", return_value=runtime) as launch,
            patch.object(gate, "wait_for_log_marker", side_effect=marker),
            patch.object(gate, "wait_for_coordination_marker", side_effect=fixture),
        ):
            gate.run("iphone-udid", recovery=True)
        self.assertIn("recovery", commands[0])
        self.assertIn("--rf-recovery-service-uuid", launch.call_args.args[0])
        self.assertIn("--rf-recovery-service-uuid", commands[-2])
        assert self.runtime_exit is not None
        self.runtime_exit.assert_called_once_with(runtime, 475, progress=ANY)
        self.assertEqual(steps.count("fixture-ready"), 3)
        self.assertEqual(steps.count("recovery-ready"), 3)
        for cycle in range(3):
            directory = coordination / f"cycle-{cycle}"
            self.assertFalse(directory.exists())  # cleaned even after success
            self.assertIn(f"rf-recovery:cycle:{cycle}:cycle:passed:", steps)
        self.assertEqual(
            (self.build / "device-rf-recovery.log").read_text(encoding="utf-8"),
            "verified",
        )
        self.assertFalse(self.pid.exists())
        self.assertFalse(self.lock.exists())

    def test_failed_launch_preserves_last_verified_rf_log(self) -> None:
        previous = self.build / "device-rf-scan.log"
        previous.write_text("previous verified RF result", encoding="utf-8")

        def command(args: list[str], **kwargs: object) -> None:
            if args[-1] == "direct-start":
                self.assert_fixture_command(args, kwargs, "direct-start")
                self.pid.write_text("1234", encoding="utf-8")
            elif args[-1] == "stop":
                self.assert_fixture_command(args, kwargs, "stop")
                self.pid.unlink()
            elif "tools.react_native.run_ios_device_runtime" in " ".join(args):
                (self.build / "device-rf-scan.attempt.log").write_text(
                    "device locked", encoding="utf-8"
                )
                raise subprocess.CalledProcessError(1, args)

        with (
            patch.object(gate.subprocess, "run", side_effect=command),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            gate.run("iphone-udid")
        self.assertEqual(
            previous.read_text(encoding="utf-8"), "previous verified RF result"
        )
        self.assertEqual(
            (self.build / "device-rf-scan.attempt.log").read_text(encoding="utf-8"),
            "device locked",
        )
        self.assertFalse(self.lock.exists())

    def test_checker_failure_keeps_previous_published_log(self) -> None:
        published = self.build / "device-rf-scan.log"
        published.write_text("previous verified RF result", encoding="utf-8")
        attempt = self.build / "device-rf-scan.attempt.log"

        def command(args: list[str], **_kwargs: object) -> None:
            if args[-1] == "direct-start":
                self.pid.write_text("1234", encoding="utf-8")
            elif args[-1] == "stop":
                self.pid.unlink()
            elif "tools.react_native.run_ios_device_runtime" in " ".join(args):
                attempt.write_text("unverified result", encoding="utf-8")
            elif "check_runtime_log.py" in " ".join(args):
                raise subprocess.CalledProcessError(1, args)

        with (
            patch.object(gate.subprocess, "run", side_effect=command),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            gate.run("iphone-udid")
        self.assertEqual(
            published.read_text(encoding="utf-8"), "previous verified RF result"
        )
        self.assertEqual(attempt.read_text(encoding="utf-8"), "unverified result")
        self.assertFalse(self.pid.exists())
        self.assertFalse(self.lock.exists())

    def test_changed_pid_is_not_stopped_after_install_failure(self) -> None:
        def command(args: list[str], **kwargs: object) -> None:
            if args[-1] == "direct-start":
                self.assert_fixture_command(args, kwargs, "direct-start")
                self.pid.write_text("1234", encoding="utf-8")
                return
            self.assertIn("devicectl", args)
            self.pid.write_text("5678", encoding="utf-8")
            raise subprocess.CalledProcessError(2, args)

        with (
            patch.object(gate.subprocess, "run", side_effect=command) as run,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            gate.run("device")
        self.assertEqual(len(run.call_args_list), 2)
        self.assertEqual(run.call_args_list[0].args[0][-1], "direct-start")
        self.assertIn("devicectl", run.call_args_list[1].args[0])
        self.assertEqual(self.pid.read_text(encoding="utf-8"), "5678")
        self.assertFalse(self.lock.exists())

    def test_existing_rn_lock_does_not_touch_fixture(self) -> None:
        self.lock.mkdir(parents=True)
        with (
            patch.object(gate.subprocess, "run") as command,
            self.assertRaisesRegex(RuntimeError, "already running"),
        ):
            gate.run("device")
        command.assert_not_called()
        self.assertTrue(self.lock.is_dir())


@final
class ProgressReporterTests(unittest.TestCase):
    def test_heartbeat_is_sparse_and_phase_output_is_immediate(self) -> None:
        output = io.StringIO()
        now = [0.0]
        progress = gate.ProgressReporter("io", stream=output, clock=lambda: now[0])
        progress.phase_started("scan")
        for tick in (0.2, 14.9):
            now[0] = tick
            progress.waiting("advertisement")
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        now[0] = 15.0
        progress.waiting("advertisement")
        self.assertIn("waiting=advertisement elapsed=15s", output.getvalue())
        now[0] = 15.1
        progress.waiting("advertisement")
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        progress.observed("rf:scan:passed:")
        progress.phase_passed("scan")
        self.assertIn("phase=scan status=passed phase_elapsed=15s", output.getvalue())
        self.assertIn("last_marker=none", output.getvalue())
        now[0] = 30.0
        progress.waiting("next-phase")
        self.assertIn("last_marker=rf:scan:passed:", output.getvalue())

    def test_all_structured_runtime_failures_abort_before_success_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "attempt.log"
            runtime = Mock()
            runtime.poll.return_value = None
            for marker in (
                "rf:step:notification-value:failed:timeout",
                "rf-gatt:failure:bad-database",
                "rf-reload:failed:close",
                "rf-recovery-ten:cycle:3:failed:disconnect",
                "rf:step:notification-value:failure:timeout",
            ):
                with self.subTest(marker=marker):
                    log.write_text(
                        f"console prefix: BLEHUB_NITRO_RUNTIME {marker}\n"
                        "BLEHUB_NITRO_RUNTIME rf:step:close:passed:ok\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(RuntimeError, "RN RF gate failed"):
                        gate.wait_for_log_marker(
                            log, "rf:step:close:passed:", runtime, 1
                        )
            runtime.poll.assert_not_called()

    def test_coordination_checks_failure_and_runtime_exit_even_with_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture-ready"
            path.write_text("ready\n", encoding="ascii")
            log = Path(directory) / "attempt.log"
            runtime = Mock()
            runtime.args = ["runtime"]
            runtime.poll.return_value = None
            output = io.StringIO()
            progress = gate.ProgressReporter("recovery", stream=output)
            log.write_text(
                "console: BLEHUB_NITRO_RUNTIME rf-recovery:cycle:0:failure:closed\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "RN RF gate failed"):
                gate.wait_for_coordination_marker(
                    path, 1, runtime=runtime, log_path=log, progress=progress
                )
            runtime.poll.assert_not_called()
            self.assertEqual(progress.last_marker, "none")
            log.write_text("", encoding="utf-8")
            runtime.poll.return_value = 3
            with self.assertRaises(subprocess.CalledProcessError):
                gate.wait_for_coordination_marker(
                    path, 1, runtime=runtime, log_path=log, progress=progress
                )
            runtime.poll.return_value = None
            gate.wait_for_coordination_marker(
                path, 1, runtime=runtime, log_path=log, progress=progress
            )
            self.assertEqual(progress.last_marker, "coordination:fixture-ready")

    def test_coordination_marker_allows_bounded_zero_exit_flush(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "value-applied"
            log = root / "attempt.log"
            log.write_text("", encoding="utf-8")
            runtime = Mock()
            runtime.args = ["runtime"]
            polls = iter((0, 0))

            def poll() -> int:
                result = next(polls)
                if result == 0 and not path.exists():
                    path.write_text("ready\n", encoding="ascii")
                return result

            runtime.poll.side_effect = poll
            progress = gate.ProgressReporter("reload", stream=io.StringIO())
            gate.wait_for_coordination_marker(
                path, 1, runtime=runtime, log_path=log, progress=progress
            )
            self.assertEqual(progress.last_marker, "coordination:value-applied")

    def test_log_marker_occurrence_records_only_observed_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "attempt.log"
            log.write_text(
                "BLEHUB_NITRO_RUNTIME rf-reload:active:first\n"
                "BLEHUB_NITRO_RUNTIME rf-reload:active:second\n",
                encoding="utf-8",
            )
            runtime = Mock()
            progress = gate.ProgressReporter("reload", stream=io.StringIO())
            gate.wait_for_log_marker(
                log, "rf-reload:active:", runtime, 1, occurrence=2, progress=progress
            )
            runtime.poll.assert_not_called()
            self.assertEqual(progress.last_marker, "rf-reload:active:#2")

    def test_waits_report_heartbeat_and_runtime_exit_is_bounded(self) -> None:
        output = io.StringIO()
        now = [0.0]
        progress = gate.ProgressReporter(
            "recovery", stream=output, clock=lambda: now[0]
        )
        runtime = Mock()
        runtime.args = ["runtime"]
        runtime.poll.side_effect = [None, None, 0]
        with (
            patch.object(gate.time, "monotonic", side_effect=[0, 0, 0.2, 0.4]),
            patch.object(gate.time, "sleep"),
        ):
            gate.wait_for_runtime_exit(runtime, 1, progress=progress)
        self.assertEqual(output.getvalue(), "")  # no output per poll
        runtime.poll.side_effect = None
        runtime.poll.return_value = 2
        with self.assertRaises(subprocess.CalledProcessError):
            gate.wait_for_runtime_exit(runtime, 1, progress=progress)
        self.assertIn("reason=runtime-exit:2", output.getvalue())
        runtime.poll.return_value = None
        with (
            patch.object(gate.time, "monotonic", side_effect=[0, 0, 1]),
            patch.object(gate.time, "sleep"),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            gate.wait_for_runtime_exit(runtime, 1, progress=progress)
        self.assertIn("status=failed reason=timeout:runtime-exit:1s", output.getvalue())


if __name__ == "__main__":
    unittest.main()
