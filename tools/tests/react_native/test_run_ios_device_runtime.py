"""Bounded console termination tests without an iPhone or CoreDevice."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840.cancellation_control import (
    write_document,
)
from tests.peripherals.nrf52840.support.session_receipts import fake_attempt
from tools.react_native import run_ios_device_runtime as runtime
from tools.tests.react_native.support.ios_receipts import (
    host_marker,
    ios_bootstrap,
    ios_process_document,
    write_ios_install,
)


class IosConsoleTerminationTests(unittest.TestCase):
    def test_default_basic_launch_flag_and_bounded_close(self) -> None:
        original_popen = subprocess.Popen
        commands: list[list[str]] = []
        children: list[subprocess.Popen[bytes]] = []
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "basic.log"
            script = (
                "import time; print('BLEHUB_NITRO_RUNTIME close:resolved', flush=True); "
                "time.sleep(10)"
            )

            def launch(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
                commands.append(command)
                child = original_popen([sys.executable, "-u", "-c", script], **kwargs)
                children.append(child)
                return child

            with (
                patch.object(runtime.shutil, "which", return_value="xcrun"),
                patch.object(runtime.subprocess, "Popen", side_effect=launch),
                patch.object(
                    sys,
                    "argv",
                    ["runtime", "device", str(log), "--seconds", "3"],
                ),
            ):
                started = time.monotonic()
                self.assertEqual(runtime.main(), 0)
                self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0].count("--blehub-nitro-basic"), 1)
            self.assertIn("BLEHUB_NITRO_RUNTIME close:resolved\n", log.read_text())
            self.assertIsNotNone(children[0].poll())

    def test_keep_app_detaches_non_rf_success_but_forwards_failure(self) -> None:
        original_popen = subprocess.Popen
        for options, marker in (
            ([], "close:resolved"),
            (["--stress"], "stress:complete:passed"),
            (["--reload"], "reload:complete:passed"),
        ):
            for failed in (False, True):
                with (
                    self.subTest(options=options, failed=failed),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    log = Path(directory) / "runtime.log"
                    children: list[subprocess.Popen[bytes]] = []
                    content = "synthetic:failed:reason" if failed else marker
                    script = (
                        "import time; "
                        f"print('BLEHUB_NITRO_RUNTIME {content}',flush=True); "
                        "time.sleep(10)"
                    )

                    def launch(command, *, script=script, children=children, **kwargs):
                        self.assertEqual(command[:2], ["xcrun", "devicectl"])
                        child = original_popen(
                            [sys.executable, "-u", "-c", script], **kwargs
                        )
                        children.append(child)
                        return child

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
                                "--keep-app",
                                *options,
                            ],
                        ),
                    ):
                        self.assertEqual(runtime.main(), 1 if failed else 0)
                    receipt = json.loads(
                        log.with_suffix(".console-cleanup.json").read_text()
                    )
                    self.assertEqual(
                        receipt["action"], "signal-forwarded" if failed else "detached"
                    )
                    self.assertEqual(len(children), 1)
                    self.assertIsNotNone(children[0].poll())
                    self.assertIn(content, log.read_text())

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
            self.assertNotIn("--blehub-nitro-basic", commands[0])
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
            self.assertNotIn("--blehub-nitro-basic", commands[0])
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
            option, prefix = "--rf-recovery-service-uuid", "rf-recovery"
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
            self.assertIn(f"{prefix}:close:passed:", log.read_text(encoding="utf-8"))

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
                        "import time; print('BLEHUB_NITRO_RUNTIME rf-recovery:cycle:2:cycle:passed:scanSessionId=10201', flush=True); time.sleep(30)",
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
                        "--rf-recovery-service-uuid",
                        "fixture",
                    ],
                ),
            ):
                started = time.monotonic()
                self.assertEqual(runtime.main(), 1)
                self.assertLess(time.monotonic() - started, 2)
            self.assertIn("cycle:2:cycle:passed:", log.read_text(encoding="utf-8"))

    def test_mid_cycle_failure_ends_console_without_waiting_for_close(self) -> None:
        original_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "mid-cycle-failed.log"
            script = (
                "import time; print('BLEHUB_NITRO_RUNTIME "
                "rf-recovery:cycle:2:gatt:failed:reason=connection', flush=True); "
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
                        "--rf-recovery-service-uuid",
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
            receipt = runtime.json.loads(
                log.with_suffix(".console-cleanup.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["action"], "signal-forwarded")
            self.assertEqual(receipt["console_pid"], children[0].pid)
            selector.close.assert_called_once()

    def test_continuous_normal9_requires_control_before_native_launch(self) -> None:
        with (
            patch.object(
                sys,
                "argv",
                ["runtime", "device", "log", "--rf-lab-suite-service-uuid", "fixture"],
            ),
            patch.object(runtime.subprocess, "Popen") as launch,
            self.assertRaises(SystemExit),
        ):
            runtime.main()
        launch.assert_not_called()

    def test_normal9_launch_precondition_exit_preserves_original_code_and_output(
        self,
    ) -> None:
        original_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "runtime.log"
            attempt = fake_attempt(root)
            install = root / "install.json"
            write_ios_install(install)
            children: list[subprocess.Popen[str]] = []
            diagnostic = "ERROR: device launch precondition failed (device locked)"

            def launch(args: list[str], **kwargs: object) -> subprocess.Popen[str]:
                child = original_popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        f"import sys; print({diagnostic!r}, file=sys.stderr, flush=True); sys.exit(17)",
                    ],
                    **kwargs,
                )
                children.append(child)
                return child

            with (
                patch.object(runtime.shutil, "which", return_value="xcrun"),
                patch.object(runtime.subprocess, "Popen", side_effect=launch),
                patch.object(runtime.subprocess, "run") as query,
                patch.object(runtime, "relay_ios") as notify,
                patch.object(
                    sys,
                    "argv",
                    [
                        "runtime",
                        "device",
                        str(log),
                        "--seconds",
                        "4",
                        "--rf-lab-suite-service-uuid",
                        "fixture",
                        "--rf-lab-suite-coordination-path",
                        str(attempt.root),
                        "--rf-host-install-json",
                        str(install),
                        "--keep-app",
                    ],
                ),
            ):
                self.assertEqual(runtime.main(), 17)
            self.assertEqual(children[0].returncode, 17)
            self.assertEqual(log.read_text(), diagnostic + "\n")
            identity = json.loads(log.with_suffix(".rf-host-identity.json").read_text())
            self.assertEqual(identity["status"], "failed")
            self.assertIn("17", identity["failure"])
            self.assertNotIn("bootstrap", identity["failure"])
            cleanup = json.loads(log.with_suffix(".console-cleanup.json").read_text())
            self.assertEqual(cleanup["action"], "console-exited")
            self.assertTrue((attempt.directory / "abort").exists())
            self.assertFalse((attempt.directory / "request-arm.json").exists())
            query.assert_not_called()
            notify.assert_not_called()

    def test_normal9_identity_or_ack_failure_aborts_before_any_process_reselection(
        self,
    ) -> None:
        for fault in (
            "missing-bootstrap",
            "foreign-process",
            "console-exited",
            "missing-native-ack",
        ):
            with self.subTest(fault=fault):
                self._normal9_identity_failure_case(fault)

    def _normal9_identity_failure_case(self, fault: str) -> None:
        original_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "runtime.log"
            attempt = fake_attempt(root)
            install = root / "install.json"
            write_ios_install(install)
            children: list[subprocess.Popen[str]] = []
            boot = ios_bootstrap(attempt.control)
            lines = [
                host_marker("bootstrap", boot),
                "BLEHUB_NITRO_RUNTIME "
                + attempt.control.marker("listener", "registered"),
                "BLEHUB_NITRO_RUNTIME " + attempt.control.marker("ready", "arm"),
            ]
            if fault == "missing-bootstrap":
                lines.pop(0)
            script = f"import time; print({chr(10).join(lines)!r}, flush=True); time.sleep(30)"

            def launch(args: list[str], **kwargs: object) -> subprocess.Popen[str]:
                child = original_popen([sys.executable, "-u", "-c", script], **kwargs)
                children.append(child)
                return child

            def query(args: list[str], **kwargs: object) -> None:
                document = ios_process_document()
                if fault == "foreign-process":
                    document["result"]["runningProcesses"][0]["processIdentifier"] = 43
                Path(args[args.index("--json-output") + 1]).write_text(
                    json.dumps(document), encoding="utf-8"
                )

            def notify(_device: str, _control: object, _phase: str) -> None:
                if fault == "console-exited":
                    children[0].kill()
                    children[0].wait(timeout=1)

            original_advance = runtime.advance_relay

            def advance(barrier: object, text: str, send: object) -> None:
                if (attempt.directory / "request-arm.json").exists() and not (
                    attempt.directory / "signal-armed.json"
                ).exists():
                    write_document(
                        attempt.directory / "signal-armed.json",
                        runtime.asdict(attempt.control),
                    )
                original_advance(barrier, text, send)

            with (
                patch.object(runtime.shutil, "which", return_value="xcrun"),
                patch.object(runtime.subprocess, "Popen", side_effect=launch),
                patch.object(runtime.subprocess, "run", side_effect=query),
                patch.object(runtime, "relay_ios", side_effect=notify) as notification,
                patch.object(runtime, "advance_relay", side_effect=advance),
                patch.object(
                    sys,
                    "argv",
                    [
                        "runtime",
                        "device",
                        str(log),
                        "--seconds",
                        "4",
                        "--rf-lab-suite-service-uuid",
                        "fixture",
                        "--rf-lab-suite-coordination-path",
                        str(attempt.root),
                        "--rf-host-install-json",
                        str(install),
                        "--keep-app",
                    ],
                ),
                self.assertRaises((RuntimeError, TimeoutError)),
            ):
                runtime.main()
            self.assertIsNotNone(children[0].poll())
            self.assertTrue((attempt.directory / "abort").exists())
            self.assertFalse(log.with_suffix(".launch.json").exists())
            identity = json.loads(log.with_suffix(".rf-host-identity.json").read_text())
            self.assertEqual(identity["status"], "failed")
            self.assertTrue(identity["failure"])
            if fault in ("missing-bootstrap", "foreign-process"):
                notification.assert_not_called()
                self.assertFalse((attempt.directory / "request-arm.json").exists())
                self.assertIsNone(identity["bootstrap"])
            else:
                notification.assert_called_once_with("device", attempt.control, "armed")
                self.assertEqual(identity["bootstrap"]["pid"], 42)

    def test_keep_app_kills_only_the_suite_console_bridge(self) -> None:
        original_popen = subprocess.Popen
        original_killpg = runtime.os.killpg
        signals: list[int] = []
        commands: list[list[str]] = []
        name = "Custom RF: fixture"
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "suite.log"
            attempt = fake_attempt(Path(temporary))
            install = Path(temporary) / "install.json"
            write_ios_install(install)
            boot = ios_bootstrap(attempt.control)
            children: list[subprocess.Popen[str]] = []
            queries: list[list[str]] = []
            from tests.peripherals.nrf52840.cancellation_control import (
                load_suite_controls,
            )
            from tests.peripherals.nrf52840.support.watchdog_reports import (
                fake_watchdog,
            )
            from tests.support.rf_lab.markers import log_parts

            fake_watchdog(attempt)
            from tests.peripherals.nrf52840.cancellation_control import ControlRelay
            from tools.tests.react_native.support.full_suite_ledger import (
                replay_physical_pending,
            )

            controls = load_suite_controls(attempt.root)
            script = "from pathlib import Path\nimport time\n"
            script += f"print({host_marker('bootstrap', boot)!r}, flush=True)\n"
            for control in controls:
                script += f"print({'BLEHUB_NITRO_RUNTIME rf-lab-suite:flow-started:index=' + str(control.flow) + ':mode=' + control.mode!r}, flush=True)\n"
                script += f"print({'BLEHUB_NITRO_RUNTIME ' + control.marker('listener', 'registered')!r}, flush=True)\n"
                for phase, request in zip(
                    control.phases, control.requests, strict=True
                ):
                    script += f"print({'BLEHUB_NITRO_RUNTIME ' + control.marker('ready', request)!r}, flush=True)\n"
                    script += f"while not Path({str(Path(temporary) / (str(control.flow) + '-' + phase))!r}).exists(): time.sleep(0.005)\n"
                    native = host_marker(
                        "signal", {**boot, **runtime.asdict(control), "phase": phase}
                    )
                    observed = "BLEHUB_NITRO_RUNTIME " + control.marker(
                        "observed", phase
                    )
                    for marker in (
                        (observed, native) if phase == "held" else (native, observed)
                    ):
                        script += f"print({marker!r}, flush=True)\n"
                    if phase == "held":
                        if control.flow == 8:
                            script += "print('BLEHUB_NITRO_RUNTIME rf:step:cancellation-terminal:passed:commandId=9028:error=cancelled', flush=True)\n"
                        else:
                            setup, gate, _ = log_parts()
                            script += f"print({setup + gate!r}, flush=True)\n"
            script += "print('BLEHUB_NITRO_RUNTIME rf-lab-suite:passed:flows=10:passedCount=10:failureCount=0', flush=True)\ntime.sleep(30)\n"

            def query(args: list[str], **kwargs: object) -> None:
                self.assertIsNone(children[0].poll())
                self.assertFalse(log.with_suffix(".launch.json").exists())
                if not queries:
                    self.assertFalse((attempt.directory / "request-arm.json").exists())
                queries.append(args)
                Path(args[args.index("--json-output") + 1]).write_text(
                    json.dumps(ios_process_document()), encoding="utf-8"
                )

            def notify(_device: str, control: object, phase: str) -> None:
                self.assertIn(control, controls)
                (Path(temporary) / (str(control.flow) + "-" + phase)).touch()

            original_advance = runtime.advance_relay

            def advance(barrier: object, text: str, send: object) -> None:
                for control in controls:
                    directory = attempt.root / control.directory
                    directory.mkdir(exist_ok=True)
                    for phase, request in zip(
                        control.phases, control.requests, strict=True
                    ):
                        if (directory / f"request-{request}.json").exists() and not (
                            directory / f"signal-{phase}.json"
                        ).exists():
                            write_document(
                                directory / f"signal-{phase}.json",
                                runtime.asdict(control),
                            )
                with patch.object(ControlRelay, "pending", new=replay_physical_pending):
                    original_advance(barrier, text, send)

            def launch(_command: list[str], **kwargs: object) -> subprocess.Popen[str]:
                commands.append(_command)
                child = original_popen([sys.executable, "-u", "-c", script], **kwargs)
                children.append(child)
                return child

            def killpg(process_group: int, signal_number: int) -> None:
                signals.append(signal_number)
                original_killpg(process_group, signal_number)

            with (
                patch.object(runtime.shutil, "which", return_value="xcrun"),
                patch.object(runtime.subprocess, "Popen", side_effect=launch),
                patch.object(runtime.os, "killpg", side_effect=killpg),
                patch(
                    "tools.react_native.check_runtime_log.check_rf_lab_suite"
                ) as physical_check,
                patch.object(runtime.subprocess, "run", side_effect=query),
                patch.object(runtime, "relay_ios", side_effect=notify),
                patch.object(runtime, "advance_relay", side_effect=advance),
                patch.object(
                    sys,
                    "argv",
                    [
                        "runtime",
                        "device",
                        str(log),
                        "--seconds",
                        "4",
                        "--rf-lab-suite-service-uuid",
                        "fixture",
                        "--rf-expected-local-name",
                        name,
                        "--rf-lab-suite-coordination-path",
                        str(attempt.root),
                        "--rf-host-install-json",
                        str(install),
                        "--keep-app",
                    ],
                ),
            ):
                self.assertEqual(runtime.main(), 0)

            self.assertEqual(signals, [runtime.signal.SIGKILL])
            self.assertEqual(len(queries), 9)
            self.assertFalse(log.with_suffix(".launch.json").exists())
            identity = json.loads(log.with_suffix(".rf-host-identity.json").read_text())
            self.assertEqual(identity["status"], "passed")
            self.assertEqual(identity["bootstrap"], boot)
            self.assertEqual(
                identity["bootstrap_deadline"], identity["launch_started_at"] + 4
            )
            self.assertEqual(
                identity["js_acknowledged"],
                [f"{c.flow}:{p}" for c in controls for p in c.phases],
            )
            self.assertGreaterEqual(physical_check.call_count, 1)
            self.assertEqual(
                physical_check.call_args.kwargs["cancellation_evidence"], attempt.path
            )
            self.assertEqual(
                runtime.json.loads(commands[0][-1]),
                {"controls": [runtime.asdict(c) for c in controls]},
            )
            self.assertEqual(
                commands[0][-6:-2],
                [
                    "--blehub-nitro-rf-lab-suite-service",
                    "fixture",
                    "--blehub-nitro-rf-expected-local-name",
                    name,
                ],
            )
            self.assertIn(
                "rf-lab-suite:passed:flows=10", log.read_text(encoding="utf-8")
            )

    def test_watchdog_requires_strict_canonical_uuid_before_launch(self) -> None:
        for service in (
            "",
            "fd4",
            "not-a-uuid",
            "FD400000-0000-4000-8000-000000000000",
            " fd400000-0000-4000-8000-000000000000",
            "fd400000-0000-4000-8000-000000000000\n",
            "{fd400000-0000-4000-8000-000000000000}",
        ):
            with (
                self.subTest(service=service),
                patch.object(
                    sys,
                    "argv",
                    ["runtime", "device", "log", "--rf-watchdog-service-uuid", service],
                ),
                patch.object(runtime.subprocess, "Popen") as launch,
                self.assertRaises(SystemExit),
            ):
                runtime.main()
            launch.assert_not_called()

    def test_watchdog_keep_app_detaches_success_but_forwards_failure_cleanup(
        self,
    ) -> None:
        original_popen = subprocess.Popen
        cases = (
            (service, final, status)
            for service in (
                "fd300000-0000-4000-8000-000000000000",
                "fd400000-0000-4000-8000-000000000000",
            )
            for final, status in (
                ("rf-watchdog:passed", 0),
                ("rf-watchdog:failed:native evidence absent", 1),
            )
        )
        for service, final, expected_status in cases:
            with (
                self.subTest(service=service, final=final),
                tempfile.TemporaryDirectory() as directory,
            ):
                commands: list[list[str]] = []
                signals: list[int] = []
                log = Path(directory) / "watchdog.log"
                original_killpg = runtime.os.killpg
                log.with_suffix(".launch.json").write_text(
                    "stale launch", encoding="utf-8"
                )
                log.with_suffix(".console-cleanup.json").write_text(
                    "stale cleanup", encoding="utf-8"
                )

                def launch(
                    command: list[str],
                    *,
                    commands: list[list[str]] = commands,
                    final: str = final,
                    log: Path = log,
                    **kwargs: object,
                ) -> subprocess.Popen[bytes]:
                    commands.append(command)
                    launch_json = Path(command[command.index("--json-output") + 1])
                    self.assertFalse(launch_json.exists())
                    self.assertFalse(log.with_suffix(".console-cleanup.json").exists())
                    # Synthetic devicectl writes the official launch identity artifact.
                    launch_json.write_text(
                        runtime.json.dumps(
                            {
                                "info": {
                                    "outcome": "success",
                                    "commandType": "devicectl.device.process.launch",
                                    "arguments": command[1:],
                                },
                                "result": {
                                    "deviceIdentifier": "synthetic-coredevice",
                                    "process": {
                                        "processIdentifier": 43210,
                                        "executable": "file:///synthetic/BleHubNitroHost.app/BleHubNitroHost",
                                    },
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    # Neither progress nor an embedded event/message is a final pass.
                    script = (
                        "import time; "
                        "print('BLEHUB_NITRO_RUNTIME "
                        "rf-watchdog:recovery-read:passed:commandId=21024', flush=True); "
                        "print('BLEHUB_NITRO_RUNTIME rf-watchdog:event:"
                        '{\\"message\\":\\"rf-watchdog:passed\\"}\', flush=True); '
                        "print('BLEHUB_NITRO_RUNTIME rf-watchdog:close:passed', flush=True); "
                        f"print({('BLEHUB_NITRO_RUNTIME ' + final)!r}, flush=True); "
                        "time.sleep(30)"
                    )
                    return original_popen(
                        [sys.executable, "-u", "-c", script], **kwargs
                    )

                def killpg(
                    pid: int,
                    signal: int,
                    *,
                    signals: list[int] = signals,
                    original_killpg: Callable[[int, int], None] = original_killpg,
                ) -> None:
                    signals.append(signal)
                    original_killpg(pid, signal)

                with (
                    patch.object(runtime.shutil, "which", return_value="xcrun"),
                    patch.object(runtime.subprocess, "Popen", side_effect=launch),
                    patch.object(runtime.os, "killpg", side_effect=killpg),
                    patch.object(
                        sys,
                        "argv",
                        [
                            "runtime",
                            "device",
                            str(log),
                            "--seconds",
                            "4",
                            "--rf-watchdog-service-uuid",
                            service,
                            "--rf-control-json",
                            json.dumps(
                                runtime.asdict(
                                    runtime.Control(
                                        "12345678-1234-4234-8234-123456789abc",
                                        9,
                                        "rf-watchdog",
                                        "11111111-1111-4111-8111-111111111111",
                                    )
                                )
                            ),
                            "--rf-expected-local-name",
                            "BleHubMultiRfV2DKA",
                            "--keep-app",
                        ],
                    ),
                ):
                    self.assertEqual(runtime.main(), expected_status)
                self.assertEqual(
                    commands[0][
                        commands[0].index("--blehub-nitro-rf-watchdog-service") + 1
                    ],
                    service,
                )
                self.assertEqual(
                    signals,
                    [
                        runtime.signal.SIGKILL
                        if expected_status == 0
                        else runtime.signal.SIGTERM
                    ],
                )
                self.assertEqual(
                    commands[0][commands[0].index("--json-output") + 1],
                    str(log.with_suffix(".launch.json")),
                )
                receipt = runtime.json.loads(
                    log.with_suffix(".console-cleanup.json").read_text(encoding="utf-8")
                )
                self.assertEqual(receipt["device"], "device")
                self.assertEqual(receipt["bundle_id"], runtime.BUNDLE_ID)
                self.assertEqual(receipt["helper_pid"], runtime.os.getpid())
                launch_identity = runtime.json.loads(
                    log.with_suffix(".launch.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    launch_identity["result"]["process"]["processIdentifier"], 43210
                )
                self.assertEqual(
                    receipt["launch_json"], str(log.with_suffix(".launch.json"))
                )
                self.assertEqual(
                    receipt["action"],
                    "detached" if expected_status == 0 else "signal-forwarded",
                )
                self.assertIn(final, log.read_text(encoding="utf-8"))

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
                        "--rf-recovery-service-uuid",
                        "fixture",
                    ],
                ),
            ):
                self.assertEqual(runtime.main(), 1)
            self.assertIn("not a terminal marker", log.read_text(encoding="utf-8"))
