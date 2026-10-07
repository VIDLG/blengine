"""Bounded console termination tests without an iPhone or CoreDevice."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.react_native import run_ios_device_runtime as runtime


class IosConsoleTerminationTests(unittest.TestCase):
    def test_reload_notification_requires_explicit_reload_and_unique_name(self) -> None:
        notification = "dev.blehub.rn.reload.12345678-1234-1234-1234-123456789abc"
        for options in (
            ["--reload-notification-name", notification],
            ["--reload", "--reload-notification-name", "dev.blehub.rn.reload.shared"],
        ):
            with (
                self.subTest(options=options),
                patch.object(sys, "argv", ["runtime", "device", "log", *options]),
                patch.object(runtime.subprocess, "Popen") as launch,
                self.assertRaises(SystemExit),
            ):
                runtime.main()
            launch.assert_not_called()

    def test_pending_read_reload_requires_unique_notification_and_forwards_both_app_args(
        self,
    ) -> None:
        notification = "dev.blehub.rn.reload.12345678-1234-1234-1234-123456789abc"
        service = "fd100000-0000-4000-8000-000000000000"
        for options in (
            ["--rf-pending-read-reload-service-uuid", service],
            ["--rf-pending-read-reload-service-uuid", ""],
            [
                "--rf-pending-read-reload-service-uuid",
                service,
                "--reload-notification-name",
                "shared",
            ],
            [
                "--rf-rssi-service-uuid",
                service,
                "--reload-notification-name",
                notification,
            ],
        ):
            with (
                self.subTest(options=options),
                patch.object(sys, "argv", ["runtime", "device", "log", *options]),
                patch.object(runtime.subprocess, "Popen") as launch,
                self.assertRaises(SystemExit),
            ):
                runtime.main()
            launch.assert_not_called()

        original_popen = subprocess.Popen
        commands: list[list[str]] = []
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "pending.log"

            def launch(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
                commands.append(command)
                return original_popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        "print('BLEHUB_NITRO_RUNTIME rf-pending-read-reload:complete:passed', flush=True)",
                    ],
                    **kwargs,
                )

            with (
                patch.object(runtime.shutil, "which", return_value="xcrun"),
                patch.object(runtime.subprocess, "Popen", side_effect=launch),
                patch.object(
                    sys,
                    "argv",
                    [
                        "runtime",
                        "device",
                        str(log),
                        "--seconds",
                        "3",
                        "--rf-pending-read-reload-service-uuid",
                        service,
                        "--reload-notification-name",
                        notification,
                    ],
                ),
            ):
                self.assertEqual(runtime.main(), 0)
            self.assertEqual(
                commands[0][-4:],
                [
                    "--blehub-nitro-rf-pending-read-reload-service",
                    service,
                    "--blehub-nitro-rf-pending-read-reload-notification",
                    notification,
                ],
            )

    def test_reload_notification_is_forwarded_only_to_reloading_app(self) -> None:
        original_popen = subprocess.Popen
        notification = "dev.blehub.rn.reload.12345678-1234-1234-1234-123456789abc"
        commands: list[list[str]] = []
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "reload.log"

            def launch(command: list[str], **kwargs: object) -> subprocess.Popen[str]:
                commands.append(command)
                return original_popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        "print('BLEHUB_NITRO_RUNTIME reload:complete:passed', flush=True)",
                    ],
                    **kwargs,
                )

            with (
                patch.object(runtime.shutil, "which", return_value="xcrun"),
                patch.object(runtime.subprocess, "Popen", side_effect=launch),
                patch.object(
                    sys,
                    "argv",
                    [
                        "runtime",
                        "device",
                        str(log),
                        "--seconds",
                        "3",
                        "--reload",
                        "--reload-notification-name",
                        notification,
                    ],
                ),
            ):
                self.assertEqual(runtime.main(), 0)
            self.assertEqual(
                commands[0][-3:],
                [
                    "--blehub-nitro-reload",
                    "--blehub-nitro-reload-notification",
                    notification,
                ],
            )

    def test_burst_lines_are_logged_and_terminal_seen_before_silent_child_exits(
        self,
    ) -> None:
        original_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "burst.log"
            script = (
                "import os, time; "
                "os.write(1, b'console header \\xc3'); "
                "os.write(1, b'\\xa9\\nBLEHUB_NITRO_RUNTIME reload:external-ready\\n' "
                "           b'BLEHUB_NITRO_RUNTIME reload:complete:passed\\n'); "
                "time.sleep(10)"
            )

            def launch(
                _command: list[str], **kwargs: object
            ) -> subprocess.Popen[bytes]:
                return original_popen([sys.executable, "-u", "-c", script], **kwargs)

            with (
                patch.object(runtime.shutil, "which", return_value="xcrun"),
                patch.object(runtime.subprocess, "Popen", side_effect=launch),
                patch.object(
                    sys,
                    "argv",
                    ["runtime", "device", str(log), "--seconds", "3", "--reload"],
                ),
            ):
                started = time.monotonic()
                self.assertEqual(runtime.main(), 0)
                self.assertLess(time.monotonic() - started, 2)
            self.assertIn("console header é\n", log.read_text(encoding="utf-8"))
            self.assertIn(
                "BLEHUB_NITRO_RUNTIME reload:external-ready\n"
                "BLEHUB_NITRO_RUNTIME reload:complete:passed\n",
                log.read_text(encoding="utf-8"),
            )

    def test_recovery_close_ends_console_without_optional_js_summary(self) -> None:
        original_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            for option, prefix in (
                ("--rf-recovery-service-uuid", "rf-recovery"),
                ("--rf-recovery-ten-service-uuid", "rf-recovery-ten"),
            ):
                with self.subTest(option=option):
                    log = Path(temporary) / f"{prefix}.log"
                    script = (
                        f"import time; print('BLEHUB_NITRO_RUNTIME {prefix}:close:passed:"
                        "listener-removed=true:probe-closed=true', flush=True); time.sleep(30)"
                    )

                    def launch(
                        _command: list[str],
                        *,
                        script_to_run: str = script,
                        **kwargs: object,
                    ) -> subprocess.Popen[str]:
                        return original_popen(
                            [sys.executable, "-u", "-c", script_to_run], **kwargs
                        )

                    with (
                        patch.object(runtime.shutil, "which", return_value="xcrun"),
                        patch.object(runtime.subprocess, "Popen", side_effect=launch),
                        patch.object(
                            sys,
                            "argv",
                            [
                                "run_ios_device_runtime",
                                "device",
                                str(log),
                                "--seconds",
                                "4",
                                option,
                                "fixture",
                            ],
                        ),
                    ):
                        started = time.monotonic()
                        self.assertEqual(runtime.main(), 0)
                        self.assertLess(time.monotonic() - started, 4)
                    self.assertIn(
                        f"{prefix}:close:passed:", log.read_text(encoding="utf-8")
                    )

    def test_final_cycle_without_close_fails_with_short_progress_deadline(self) -> None:
        original_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "final-cycle-without-close.log"

            def launch(_command: list[str], **kwargs: object) -> subprocess.Popen[str]:
                return original_popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        "import time; print('BLEHUB_NITRO_RUNTIME rf-recovery-ten:cycle:9:cycle:passed:scanSessionId=10901', flush=True); time.sleep(30)",
                    ],
                    **kwargs,
                )

            with (
                patch.object(runtime.shutil, "which", return_value="xcrun"),
                patch.object(runtime.subprocess, "Popen", side_effect=launch),
                patch.object(runtime, "RECOVERY_CLOSE_GRACE_SECONDS", 0.2),
                patch.object(
                    sys,
                    "argv",
                    [
                        "run_ios_device_runtime",
                        "device",
                        str(log),
                        "--seconds",
                        "5",
                        "--rf-recovery-ten-service-uuid",
                        "fixture",
                    ],
                ),
            ):
                started = time.monotonic()
                self.assertEqual(runtime.main(), 1)
                self.assertLess(time.monotonic() - started, 2)
            self.assertIn("cycle:9:cycle:passed:", log.read_text(encoding="utf-8"))

    def test_mid_cycle_failure_ends_console_without_waiting_for_close(self) -> None:
        original_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "mid-cycle-failed.log"
            script = (
                "import time; print('BLEHUB_NITRO_RUNTIME "
                "rf-recovery-ten:cycle:2:gatt:failed:reason=connection', flush=True); "
                "time.sleep(30)"
            )

            def launch(_command: list[str], **kwargs: object) -> subprocess.Popen[str]:
                return original_popen([sys.executable, "-u", "-c", script], **kwargs)

            with (
                patch.object(runtime.shutil, "which", return_value="xcrun"),
                patch.object(runtime.subprocess, "Popen", side_effect=launch),
                patch.object(
                    sys,
                    "argv",
                    [
                        "run_ios_device_runtime",
                        "device",
                        str(log),
                        "--seconds",
                        "4",
                        "--rf-recovery-ten-service-uuid",
                        "fixture",
                    ],
                ),
            ):
                started = time.monotonic()
                self.assertEqual(runtime.main(), 1)
                self.assertLess(time.monotonic() - started, 4)
            self.assertIn(":gatt:failed:", log.read_text(encoding="utf-8"))

    def test_interrupted_capture_stops_owned_console(self) -> None:
        original_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "interrupted.log"
            children: list[subprocess.Popen[str]] = []

            def launch(_command: list[str], **kwargs: object) -> subprocess.Popen[str]:
                child = original_popen(
                    [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
                    **kwargs,
                )
                children.append(child)
                return child

            selector = Mock()
            selector.select.side_effect = KeyboardInterrupt
            with (
                patch.object(runtime.shutil, "which", return_value="xcrun"),
                patch.object(runtime.subprocess, "Popen", side_effect=launch),
                patch.object(
                    runtime.selectors, "DefaultSelector", return_value=selector
                ),
                patch.object(
                    sys,
                    "argv",
                    ["run_ios_device_runtime", "device", str(log), "--seconds", "4"],
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                runtime.main()
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            selector.close.assert_called_once()

    def test_rssi_mode_launches_explicit_flag_and_observes_terminal(self) -> None:
        original_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "rssi.log"
            commands: list[list[str]] = []

            def launch(command: list[str], **kwargs: object) -> subprocess.Popen[str]:
                commands.append(command)
                return original_popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        "import time; print('BLEHUB_NITRO_RUNTIME rf-rssi:passed:rssiDbm=-65', flush=True); time.sleep(30)",
                    ],
                    **kwargs,
                )

            with (
                patch.object(runtime.shutil, "which", return_value="xcrun"),
                patch.object(runtime.subprocess, "Popen", side_effect=launch),
                patch.object(
                    sys,
                    "argv",
                    [
                        "run_ios_device_runtime",
                        "device",
                        str(log),
                        "--seconds",
                        "4",
                        "--rf-rssi-service-uuid",
                        "fixture",
                    ],
                ),
            ):
                self.assertEqual(runtime.main(), 0)
            self.assertEqual(
                commands[0][-2:], ["--blehub-nitro-rf-rssi-service", "fixture"]
            )
            self.assertIn("rf-rssi:passed:", log.read_text(encoding="utf-8"))

    def test_multi_mode_launches_multi_service_flag_and_observes_terminal(self) -> None:
        original_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "multi.log"
            commands: list[list[str]] = []

            def launch(command: list[str], **kwargs: object) -> subprocess.Popen[str]:
                commands.append(command)
                return original_popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        "import time; print('BLEHUB_NITRO_RUNTIME rf-multi:passed:peripheralCount=2', flush=True); time.sleep(30)",
                    ],
                    **kwargs,
                )

            with (
                patch.object(runtime.shutil, "which", return_value="xcrun"),
                patch.object(runtime.subprocess, "Popen", side_effect=launch),
                patch.object(
                    sys,
                    "argv",
                    [
                        "run_ios_device_runtime",
                        "device",
                        str(log),
                        "--seconds",
                        "4",
                        "--rf-multi-service-uuids",
                        "service-a,service-b",
                    ],
                ),
            ):
                self.assertEqual(runtime.main(), 0)
            command = commands[0]
            service_index = command.index("--blehub-nitro-rf-multi-service")
            self.assertEqual(command[service_index + 1], "service-a,service-b")
            mode_index = command.index("--blehub-nitro-rf-multi-subscription-mode")
            self.assertEqual(command[mode_index + 1], "notification")
            self.assertIn("rf-multi:passed:", log.read_text(encoding="utf-8"))

    def test_missing_terminal_marker_fails_on_bound_without_orphan(self) -> None:
        original_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "missing.log"

            def launch(_command: list[str], **kwargs: object) -> subprocess.Popen[str]:
                return original_popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        "import time; print('not a terminal marker', flush=True); time.sleep(30)",
                    ],
                    **kwargs,
                )

            with (
                patch.object(runtime.shutil, "which", return_value="xcrun"),
                patch.object(runtime.subprocess, "Popen", side_effect=launch),
                patch.object(
                    sys,
                    "argv",
                    [
                        "run_ios_device_runtime",
                        "device",
                        str(log),
                        "--seconds",
                        "1",
                        "--rf-recovery-ten-service-uuid",
                        "fixture",
                    ],
                ),
            ):
                self.assertEqual(runtime.main(), 1)
            self.assertIn("not a terminal marker", log.read_text(encoding="utf-8"))
