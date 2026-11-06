"""Synthetic streaming/process/deadline tests; never Android or RF evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tests.peripherals.nrf52840.adapter_transition import MODE, PHYSICAL_NAME, PREFIX
from tests.support.rf_lab.markers import SERVICE
from tools.react_native import adapter_transition_capture as module
from tools.react_native import adapter_transition_ledger
from tools.tests.react_native.support.adapter_transition_samples import (
    BOOTSTRAP,
    RUN,
    bodies,
)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.tasks: list[tuple[float, object]] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        ready = [task for task in self.tasks if task[0] <= self.now]
        self.tasks = [task for task in self.tasks if task[0] > self.now]
        for _, action in ready:
            action()


class Child:
    def __init__(self, *, streaming: bool = False) -> None:
        self.pid = 9876
        self.returncode: int | None = None if streaming else 0
        self.terminate = Mock(side_effect=self._terminate)
        self.kill = Mock(side_effect=self._terminate)
        self.wait = Mock(return_value=0)

    def _terminate(self) -> None:
        self.returncode = 0

    def poll(self) -> int | None:
        return self.returncode


class Harness:
    """Private capture module with real growing files and no real subprocesses."""

    def __init__(self, root: Path) -> None:
        spec = importlib.util.spec_from_file_location(
            "synthetic_adapter_capture", module.__file__
        )
        assert spec is not None and spec.loader is not None
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.clock = Clock()
        self.api.time = self.clock
        self.api.android_sdk = SimpleNamespace(
            adb_command=lambda device, *args: ["/sdk/adb", "-s", device, *args],
            run_command=Mock(side_effect=self.cleanup_command),
        )
        self.api.subprocess = SimpleNamespace(
            Popen=self.popen,
            DEVNULL=subprocess.DEVNULL,
            STDOUT=subprocess.STDOUT,
            TimeoutExpired=subprocess.TimeoutExpired,
            CalledProcessError=subprocess.CalledProcessError,
        )
        self.api.wait_for_initial_pid = Mock(return_value="1234")
        self.guard = Mock()
        self.phone_alive = True
        self.phone_guard = Mock(side_effect=self.require_phone)
        self.events: list[str] = []
        self.commands: list[list[str]] = []
        self.children: list[Child] = []
        self.last_dispatch_guard = 0
        self.writer = None
        self.stream_child: Child | None = None
        self.marker_pid = 1234
        self.pidof = "1234"
        self.source = "/data/app/synthetic/base.apk"
        self.initial = bodies()[:8]
        self.tail = bodies()[8:]
        self.ack_delay = 0.0
        self.radio_delay = 0.0
        self.radio_error: Exception | None = None
        self.launch_output = "Starting: Intent\n"
        self.control_output = "Starting: Intent\n"
        self.control_returncode = 0
        self.stop_tail: str | None = None
        self.make_physical = True
        self.ready_delay: float | None = 0.1
        self.root = root / "coordination"
        self.root.mkdir()
        session = self.root / "fixture-session"
        session.mkdir()
        self.receipt = session / "fixture-receipt.json"
        self.receipt.write_bytes(b'{\n  "synthetic": "original receipt bytes"\n}\n')
        self.ready_payload = {
            "schema_version": 1,
            "run_id": RUN,
            "status": "ready",
            "receipt_sha256": hashlib.sha256(self.receipt.read_bytes()).hexdigest(),
            "native_bootstrap": dict(BOOTSTRAP),
            "monotonic": 100.1,
        }
        self.log = root / "adapter-transition-runtime.log"
        self.capture = self.api.AdapterCapture(
            "synthetic-device",
            self.log,
            self.root,
            RUN,
            SERVICE,
            "BleHubNrf52840",
            timeout=20,
            guard=self.guard,
            radio_on=self.radio_on,
            phone_guard=self.phone_guard,
        )

    def require_phone(self) -> None:
        if not self.phone_alive:
            raise RuntimeError("phone owner lost")

    def ready(self) -> None:
        self.events.append("provider-ready")
        (self.root / self.api.READY_NAME).write_text(json.dumps(self.ready_payload))

    def append(self, markers: list[str]) -> None:
        assert self.writer is not None
        for body in markers:
            line = f"10-15 12:00:00.000 {self.marker_pid} 5678 I Test: {PREFIX}{body}\n"
            self.writer.write(line.encode())
        self.writer.flush()

    def physical(self) -> None:
        (self.root / PHYSICAL_NAME).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": MODE,
                    "run_id": RUN,
                    "status": "passed",
                }
            )
        )

    def deliver(self) -> None:
        self.append(self.tail)
        if self.make_physical:
            self.clock.tasks.append((self.clock.now + 0.1, self.physical))

    def radio_on(self) -> None:
        self.events.append("radio-on")
        self.clock.sleep(self.radio_delay)
        if self.radio_error:
            raise self.radio_error

    def cleanup_command(self, argv: list[str], **kwargs: object) -> SimpleNamespace:
        self.commands.append(argv)
        if "pidof" in argv:
            output = self.pidof
        elif "path" in argv:
            output = "package:" + self.source
        else:
            output = ""
        return SimpleNamespace(stdout=output, returncode=0)

    def popen(self, argv: list[str], **kwargs: object) -> Child:
        assert self.phone_guard.call_count > self.last_dispatch_guard
        self.last_dispatch_guard = self.phone_guard.call_count
        self.commands.append(argv)
        output = kwargs["stdout"]
        if "logcat" in argv:
            self.writer = output
            child = Child(streaming=True)
            self.stream_child = child
            if self.stop_tail:

                def stop() -> None:
                    self.append([self.stop_tail])
                    child.returncode = 0

                child.terminate.side_effect = stop
        else:
            child = Child()
            text = ""
            if "pidof" in argv:
                text = self.pidof
            elif "path" in argv:
                text = "package:" + self.source
            elif "android.intent.action.VIEW" in argv:
                self.events.append("url")
                text = self.control_output
                child.returncode = self.control_returncode
                if self.ack_delay:
                    self.clock.tasks.append(
                        (self.clock.now + self.ack_delay, self.deliver)
                    )
                    child.returncode = None
                    self.clock.tasks.append(
                        (self.clock.now + self.ack_delay, child._terminate)
                    )
                else:
                    self.deliver()
            elif "rf-adapter-transition" in argv:
                self.events.append("launch")
                text = self.launch_output
                self.append(self.initial)  # Bootstrap precedes initial PID acquisition.
                if self.ready_delay is not None:
                    self.clock.tasks.append(
                        (self.clock.now + self.ready_delay, self.ready)
                    )
            output.write(text.encode())
            output.flush()
        self.children.append(child)
        return child


class ActiveCaptureTests(unittest.TestCase):
    def setup_active(self, root: Path) -> Harness:
        from tests.peripherals.nrf52840 import adapter_transition as protocol
        from tools.tests.react_native.support.adapter_transition_samples import (
            active_bodies,
            active_physical_sidecars,
            transcript,
        )

        h = Harness(root)
        h.off = Mock()
        h.capture = h.api.AdapterCapture(
            "synthetic-device",
            h.log,
            h.root,
            RUN,
            SERVICE,
            "BleHubNrf52840",
            timeout=20,
            guard=h.guard,
            phone_guard=h.phone_guard,
            radio_on=h.radio_on,
            mode="active-loss",
            radio_off=h.off,
        )
        h.capture._poll = Mock()
        h.capture._verify_process = Mock()
        markers = active_bodies()
        stop = next(i for i, m in enumerate(markers) if m.startswith("native:arm:"))
        for marker in markers[:stop]:
            h.capture.facts.observe(PREFIX + marker)
        runtime = adapter_transition_ledger.verify_runtime(
            transcript(markers), RUN, SERVICE, Path("runtime.log")
        )
        _, _, physical = active_physical_sidecars(runtime)
        h.protocol = protocol
        h.documents = physical["active_protocol"]
        for value in h.documents.values():
            if "receipt_sha256" in value:
                value["receipt_sha256"] = h.capture.receipt_sha256

        def command(*args: str) -> str:
            h.commands.append(list(args))
            h.capture._observe(PREFIX + markers[stop])
            h.capture._observe(PREFIX + markers[stop + 1])
            return "Starting: Intent\n"

        h.capture._command = Mock(side_effect=command)
        return h

    @staticmethod
    def publish(h: Harness, name: str, value: dict | None = None) -> None:
        (h.root / name).write_text(
            json.dumps(h.documents[name] if value is None else value)
        )

    def arm_and_queue(self, h: Harness) -> None:
        self.publish(h, h.protocol.ARM_NAME)
        h.capture._active_actions()
        h.capture._observe(PREFIX + "active:accepted:32008")
        h.capture._observe(PREFIX + "active:accepted:32009")
        self.publish(h, h.protocol.LOSS_NAME)

    def authorize(self, h: Harness) -> None:
        value = dict(h.documents[h.protocol.OFF_READY])
        value["request_sha256"] = hashlib.sha256(
            (h.root / h.protocol.OFF_REQUEST).read_bytes()
        ).hexdigest()
        self.publish(h, h.protocol.OFF_READY, value)

    def test_no_off_without_live_queue_and_fresh_request_ack_then_exactly_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = self.setup_active(Path(directory))
            self.publish(h, h.protocol.ARM_NAME)
            h.capture._active_actions()
            h.capture._observe(PREFIX + "active:accepted:32008")
            self.publish(h, h.protocol.LOSS_NAME)
            h.capture._active_actions()
            self.assertFalse((h.root / h.protocol.OFF_REQUEST).exists())
            h.off.assert_not_called()
            h.capture._observe(PREFIX + "active:accepted:32009")
            h.capture._active_actions()
            self.assertTrue((h.root / h.protocol.OFF_REQUEST).exists())
            h.off.assert_not_called()
            self.authorize(h)
            h.capture._active_actions()
            h.capture._active_actions()
            h.off.assert_called_once_with(timeout=4.0)
            self.assertEqual(h.capture._command.call_count, 1)
            self.assertTrue((h.root / h.protocol.OFF_CONFIRMED).exists())
            self.assertTrue(h.capture.facts.active.loss_authorized)

    def test_stale_changed_foreign_and_wrong_request_proofs_do_not_toggle(self) -> None:
        for fault in ("stale", "wrong-hash", "foreign-hold", "owner-lost", "changed"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                h = self.setup_active(Path(directory))
                self.arm_and_queue(h)
                h.capture._active_actions()
                self.authorize(h)
                if fault == "stale":
                    h.clock.now += 1.01
                elif fault == "owner-lost":
                    h.phone_alive = False
                elif fault == "changed":
                    (h.root / h.protocol.ARM_NAME).write_text("{}")
                    h.capture._poll = lambda h=h: h.api.AdapterCapture._poll(h.capture)
                else:
                    value = json.loads((h.root / h.protocol.OFF_READY).read_bytes())
                    if fault == "wrong-hash":
                        value["request_sha256"] = "0" * 64
                    else:
                        value["ticket"]["sequence"] += 1
                    self.publish(h, h.protocol.OFF_READY, value)
                with self.assertRaises((RuntimeError, TimeoutError)):
                    h.capture._active_actions()
                h.off.assert_not_called()

    def test_slow_off_is_failed_without_forging_confirmation_or_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = self.setup_active(Path(directory))
            self.arm_and_queue(h)
            h.capture._active_actions()
            self.authorize(h)
            h.off.side_effect = lambda *, timeout: h.clock.sleep(timeout + 0.01)
            with self.assertRaisesRegex(TimeoutError, "4s"):
                h.capture._active_actions()
            h.off.assert_called_once_with(timeout=4.0)
            self.assertFalse((h.root / h.protocol.OFF_CONFIRMED).exists())


class AdapterCaptureTests(unittest.TestCase):
    def test_one_stream_bootstrap_before_pid_atomic_lineage_and_keep_app(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            text = h.capture.run()
            self.assertEqual(h.events, ["launch", "provider-ready", "radio-on", "url"])
            self.assertEqual(sum("logcat" in c for c in h.commands), 1)
            self.assertFalse(any("force-stop" in c for c in h.commands))
            self.assertEqual(text, h.log.read_text())
            self.assertEqual(text, (h.root / "runtime.log").read_text())
            self.assertFalse(list(h.root.glob("*.tmp")))
            value = json.loads(h.log.with_suffix(".identity.json").read_text())
            self.assertEqual(value["native_bootstrap"], BOOTSTRAP)
            self.assertEqual(value["status"], "passed")
            self.assertEqual(
                value["process_receipts"][-1]["boundary"], "after-logcat-detach"
            )
            for content in (text, h.log.with_suffix(".raw.log").read_text()):
                adapter_transition_ledger.verify_runtime(content, RUN, SERVICE, h.log)
            url = next(c for c in h.commands if "android.intent.action.VIEW" in c)
            self.assertIn("--activity-single-top", url)
            self.assertEqual(url[-1], f"blehub-rn-spike://adapter/{RUN}/on")
            self.assertIn("-n", url)
            self.assertTrue(all(c.poll() is not None for c in h.children))

    def test_exact_single_top_warning_is_control_only_and_other_diagnostics_fail(
        self,
    ) -> None:
        warning = module.DELIVERED_WARNING
        check = module.AdapterCapture._check_launch
        for newline in ("\n", "\r\n"):
            output = "Starting: Intent" + newline + warning + newline
            check(output, control=True)
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                check(output)
        check("Starting: Intent\n")
        for output in (
            warning.removesuffix("."),
            "prefix " + warning,
            " " + warning,
            warning + " suffix",
            warning + " ",
            warning + "\n" + warning,
            "Warning: other warning",
            "Error: permission denied",
            "Error without a colon",
            "java.lang.SecurityException",
            "Activity not started: unresolved component",
        ):
            with self.subTest(output=output):
                for content in (
                    output,
                    warning + "\n" + output,
                    output + "\n" + warning,
                ):
                    with self.assertRaisesRegex(RuntimeError, "rejected"):
                        check(content, control=True)

    def test_single_top_warning_passes_with_ack_identity_and_raw_am_receipts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.control_output = (
                "Starting: Intent { synthetic }\r\n" + module.DELIVERED_WARNING + "\r\n"
            )
            h.capture.run()
            for phase, output in (
                ("launch", h.launch_output),
                ("payload", h.control_output),
            ):
                receipt = json.loads(h.log.with_suffix(f".am-{phase}.json").read_text())
                self.assertEqual(receipt["stdout"], output)
                self.assertEqual(receipt["returncode"], 0)
                self.assertEqual(receipt["run_id"], RUN)
                self.assertEqual(receipt["phase"], phase)
                self.assertFalse(receipt["stdout_truncated"])
                self.assertLessEqual(
                    receipt["started_monotonic"], receipt["finished_monotonic"]
                )
            self.assertTrue(h.capture.native_ack and h.capture.js_ack)
            self.assertEqual(h.capture.facts.bootstrap, BOOTSTRAP)
            self.assertEqual(h.capture.receipts[-1]["boundary"], "after-logcat-detach")
            self.assertFalse(any("force-stop" in c for c in h.commands))

    def test_warning_cannot_replace_zero_exit_ack_pid_or_boot_and_failed_stdout_is_saved(
        self,
    ) -> None:
        for case in (
            "nonzero",
            "missing-js",
            "pid",
            "boot",
            "other-warning",
            "initial-launch",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary))
                h.control_output = (
                    "Starting: Intent\n" + module.DELIVERED_WARNING + "\n"
                )
                if case == "nonzero":
                    h.control_returncode = 1
                elif case == "missing-js":
                    h.tail = [bodies()[8]]
                elif case == "pid":
                    original = h.deliver

                    def deliver(original=original, h=h) -> None:
                        original()
                        h.pidof = "9999"

                    h.deliver = deliver
                elif case == "boot":
                    signal = json.loads(h.tail[0].removeprefix("native:signal:"))
                    signal["boot_id"] = RUN
                    h.tail[0] = "native:signal:" + json.dumps(signal)
                elif case == "other-warning":
                    h.control_output += "Warning: unresolved component\n"
                else:
                    h.launch_output = h.control_output
                with self.assertRaises(
                    (RuntimeError, TimeoutError, subprocess.CalledProcessError)
                ):
                    h.capture.run()
                phase = "launch" if case == "initial-launch" else "payload"
                receipt = json.loads(h.log.with_suffix(f".am-{phase}.json").read_text())
                expected = h.launch_output if phase == "launch" else h.control_output
                self.assertEqual(receipt["stdout"], expected)
                self.assertEqual(receipt["returncode"], 1 if case == "nonzero" else 0)
                self.assertTrue((h.root / "abort").exists())
                self.assertFalse(any(c.poll() is None for c in h.children))
                if case == "initial-launch":
                    self.assertNotIn("radio-on", h.events)
                    self.assertNotIn("url", h.events)

    def test_radio_confirmation_time_is_not_charged_to_url_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.radio_delay = 4
            h.ack_delay = 0.2
            h.capture.run()
            self.assertGreaterEqual(h.capture.sent_at, 104)

    def test_ack_deadline_pumps_while_control_command_is_pending(self) -> None:
        for delay, tail in (
            (2.1, bodies()[8:]),
            (0.1, [bodies()[8]]),  # Native ACK alone cannot satisfy both ACKs.
        ):
            with (
                self.subTest(delay=delay, tail=len(tail)),
                tempfile.TemporaryDirectory() as temporary,
            ):
                h = Harness(Path(temporary))
                h.ack_delay, h.tail = delay, tail
                with self.assertRaises((TimeoutError, subprocess.TimeoutExpired)):
                    h.capture.run()
                self.assertTrue((h.root / "abort").exists())
                self.assertTrue(all(c.poll() is not None for c in h.children))

    def test_foreign_pid_bootstrap_path_boot_and_prefix_fail_closed(self) -> None:
        cases = ("pid-header", "pidof", "source", "boot", "prefix")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary))
                if case == "pid-header":
                    h.marker_pid = 9999
                elif case == "pidof":
                    h.pidof = "1234 9999"
                elif case == "source":
                    h.source = "/data/app/foreign/base.apk"
                elif case == "boot":
                    value = {**BOOTSTRAP, "boot_id": "not-a-uuid"}
                    h.initial[0] = "native:bootstrap:" + json.dumps(value)
                else:
                    original = h.append

                    def append(markers: list[str], original=original, h=h) -> None:
                        original(markers)
                        h.writer.write(
                            b"10-15 12:00:00.000 1234 5678 I Test: BLEHUB_NITRO_RUNTIME rf:passed\n"
                        )
                        h.writer.flush()

                    h.append = append
                with self.assertRaises((RuntimeError, ValueError)):
                    h.capture.run()
                self.assertNotIn("radio-on", h.events)
                self.assertTrue((h.root / "abort").exists())
                if case == "pidof":
                    self.assertFalse(any("force-stop" in c for c in h.commands))

    def test_off_phase_gates_native_js_order_duplicates_and_expected_error(
        self,
    ) -> None:
        cases = {
            "missing-resources": bodies()[:5] + bodies()[6:8],
            "nonzero-resources": bodies()[:5]
            + [bodies()[5].replace(": 0", ": 1", 1)]
            + bodies()[6:8],
            "early-native": bodies()[:8] + [bodies()[8]],
            "duplicate-awaiting": bodies()[:8] + [bodies()[7]],
        }
        for case, initial in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary))
                h.initial = initial
                with self.assertRaises(RuntimeError):
                    h.capture.run()
                self.assertNotIn("radio-on", h.events)
        for tail in (
            [bodies()[9], bodies()[8]],
            [bodies()[8], bodies()[8]],
            [bodies()[8], bodies()[9], bodies()[9]],
            ["native:signal-failed:foreign-url"],
        ):
            with self.subTest(tail=tail), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary))
                h.tail = tail
                with self.assertRaises(RuntimeError):
                    h.capture.run()

    def test_unsolicited_ack_during_radio_on_rejected_before_url_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.capture.radio_on = lambda: h.append([bodies()[8]])
            with self.assertRaisesRegex(RuntimeError, "early"):
                h.capture.run()
            self.assertNotIn("url", h.events)

    def test_detach_drains_late_records_and_does_not_hide_primary_cleanup_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.stop_tail = f"passed:runId={RUN}"
            with self.assertRaisesRegex(RuntimeError, "out-of-order|unknown"):
                h.capture.run()
            self.assertTrue((h.root / "abort").exists())
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            primary = RuntimeError("radio failed")
            h.radio_error = primary
            h.capture._cleanup_app = Mock(side_effect=RuntimeError("cleanup failed"))
            with self.assertRaises(RuntimeError) as caught:
                h.capture.run()
            self.assertIs(caught.exception, primary)
            self.assertIn("cleanup failed", " ".join(primary.__notes__))

    def test_parent_abort_requires_same_run_identity_and_rechecks_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.capture.run()
            h.api.abort_owned_app(
                "synthetic-device", h.log, RUN, phone_guard=h.phone_guard
            )
            self.assertEqual(sum("force-stop" in c for c in h.commands), 1)
            h.pidof = "9999"
            with self.assertRaisesRegex(RuntimeError, "replaced/absent"):
                h.api.abort_owned_app(
                    "synthetic-device", h.log, RUN, phone_guard=h.phone_guard
                )
            self.assertEqual(sum("force-stop" in c for c in h.commands), 1)
            with self.assertRaisesRegex(RuntimeError, "same-run"):
                h.api.abort_owned_app(
                    "synthetic-device",
                    h.log,
                    "12345678-1234-4123-8123-123456789abe",
                    phone_guard=h.phone_guard,
                )

    def test_ready_ack_is_required_before_radio_on_and_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.ready_delay = 0.6
            h.capture.run()
            self.assertGreaterEqual(h.capture.sent_at, 100.6)
            self.assertEqual(
                json.loads((h.root / h.api.READY_NAME).read_text()), h.ready_payload
            )
            self.assertEqual(
                h.capture.ready_raw, (h.root / h.api.READY_NAME).read_bytes()
            )
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.ready_delay = None
            with self.assertRaisesRegex(TimeoutError, "absolute deadline"):
                h.capture.run()
            self.assertNotIn("radio-on", h.events)
            self.assertNotIn("url", h.events)

    def test_ready_ack_exact_fields_boot_session_hash_and_finite_time(self) -> None:
        mutations = (
            ("schema_version", True),
            ("run_id", "foreign"),
            ("status", "passed"),
            ("receipt_sha256", "0" * 64),
            ("native_bootstrap", {**BOOTSTRAP, "boot_id": RUN}),
            ("native_bootstrap", {**BOOTSTRAP, "pid": 9999}),
            ("monotonic", True),
            ("monotonic", "100"),
            ("monotonic", float("inf")),
            ("monotonic", float("nan")),
            ("extra", "not permitted"),
            ("missing", None),
            ("canonical-hash", None),
            ("receipt-changed", None),
        )
        for key, value in mutations:
            with (
                self.subTest(key=key, value=value),
                tempfile.TemporaryDirectory() as temporary,
            ):
                h = Harness(Path(temporary))
                if key == "missing":
                    h.ready_payload.pop("native_bootstrap")
                elif key == "canonical-hash":
                    h.ready_payload["receipt_sha256"] = hashlib.sha256(
                        json.dumps(json.loads(h.receipt.read_bytes())).encode()
                    ).hexdigest()
                elif key == "receipt-changed":
                    h.receipt.write_bytes(h.receipt.read_bytes() + b" ")
                else:
                    h.ready_payload[key] = value
                with self.assertRaisesRegex(RuntimeError, "ready ACK"):
                    h.capture.run()
                self.assertNotIn("radio-on", h.events)
                self.assertNotIn("url", h.events)

    def test_early_changed_and_removed_ready_ack_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.ready()
            with self.assertRaisesRegex(RuntimeError, "early adapter ready"):
                h.capture.run()
            self.assertNotIn("launch", h.events)
        for case in ("changed", "removed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary))

                def radio_on(case=case, h=h) -> None:
                    path = h.root / h.api.READY_NAME
                    if case == "changed":
                        path.write_bytes(path.read_bytes() + b" ")
                    else:
                        path.unlink()

                h.capture.radio_on = radio_on
                with self.assertRaisesRegex(
                    RuntimeError, "ready ACK (changed|disappeared)"
                ):
                    h.capture.run()
                self.assertNotIn("url", h.events)

    def test_fixture_exit_does_not_block_phone_owned_app_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            primary = RuntimeError("fixture exited")

            def radio_on() -> None:
                h.guard.side_effect = primary

            h.capture.radio_on = radio_on
            with self.assertRaises(RuntimeError) as caught:
                h.capture.run()
            self.assertIs(caught.exception, primary)
            self.assertEqual(sum("force-stop" in c for c in h.commands), 1)
            self.assertNotIn("url", h.events)

    def test_owner_loss_blocks_launch_url_and_cleanup_mutations(self) -> None:
        for boundary in ("launch", "url"):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory() as temporary,
            ):
                h = Harness(Path(temporary))
                if boundary == "launch":
                    original = h.popen

                    def popen(
                        argv: list[str], original=original, h=h, **kwargs: object
                    ) -> Child:
                        child = original(argv, **kwargs)
                        if "logcat" in argv:
                            h.phone_alive = False
                        return child

                    h.api.subprocess.Popen = popen
                else:

                    def radio_on(h=h) -> None:
                        h.phone_alive = False

                    h.capture.radio_on = radio_on
                with self.assertRaisesRegex(RuntimeError, "phone owner lost"):
                    h.capture.run()
                self.assertNotIn(boundary, h.events)
                self.assertFalse(any("force-stop" in c for c in h.commands))
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.capture.run()
            original = h.cleanup_command

            def query(argv: list[str], **kwargs: object) -> SimpleNamespace:
                result = original(argv, **kwargs)
                if "path" in argv:
                    h.phone_alive = False
                return result

            h.api.android_sdk.run_command.side_effect = query
            with self.assertRaisesRegex(RuntimeError, "phone owner lost"):
                h.api.abort_owned_app(
                    "synthetic-device", h.log, RUN, phone_guard=h.phone_guard
                )
            self.assertFalse(any("force-stop" in c for c in h.commands))

    def test_absent_physical_proof_rejected_control_and_changed_source(self) -> None:
        for case in ("physical", "control", "source"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary))
                if case == "physical":
                    h.make_physical = False
                elif case == "control":
                    h.control_output = "Error: permission denied"
                else:
                    original = h.deliver

                    def deliver(original=original, h=h) -> None:
                        original()
                        h.source = "/data/app/replaced/base.apk"

                    h.deliver = deliver
                with self.assertRaises((RuntimeError, TimeoutError)):
                    h.capture.run()
                self.assertEqual(h.events.count("url"), 1)
                if case == "source":
                    self.assertFalse(any("force-stop" in c for c in h.commands))


if __name__ == "__main__":
    unittest.main()
