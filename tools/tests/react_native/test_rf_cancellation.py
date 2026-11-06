"""Standalone v2 cancellation and shared single-control runtime; no hardware."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import fault_control as fault
from tests.peripherals.nrf52840.btp_protocol import BtpFrame
from tests.peripherals.nrf52840.btp_session import BtpSession
from tests.peripherals.nrf52840.cancellation_control import Control, control_markers
from tests.peripherals.nrf52840.gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
)
from tests.peripherals.nrf52840.profile import load_profile
from tests.peripherals.nrf52840.support.read_fault_emulator import TARGET, Clock, Wire
from tests.peripherals.nrf52840.support.session_receipts import write_v2_inputs
from tests.support.rf_lab.markers import (
    CANCELLATION_MARKERS,
    SERVICE,
    controlled_markers,
    log,
)
from tools.react_native import rf_control_relay as relay
from tools.react_native import run_rf_cancellation as cancellation
from tools.react_native import run_rf_watchdog as runner
from tools.tests.react_native.support.ios_receipts import (
    host_marker,
    ios_bootstrap,
    write_ios_install,
)

RUN = "12345678-1234-4234-8234-123456789abc"
CONTROL = Control(RUN, 8, "rf-cancellation", "11111111-1111-4111-8111-111111111111")


class Central:
    """Exercise the existing barrier, with a real typed BTP session below it."""

    def __init__(self, wire: Wire, profile, root: Path) -> None:
        self.wire, self.control = wire, CONTROL
        self.log = root / "runtime.log"
        self.service, self.local_name = profile.service_uuid, profile.local_name
        text = log(controlled_markers(CANCELLATION_MARKERS, CONTROL)) + "\n"
        text = text.replace(SERVICE, profile.service_uuid).replace(
            "localName=missing", "localName=" + profile.local_name
        )
        self.head, text = text.split(log([CONTROL.marker("observed", "armed")]) + "\n")
        _, text = text.split(log([CONTROL.marker("observed", "held")]) + "\n")
        self.middle, self.tail = text.split(
            log([CONTROL.marker("observed", "released")]) + "\n"
        )
        self.text = ""
        self.signals: list[str] = []
        self.requests: list[str] = []

    def launch(self) -> None:
        assert not any(
            frame.opcode == fault.Opcode.ARM_READ for frame in self.wire.commands
        )
        self.wire.gap(GAP_DEVICE_CONNECTED)
        self.text = (
            log(
                [
                    f"rf:start:serviceUuid={self.service}:expectedLocalName={self.local_name}:initCommandId=9001:"
                ]
            )
            + "\n"
            + self.head
        )

    def wait_control_request(self, request: str, deadline: float) -> None:
        self.requests.append(request)
        if request == "release":
            if self.wire.fault == "late-gate":
                self.wire.clock.now += 7
            elif self.wire.fault == "disconnect":
                self.wire.gap(GAP_DEVICE_DISCONNECTED)
            elif self.wire.fault == "wrong-terminal":
                self.text = self.text.replace(
                    "commandId=9028:error=cancelled", "commandId=9027:error=cancelled"
                )
            elif self.wire.fault == "duplicate-terminal":
                self.text += (
                    log(
                        [
                            "rf:step:cancellation-terminal:passed:commandId=9028:error=cancelled"
                        ]
                    )
                    + "\n"
                )
        if self.wire.clock.now >= deadline:
            raise runner.WatchdogError("control request deadline")
        assert CONTROL.marker("ready", request) in self.text

    def signal(self, phase: str) -> None:
        self.signals.append(phase)
        self.text += log([CONTROL.marker("observed", phase)]) + "\n"
        if phase == "armed":
            assert self.wire.phase is fault.Phase.ARMED and self.wire.connected
            if self.wire.fault != "missing-held":
                self.wire.capture_read()
            self.text += log([CONTROL.marker("ready", "read-dispatched")]) + "\n"
        elif phase == "held":
            self.text += self.middle
        elif phase == "released":
            assert self.wire.phase is fault.Phase.RETIRED and self.wire.connected
            self.wire.gap(GAP_DEVICE_DISCONNECTED)
            self.text += self.tail

    def snapshot(self, *, allow_exit: bool = False) -> str:
        return self.text

    def completed(self, text: str) -> bool:
        return "BLEHUB_NITRO_RUNTIME rf-cancellation:passed:" in text


class ReceiptWire(Wire):
    def send(self, frame: BtpFrame) -> None:
        super().send(frame)
        if frame.opcode != fault.Opcode.STATUS:
            return
        response = self.frames.pop()
        status = fault.decode_status(response.payload)
        if self.fault == "before-drift" and status.phase is fault.Phase.HELD:
            status = replace(status, before=b"\x01")
        elif self.fault == "pinned-evidence" and status.phase is fault.Phase.RETIRED:
            status = replace(status, refs=replace(status.refs, evidence_pending=1))
        elif self.fault == "foreign-ticket":
            status = replace(
                status,
                ticket=replace(status.ticket, sequence=status.ticket.sequence + 1),
            )
        self.frames.append(replace(response, payload=fault.encode_status(status)))


class StandaloneCancellationTests(unittest.TestCase):
    def make(self, root: Path, mode: str = ""):
        _, profile_path = write_v2_inputs(root)
        profile = load_profile(profile_path)
        wire = ReceiptWire(Clock(), mode)
        owner = cancellation.CancellationExperiment(
            BtpSession(wire),
            profile,
            Mock(),
            {"session_id": RUN, "control": asdict(CONTROL)},
        )
        owner.mutation_allowed = True
        owner.gatt = Mock()
        owner.gatt.get_attribute_value.return_value = SimpleNamespace(
            att_response=0, value=b"\x00"
        )
        owner.gap = Mock()
        owner.gap.disconnect.side_effect = lambda *_: wire.gap(GAP_DEVICE_DISCONNECTED)
        return owner, Central(wire, profile, root), wire

    def test_barrier_binds_live_peer_and_normal_read_then_retained_status_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owner, runtime, wire = self.make(Path(directory))
            with (
                patch.object(
                    runner.time, "monotonic", side_effect=wire.clock.monotonic
                ),
                patch.object(runner.time, "sleep", side_effect=wire.clock.sleep),
            ):
                owner.run(runtime, TARGET)
                boundary = len(wire.commands)
                owner.cleanup_v2()
            self.assertEqual(runtime.requests, ["arm", "read-dispatched", "release"])
            self.assertEqual(runtime.signals, ["armed", "held", "released"])
            arm = next(
                frame
                for frame in wire.commands
                if frame.opcode == fault.Opcode.ARM_READ
            )
            request = fault.ArmRequest.decode(fault.Opcode.ARM_READ, arm.payload)
            self.assertEqual(request.common.owner_nonce, fault.owner_nonce(RUN))
            self.assertEqual((request.common.handle, request.before), (TARGET, b"\x00"))
            self.assertEqual(owner.report["ledger"]["outcome"], "NORMAL_NO_COMMIT")
            self.assertEqual(owner.report["physical_cleanup_errors"], [])
            self.assertEqual(
                sum(frame.opcode == fault.Opcode.RESOLVE for frame in wire.commands), 1
            )
            self.assertFalse(
                any(
                    frame.opcode in (fault.Opcode.RESOLVE, fault.Opcode.ABORT)
                    for frame in wire.commands[boundary:]
                )
            )
            self.assertEqual(wire.remote_reads, [(1, TARGET, b"\x00")])

    def test_bad_capture_terminal_deadline_or_owner_never_resolves(self) -> None:
        for mode in (
            "missing-held",
            "late-gate",
            "disconnect",
            "wrong-terminal",
            "duplicate-terminal",
            "before-drift",
            "foreign-ticket",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                owner, runtime, wire = self.make(Path(directory), mode)
                with (
                    patch.object(
                        runner.time, "monotonic", side_effect=wire.clock.monotonic
                    ),
                    patch.object(runner.time, "sleep", side_effect=wire.clock.sleep),
                    self.assertRaises((runner.WatchdogError, fault.FaultWireError)),
                ):
                    owner.run(runtime, TARGET)
                self.assertFalse(
                    any(frame.opcode == fault.Opcode.RESOLVE for frame in wire.commands)
                )
                self.assertNotIn("ledger", owner.report)
                self.assertNotIn("released", runtime.signals)
                if mode == "foreign-ticket":
                    with self.assertRaises(runner.WatchdogError):
                        owner.cleanup_v2()
                    owner.gap.stop_advertising.assert_not_called()

    def test_pinned_receipt_cannot_release_app_or_claim_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owner, runtime, wire = self.make(Path(directory), "pinned-evidence")
            with (
                patch.object(
                    runner.time, "monotonic", side_effect=wire.clock.monotonic
                ),
                patch.object(runner.time, "sleep", side_effect=wire.clock.sleep),
            ):
                with self.assertRaisesRegex(runner.WatchdogError, "NORMAL_NO_COMMIT"):
                    owner.run(runtime, TARGET)
                wire.gap(GAP_DEVICE_DISCONNECTED)
                with self.assertRaisesRegex(runner.WatchdogError, "cleanup blocked"):
                    owner.cleanup_v2()
            self.assertNotIn("released", runtime.signals)
            self.assertNotIn("ledger", owner.report)

    def test_native_projection_drift_cannot_turn_resolved_read_into_host_pass(
        self,
    ) -> None:
        for old, new in (
            (":value=none|00|none|00|none", ":value=none|00|none|10|none"),
            (
                ":commandId=9028|9027|9027|9029|9029:",
                ":commandId=9027|9027|9028|9029|9029:",
            ),
        ):
            with self.subTest(new=new), tempfile.TemporaryDirectory() as directory:
                owner, runtime, wire = self.make(Path(directory))
                runtime.tail = runtime.tail.replace(old, new)
                with (
                    patch.object(
                        runner.time, "monotonic", side_effect=wire.clock.monotonic
                    ),
                    patch.object(runner.time, "sleep", side_effect=wire.clock.sleep),
                    self.assertRaises((AssertionError, runner.WatchdogError)),
                ):
                    owner.run(runtime, TARGET)
                self.assertNotIn("ledger", owner.report)

    def test_single_control_launch_metadata_and_pinned_android_capture(self) -> None:
        for mode, flow in (("rf-cancellation", 8), ("rf-watchdog", 9)):
            for platform in ("android", "ios"):
                with (
                    self.subTest(mode=mode, platform=platform),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    runtime = runner.Runtime(
                        platform, "synthetic-device", root, Mock(), mode=mode
                    )
                    runtime.control = replace(CONTROL, mode=mode, flow=flow)
                    runtime.expected_local_name = "BleHubMultiRfV2DKA"
                    runtime.ios_identity = Mock() if platform == "ios" else None
                    runtime.adb = Mock(return_value=SimpleNamespace(stdout="123\n"))
                    with patch.object(runner.subprocess, "Popen") as process:
                        process.return_value.poll.return_value = 0
                        if platform == "android":
                            with patch.object(runtime, "start_android_capture"):
                                runtime.launch()
                            args = runtime.adb.call_args_list[0].args
                            self.assertEqual(
                                args[args.index("blehub.runtimeMode") + 1], mode
                            )
                            control_arg, name_arg = (
                                "blehub.rfCancellationControl",
                                "blehub.rfExpectedLocalName",
                            )
                        else:
                            runtime.launch()
                            args = process.call_args.args[0]
                            self.assertEqual(
                                args[args.index("--" + mode + "-service-uuid") + 1],
                                runtime.service_uuid,
                            )
                            control_arg, name_arg = (
                                "--rf-control-json",
                                "--rf-expected-local-name",
                            )
                        self.assertEqual(
                            json.loads(args[args.index(control_arg) + 1]),
                            asdict(runtime.control),
                        )
                        self.assertEqual(
                            args[args.index(name_arg) + 1], runtime.expected_local_name
                        )
                        runtime.stop(success=True)
                    if platform == "android":
                        raw = "".join(
                            "10-18 12:00:00.001 123 456 I ReactNativeJS: " + line + "\n"
                            for line in (
                                log([runtime.control.marker("listener", "registered")]),
                                log([runtime.control.marker("ready", "arm")]),
                                log([mode + ":passed:test"]),
                            )
                        )
                        self.assertEqual(len(runtime.android_marker_records(raw)), 3)
                        with self.assertRaisesRegex(
                            runner.WatchdogError, "pinned Android PID"
                        ):
                            runtime.android_marker_records(
                                raw.replace("123 456", "124 456")
                            )
                        self.assertTrue(
                            runtime.completed(
                                log(
                                    [
                                        mode
                                        + (":passed" if flow == 9 else ":passed:test")
                                    ]
                                )
                            )
                        )

    def test_runtime_barrier_counts_exact_control_and_rejects_duplicate_or_late_request(
        self,
    ) -> None:
        for mode, flow in (("rf-cancellation", 8), ("rf-watchdog", 9)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                runtime = runner.Runtime(
                    "android", "synthetic", Path(directory), Mock(), mode=mode
                )
                runtime.control = replace(CONTROL, mode=mode, flow=flow)
                ready = log([runtime.control.marker("ready", "arm")])
                runtime.snapshot = Mock(return_value=ready)
                runtime.wait_control_request("arm", runner.time.monotonic() + 1)
                runtime.snapshot.return_value = ready + "\n" + ready
                with self.assertRaisesRegex(runner.WatchdogError, "duplicate"):
                    runtime.wait_control_request("arm", runner.time.monotonic() + 1)
                runtime.snapshot.return_value = ready
                with self.assertRaisesRegex(runner.WatchdogError, "deadline"):
                    runtime.wait_control_request("arm", runner.time.monotonic() - 1)

    def test_ios_runtime_consumes_native_and_js_acks_in_existing_handoff(self) -> None:
        for mode, flow in (("rf-cancellation", 8), ("rf-watchdog", 9)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runtime = runner.Runtime("ios", "device", root, Mock(), mode=mode)
                control = runtime.control = replace(CONTROL, mode=mode, flow=flow)
                install = root / "install.json"
                write_ios_install(install)
                identity = runtime.ios_identity = relay.IosRuntimeIdentity(
                    "device", control, install, runtime.log
                )
                now = runner.time.monotonic()
                identity.begin_launch(now, now + 120)
                runtime.process = Mock()
                runtime.process.poll.return_value = None
                runtime.log.write_text(
                    host_marker("bootstrap", ios_bootstrap(control))
                    + "\n"
                    + log([control.marker("listener", "registered")])
                    + "\n"
                )

                def notify(
                    device: str,
                    actual: Control,
                    phase: str,
                    *,
                    control=control,
                    runtime=runtime,
                ) -> None:
                    self.assertEqual((device, actual), ("device", control))
                    with runtime.log.open("a") as stream:
                        stream.write(
                            host_marker(
                                "signal", {**ios_bootstrap(control), "phase": phase}
                            )
                            + "\n"
                        )
                        stream.write(log([control.marker("observed", phase)]) + "\n")

                with (
                    patch.object(identity, "_verify_process"),
                    patch.object(relay, "relay_ios", side_effect=notify) as send,
                ):
                    for request, phase in zip(
                        control.requests, control.phases, strict=True
                    ):
                        with runtime.log.open("a") as stream:
                            stream.write(log([control.marker("ready", request)]) + "\n")
                        runtime.wait_control_request(
                            request, runner.time.monotonic() + 1
                        )
                        runtime.signal(phase)
                        runtime.snapshot()
                    identity.finish(runtime.log.read_text())
                    self.assertEqual(send.call_count, len(control.phases))
                self.assertEqual(identity.acknowledged, list(control.phases))
                self.assertEqual(identity.js_acknowledged, list(control.phases))
                self.assertEqual(
                    [
                        body
                        for body in runtime.markers(runtime.log.read_text())
                        if body.startswith("rf-control:")
                    ],
                    control_markers(control),
                )
