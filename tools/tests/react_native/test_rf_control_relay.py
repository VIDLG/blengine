"""Host relay tests: explicit Linking intent/notification, no reload or hardware."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840.cancellation_control import PHASES, Control
from tools.react_native import rf_control_relay as relay
from tools.tests.react_native.support.ios_receipts import (
    CONTROL,
    EXECUTABLE,
    host_marker,
    ios_bootstrap,
    ios_process_document,
    write_ios_install,
)


class RfControlRelayTests(unittest.TestCase):
    def test_android_pins_pid_and_sends_explicit_view_without_reload(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            0,
            "Warning: Activity not started, intent has been delivered to currently running top-most instance.",
        )
        with (
            patch.object(relay, "android_pid", side_effect=["42", "42"]),
            patch.object(
                relay.android_sdk,
                "adb_command",
                side_effect=lambda serial, *args: ["fake-adb", "-s", serial, *args],
            ),
            patch.object(
                relay.android_sdk, "run_command", return_value=completed
            ) as command,
        ):
            relay.relay_android("device", "42", CONTROL, "held")
        self.assertEqual(
            command.call_args.args[0],
            [
                "fake-adb",
                "-s",
                "device",
                "shell",
                "am",
                "start",
                "-n",
                "com.blehubnitrohost/.MainActivity",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                CONTROL.uri("held"),
            ],
        )
        self.assertNotIn("reload", " ".join(command.call_args.args[0]).lower())
        for identities in (["43"], ["42", "43"]):
            with (
                self.subTest(identities=identities),
                patch.object(relay, "android_pid", side_effect=identities),
                patch.object(
                    relay.android_sdk, "adb_command", return_value=["fake-adb"]
                ),
                patch.object(
                    relay.android_sdk, "run_command", return_value=completed
                ) as command,
            ):
                with self.assertRaisesRegex(RuntimeError, "PID changed|restarted"):
                    relay.relay_android("device", "42", CONTROL, "held")
                self.assertEqual(command.call_count, len(identities) - 1)

    def test_android_offline_identity_requires_fixed_pid_capture_and_both_controls(
        self,
    ) -> None:
        from dataclasses import asdict

        from tests.peripherals.nrf52840.cancellation_control import write_document

        watchdog = Control(
            CONTROL.session, 9, "rf-watchdog", "33333333-3333-4333-8333-333333333333"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "runtime.log"
            write_document(root / "cancellation-control.json", asdict(CONTROL))
            write_document(root / "watchdog-control.json", asdict(watchdog))
            write_document(
                log.with_suffix(".identity.json"), {"status": "pinned", "pid": "42"}
            )
            capture = {
                "status": "passed",
                "controls": [asdict(CONTROL), asdict(watchdog)],
                "runtime_pid": "42",
                "command": ["adb", "logcat", "--pid", "42"],
            }
            write_document(log.with_suffix(".capture.json"), capture)
            text = (
                "10-14 12:00:00.000 42 88 I ReactNativeJS: BLEHUB_NITRO_RUNTIME test\n"
            )
            relay.check_suite_identity(text, log, root)
            for invalid in (
                {**capture, "status": "capturing"},
                {**capture, "runtime_pid": "43"},
                {**capture, "controls": [asdict(CONTROL)]},
            ):
                write_document(log.with_suffix(".capture.json"), invalid)
                with self.assertRaisesRegex(ValueError, "identity"):
                    relay.check_suite_identity(text, log, root)
            write_document(log.with_suffix(".capture.json"), capture)
            with self.assertRaisesRegex(ValueError, "foreign"):
                relay.check_suite_identity(text.replace("42 88", "43 88"), log, root)

    def test_ios_uses_independent_attempt_notification(self) -> None:
        with patch.object(relay.subprocess, "run") as command:
            relay.relay_ios("device", CONTROL, "held")
        args = command.call_args.args[0]
        self.assertEqual(args[args.index("--name") + 1], CONTROL.notification("held"))
        self.assertNotIn("reload", " ".join(args))
        self.assertEqual(args[args.index("--timeout") + 1], "5")
        self.assertEqual(command.call_args.kwargs["timeout"], 1.5)

    def test_ios_pins_live_boot_without_launch_json_and_requires_every_ack(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "runtime.log"
            install = Path(tmp) / "install.json"
            write_ios_install(install)
            identity = relay.IosRuntimeIdentity("device", CONTROL, install, log)
            started_at = relay.time.monotonic()
            identity.begin_launch(started_at, started_at + 90)

            def query(args: list[str], **kwargs: object) -> None:
                self.assertEqual(
                    args[:5], ["xcrun", "devicectl", "device", "info", "processes"]
                )
                self.assertNotIn("launch", args)
                self.assertEqual(args[args.index("--timeout") + 1], "5")
                self.assertIn(kwargs["timeout"], (1.5, 5.5))
                Path(args[args.index("--json-output") + 1]).write_text(
                    json.dumps(ios_process_document()), encoding="utf-8"
                )

            with patch.object(relay.subprocess, "run", side_effect=query) as commands:
                self.assertFalse(identity.ready_for_relay(""))
                identity.observe(host_marker("bootstrap", ios_bootstrap()))
                text = ""
                for phase in PHASES:
                    self.assertTrue(identity.ready_for_relay(text))
                    identity.before_signal(phase)
                    identity.sent_signal(phase)
                    self.assertFalse(identity.ready_for_relay(text))
                    observed = "BLEHUB_NITRO_RUNTIME " + CONTROL.marker(
                        "observed", phase
                    )
                    native = host_marker("signal", {**ios_bootstrap(), "phase": phase})
                    first, second = (
                        (observed, native) if phase == "held" else (native, observed)
                    )
                    identity.observe(first)
                    self.assertFalse(identity.ready_for_relay(text))
                    identity.observe(second)
                    text += observed + "\n"
                identity.finish(text)
                identity.finish(text)
            self.assertEqual(commands.call_count, 5)
            self.assertEqual(
                [call.kwargs["timeout"] for call in commands.call_args_list],
                [5.5, 1.5, 1.5, 1.5, 5.5],
            )
            self.assertFalse(log.with_suffix(".launch.json").exists())
            receipt = json.loads(log.with_suffix(".rf-host-identity.json").read_text())
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(receipt["bootstrap"], ios_bootstrap())
            self.assertEqual(receipt["native_acknowledged"], list(PHASES))
            self.assertEqual(len(list(Path(tmp).glob("*.rf-host-*-processes.json"))), 5)

    def test_ios_bundle_pins_one_boot_and_requires_all_seven_native_and_js_acks(
        self,
    ) -> None:
        from dataclasses import asdict, replace

        watchdog = Control(
            CONTROL.session, 9, "rf-watchdog", "33333333-3333-4333-8333-333333333333"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install.json"
            write_ios_install(install)
            identity = relay.IosRuntimeIdentity(
                "device",
                CONTROL,
                install,
                root / "runtime.log",
                controls=(CONTROL, watchdog),
            )
            identity.begin_launch(relay.time.monotonic(), relay.time.monotonic() + 90)
            with patch.object(identity, "_verify_process") as verify:
                identity.observe(host_marker("bootstrap", ios_bootstrap()))
                text = ""
                for control in (CONTROL, watchdog):
                    for phase in control.phases:
                        with self.assertRaisesRegex(RuntimeError, "drift"):
                            identity.before_signal(
                                phase, replace(control, token=control.session)
                            )
                        identity.before_signal(phase, control)
                        identity.sent_signal(phase, control)
                        expected = {
                            **ios_bootstrap(),
                            **asdict(control),
                            "phase": phase,
                        }
                        if control.flow == 9 and phase == "armed":
                            with self.assertRaisesRegex(RuntimeError, "foreign"):
                                identity.observe(
                                    host_marker(
                                        "signal",
                                        {**expected, "boot_id": watchdog.token},
                                    )
                                )
                        identity.observe(host_marker("signal", expected))
                        observed = "BLEHUB_NITRO_RUNTIME " + control.marker(
                            "observed", phase
                        )
                        identity.observe(observed)
                        text += observed + "\n"
                        self.assertTrue(identity.ready_for_relay(text))
                identity.finish(text)
            self.assertEqual(verify.call_count, 9)
            receipt = json.loads(
                root.joinpath("runtime.rf-host-identity.json").read_text()
            )
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(receipt["bootstrap"], ios_bootstrap())
            self.assertEqual(receipt["native_acknowledged"], receipt["js_acknowledged"])
            self.assertEqual(len(receipt["sent"]), 7)
            from tests.peripherals.nrf52840.cancellation_control import write_document

            for control in (CONTROL, watchdog):
                write_document(root / control.filename, asdict(control))
            complete_text = host_marker("bootstrap", ios_bootstrap()) + "\n"
            for control in (CONTROL, watchdog):
                for phase in control.phases:
                    complete_text += (
                        host_marker(
                            "signal",
                            {**ios_bootstrap(), **asdict(control), "phase": phase},
                        )
                        + "\n"
                    )
            for boundary in ("bootstrap", "closed"):
                write_document(
                    root / f"runtime.rf-host-{boundary}-processes.json",
                    ios_process_document(),
                )
            relay.check_suite_identity(complete_text, root / "runtime.log", root)
            source = root / "final.attempt.log"
            source.write_text(complete_text, encoding="utf-8")
            for suffix in (
                ".rf-host-identity.json",
                ".rf-host-bootstrap-processes.json",
                ".rf-host-closed-processes.json",
            ):
                source.with_suffix(suffix).write_bytes(
                    (root / "runtime.log").with_suffix(suffix).read_bytes()
                )
            relay.check_suite_identity(complete_text, root / "final.log", root)
            with self.assertRaisesRegex(ValueError, "differs"):
                relay.check_suite_identity(
                    complete_text + "changed\n", root / "final.log", root
                )

    def test_ios_second_control_late_ack_is_latched_not_a_new_window(self) -> None:
        from dataclasses import asdict

        watchdog = Control(
            CONTROL.session, 9, "rf-watchdog", "33333333-3333-4333-8333-333333333333"
        )
        for kind in ("native", "js"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                install = root / "install.json"
                write_ios_install(install)
                identity = relay.IosRuntimeIdentity(
                    "device",
                    CONTROL,
                    install,
                    root / "runtime.log",
                    controls=(CONTROL, watchdog),
                )
                identity.bootstrap = ios_bootstrap()
                identity.sent = identity.phase_keys[:3]
                identity.acknowledged = identity.sent.copy()
                identity.js_acknowledged = identity.sent.copy()
                with (
                    patch.object(identity, "_verify_process"),
                    patch.object(relay.time, "monotonic", return_value=100),
                ):
                    identity.before_signal("armed", watchdog)
                    identity.sent_signal("armed", watchdog)
                line = (
                    host_marker(
                        "signal",
                        {**ios_bootstrap(), **asdict(watchdog), "phase": "armed"},
                    )
                    if kind == "native"
                    else "BLEHUB_NITRO_RUNTIME " + watchdog.marker("observed", "armed")
                )
                with (
                    patch.object(relay.time, "monotonic", return_value=102.1),
                    self.assertRaises(TimeoutError),
                ):
                    identity.observe(line)
                with (
                    patch.object(relay.time, "monotonic", return_value=100.1),
                    self.assertRaisesRegex(RuntimeError, "failed"),
                ):
                    identity.ready_for_relay("")
                self.assertEqual(len(identity.acknowledged), 3)
                self.assertEqual(len(identity.js_acknowledged), 3)

    def test_ios_process_cli_timeout_floor_never_expands_outer_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install.json"
            write_ios_install(install)
            identity = relay.IosRuntimeIdentity(
                "device", CONTROL, install, root / "runtime.log"
            )

            def query(args: list[str], **kwargs: object) -> None:
                Path(args[args.index("--json-output") + 1]).write_text(
                    json.dumps(ios_process_document()), encoding="utf-8"
                )

            with patch.object(relay.subprocess, "run", side_effect=query) as command:
                for requested in (1, 5, 8):
                    with self.subTest(requested=requested):
                        identity._verify_process(ios_bootstrap(), "probe", requested)
                        args = command.call_args.args[0]
                        self.assertEqual(
                            int(args[args.index("--timeout") + 1]), max(5, requested)
                        )
                        self.assertEqual(
                            command.call_args.kwargs["timeout"], requested + 0.5
                        )

    def test_ios_command_failures_preserve_original_exception_and_stderr(self) -> None:
        for notification in (False, True):
            for expired in (False, True):
                with self.subTest(notification=notification, expired=expired):
                    self._ios_command_failure_case(notification, expired)

    def _ios_command_failure_case(self, notification: bool, expired: bool) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install.json"
            write_ios_install(install)
            identity = relay.IosRuntimeIdentity(
                "device", CONTROL, install, root / "runtime.log"
            )
            stdout = b"original command stdout\n"
            stderr = (
                b"host phase deadline reached\n"
                if expired
                else b"CLI validation failure: timeout must be >= 5\n"
            )
            raised: list[subprocess.SubprocessError] = []

            def failure(args: list[str], **kwargs: object) -> None:
                if expired:
                    error = subprocess.TimeoutExpired(
                        args, kwargs["timeout"], output=stdout, stderr=stderr
                    )
                else:
                    error = subprocess.CalledProcessError(
                        64, args, output=stdout, stderr=stderr
                    )
                raised.append(error)
                raise error

            with (
                patch.object(relay.subprocess, "run", side_effect=failure) as command,
                self.assertRaises(
                    subprocess.TimeoutExpired
                    if expired
                    else subprocess.CalledProcessError
                ) as caught,
            ):
                if notification:
                    relay.relay_ios("device", CONTROL, "armed")
                else:
                    identity._verify_process(ios_bootstrap(), "armed", 1)
            self.assertIs(caught.exception, raised[0])
            command.assert_called_once()
            args = command.call_args.args[0]
            self.assertEqual(int(args[args.index("--timeout") + 1]), 5)
            self.assertEqual(command.call_args.kwargs["timeout"], 1.5)
            self.assertIn(stderr.decode(), caught.exception.__notes__[0])
            self.assertIn("outer timeout=1.5s", caught.exception.__notes__[0])
            identity.fail(caught.exception)
            receipt = json.loads((root / "runtime.rf-host-identity.json").read_text())
            diagnostic = receipt["failure_command"]
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(diagnostic["command"], args)
            self.assertEqual(diagnostic["stdout"], stdout.decode())
            self.assertEqual(diagnostic["stderr"], stderr.decode())
            self.assertEqual(diagnostic["returncode"], None if expired else 64)
            self.assertEqual(diagnostic["timeout"], 1.5 if expired else None)
            self.assertEqual(identity.sent, [])

    def test_ios_boot_window_is_launch_anchored_bounded_and_separate_from_control(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install.json"
            write_ios_install(install)
            with patch.object(relay.time, "monotonic", return_value=100):
                identity = relay.IosRuntimeIdentity(
                    "device", CONTROL, install, root / "runtime.log"
                )
            self.assertIsNone(identity.bootstrap_deadline)
            with patch.object(relay.time, "monotonic", return_value=1000):
                identity.begin_launch(1000, 1900)
            self.assertEqual(identity.bootstrap_deadline, 1045)
            for now in (1011, 1044.99):
                with (
                    self.subTest(now=now),
                    patch.object(relay.time, "monotonic", return_value=now),
                ):
                    self.assertFalse(identity.ready_for_relay(""))
            with (
                patch.object(relay.time, "monotonic", return_value=1011),
                self.assertRaisesRegex(RuntimeError, "missing before control/ARM"),
            ):
                identity.ready_for_relay(
                    "BLEHUB_NITRO_RUNTIME " + CONTROL.marker("ready", "arm")
                )
            with patch.object(relay.time, "monotonic", return_value=1045):
                with self.assertRaisesRegex(TimeoutError, "bootstrap startup deadline"):
                    identity.ready_for_relay("")
                with patch.object(identity, "_verify_process") as query:
                    with self.assertRaisesRegex(
                        TimeoutError, "bootstrap startup deadline"
                    ):
                        identity.observe(host_marker("bootstrap", ios_bootstrap()))
                    query.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "cannot be restarted"):
                identity.begin_launch(1045, 2000)
            short = relay.IosRuntimeIdentity(
                "device", CONTROL, install, root / "short.log"
            )
            short.begin_launch(2000, 2020)
            self.assertEqual(short.bootstrap_deadline, 2020)
            with (
                patch.object(relay.time, "monotonic", return_value=2020),
                self.assertRaisesRegex(TimeoutError, "bootstrap startup deadline"),
            ):
                short.ready_for_relay("")

    def test_ios_foreign_process_receipts_never_pin_or_reselect(self) -> None:
        for fault in (
            "device",
            "pid",
            "executable",
            "duplicate",
            "malformed",
            "arguments",
            "command",
        ):
            with self.subTest(fault=fault):
                self._ios_foreign_process_case(fault)

    def _ios_foreign_process_case(self, fault: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install.json"
            write_ios_install(install)
            identity = relay.IosRuntimeIdentity(
                "device", CONTROL, install, root / "runtime.log"
            )
            started_at = relay.time.monotonic()
            identity.begin_launch(started_at, started_at + 90)
            document = ios_process_document()
            result = document["result"]
            if fault == "device":
                result["deviceIdentifier"] = "foreign"
            elif fault == "pid":
                result["runningProcesses"][0]["processIdentifier"] = 43
            elif fault == "executable":
                result["runningProcesses"][0]["executable"] = EXECUTABLE.replace(
                    "/install/", "/foreign/"
                )
            elif fault == "duplicate":
                result["runningProcesses"] *= 2
            elif fault == "malformed":
                result["runningProcesses"].append({"processIdentifier": True})
            elif fault == "arguments":
                document["info"]["arguments"][-1] = "foreign"
            else:
                document["info"]["commandType"] = "devicectl.device.process.launch"

            def query(args: list[str], **kwargs: object) -> None:
                Path(args[args.index("--json-output") + 1]).write_text(
                    json.dumps(document), encoding="utf-8"
                )

            with patch.object(relay.subprocess, "run", side_effect=query):
                with self.assertRaises(RuntimeError):
                    identity.observe(host_marker("bootstrap", ios_bootstrap()))
                self.assertIsNone(identity.bootstrap)
                document = ios_process_document()
                identity.observe(host_marker("bootstrap", ios_bootstrap()))
                document["result"]["runningProcesses"] = [
                    {"processIdentifier": 43, "executable": EXECUTABLE}
                ]
                with self.assertRaisesRegex(RuntimeError, "pinned iOS PID"):
                    identity.before_signal("armed")
                self.assertEqual(identity.bootstrap["pid"], 42)
                self.assertEqual(identity.sent, [])

    def test_ios_foreign_boot_early_duplicate_ack_and_deadlines_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install.json"
            write_ios_install(install)
            identity = relay.IosRuntimeIdentity(
                "device", CONTROL, install, root / "runtime.log"
            )
            started_at = relay.time.monotonic()
            identity.begin_launch(started_at, started_at + 90)
            with self.assertRaisesRegex(RuntimeError, "missing before"):
                identity.observe(
                    "BLEHUB_NITRO_RUNTIME " + CONTROL.marker("ready", "arm")
                )
            with (
                patch.object(
                    relay.time, "monotonic", return_value=identity.bootstrap_deadline
                ),
                self.assertRaisesRegex(TimeoutError, "bootstrap startup deadline"),
            ):
                identity.ready_for_relay("")
            for key, value in (
                ("token", "foreign"),
                ("session", "foreign"),
                ("bundle_id", "foreign"),
                ("executable", "foreign"),
                ("pid", True),
                ("boot_id", "foreign"),
                ("flow", "8"),
            ):
                with (
                    self.subTest(key=key),
                    patch.object(identity, "_verify_process") as query,
                ):
                    with self.assertRaises((RuntimeError, ValueError)):
                        identity.observe(
                            host_marker("bootstrap", {**ios_bootstrap(), key: value})
                        )
                    query.assert_not_called()
            with patch.object(identity, "_verify_process"):
                identity.observe(host_marker("bootstrap", ios_bootstrap()))
                with self.assertRaisesRegex(RuntimeError, "duplicate"):
                    identity.observe(host_marker("bootstrap", ios_bootstrap()))
                ack = {**ios_bootstrap(), "phase": "armed"}
                with self.assertRaisesRegex(RuntimeError, "early"):
                    identity.observe(host_marker("signal", ack))
                with self.assertRaisesRegex(RuntimeError, "preceded forwarding"):
                    identity.observe(
                        "BLEHUB_NITRO_RUNTIME " + CONTROL.marker("observed", "armed")
                    )
                identity.before_signal("armed")
                identity.sent_signal("armed")
                for record in (
                    {**ack, "token": "foreign"},
                    {**ack, "boot_id": "33333333-3333-4333-8333-333333333333"},
                    {**ack, "phase": "held"},
                ):
                    with self.assertRaisesRegex(RuntimeError, "native iOS signal ACK"):
                        identity.observe(host_marker("signal", record))
                with self.assertRaisesRegex(RuntimeError, "phase drift"):
                    identity.before_signal("held")
                with self.assertRaisesRegex(RuntimeError, "all native and JS"):
                    identity.finish("")
                identity.observe(host_marker("signal", ack))
                with self.assertRaisesRegex(RuntimeError, "duplicate"):
                    identity.observe(host_marker("signal", ack))
                with (
                    patch.object(
                        relay.time, "monotonic", return_value=identity.sent_at + 2
                    ),
                    self.assertRaisesRegex(TimeoutError, "ACK deadline"),
                ):
                    identity.ready_for_relay("")
                with self.assertRaisesRegex(RuntimeError, "identity failed"):
                    identity.observe(
                        "BLEHUB_NITRO_RUNTIME " + CONTROL.marker("observed", "armed")
                    )

    def test_ios_ack_receipt_deadline_rejects_late_pairs_and_latches_failure(
        self,
    ) -> None:
        for native_first in (False, True):
            for elapsed in (1.999, 2.0, 2.1):
                for late_first in (False, True):
                    with self.subTest(
                        native_first=native_first,
                        elapsed=elapsed,
                        late_first=late_first,
                    ):
                        self._ios_ack_receipt_case(native_first, elapsed, late_first)

    def _ios_ack_receipt_case(
        self, native_first: bool, elapsed: float, late_first: bool
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install.json"
            write_ios_install(install)
            identity = relay.IosRuntimeIdentity(
                "device", CONTROL, install, root / "runtime.log"
            )
            clock = [100.0]
            with (
                patch.object(relay.time, "monotonic", side_effect=lambda: clock[0]),
                patch.object(identity, "_verify_process") as query,
            ):
                identity.begin_launch(100, 1000)
                identity.observe(host_marker("bootstrap", ios_bootstrap()))
                identity.before_signal("armed")
                identity.sent_signal("armed")
                native = host_marker("signal", {**ios_bootstrap(), "phase": "armed"})
                js = "BLEHUB_NITRO_RUNTIME " + CONTROL.marker("observed", "armed")
                first, second = (native, js) if native_first else (js, native)
                clock[0] += elapsed if late_first else 0.1
                if not late_first or elapsed < 2:
                    identity.observe(first)
                    clock[0] = 100 + elapsed
                if elapsed < 2:
                    identity.observe(second)
                    self.assertTrue(identity.ready_for_relay(""))
                    self.assertEqual(identity.acknowledged, ["armed"])
                    self.assertEqual(identity.js_acknowledged, ["armed"])
                    return
                rejected = first if late_first else second
                before_native, before_js = (
                    list(identity.acknowledged),
                    list(identity.js_acknowledged),
                )
                with self.assertRaisesRegex(TimeoutError, "ACK deadline"):
                    identity.observe(rejected)
                self.assertEqual(identity.acknowledged, before_native)
                self.assertEqual(identity.js_acknowledged, before_js)
                self.assertIn("ACK deadline", identity.failure)
                clock[0] = 100.1  # Rewinding the fake clock cannot clear the latch.
                for operation in (
                    lambda: identity.observe(rejected),
                    lambda: identity.ready_for_relay(""),
                    lambda: identity.before_signal("held"),
                    lambda: identity.sent_signal("held"),
                    lambda: identity.finish(""),
                ):
                    with self.assertRaisesRegex(RuntimeError, "identity failed"):
                        operation()
                self.assertEqual(query.call_count, 2)
                receipt = json.loads(
                    (root / "runtime.rf-host-identity.json").read_text()
                )
                self.assertEqual(receipt["status"], "failed")
                self.assertIn("ACK deadline", receipt["failure"])

    def test_failed_signal_is_never_retried_or_recorded_as_forwarded(self) -> None:
        barrier = Mock()
        barrier.control = CONTROL
        barrier.pending.return_value = "held"
        send = Mock(side_effect=RuntimeError("notification missing"))
        with self.assertRaisesRegex(RuntimeError, "notification missing"):
            relay.advance_relay(barrier, "text", send)
        send.assert_called_once_with(CONTROL, "held")
        barrier.forwarded.assert_not_called()


if __name__ == "__main__":
    unittest.main()
