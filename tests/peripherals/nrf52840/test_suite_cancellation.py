"""Synthetic writable GATT regressions through the real continuous-suite dispatcher."""

from __future__ import annotations

import copy
import struct
import unittest
from dataclasses import asdict
from unittest.mock import patch

from . import cancellation_control as controls
from . import fault_control, full_suite
from .btp_commands import GAP, GATT
from .btp_protocol import BtpFrame
from .gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
    GAP_DISCONNECT,
)
from .gatt_commands import (
    GATT_SET_VALUE,
)
from .resource_lease import ResourceLeaseError
from .support.read_fault_emulator import PEER
from .support.suite_cancellation_scenario import suite_cancellation_scenario


class SuiteCancellationTests(unittest.TestCase):
    def assert_blocked(
        self, case, error, message, *, mutated=False, guard=None, empty_peers=True
    ):
        session, plan = case.fixture._session, case.fixture._fixture_session
        with self.assertRaisesRegex(error, message):
            case.run_suite(guard)
        self.assertTrue(case.wire.io_written)
        self.assertEqual(
            case.wire.values[case.handles["read-write"]],
            b"\x00" if mutated else b"\x01",
        )
        self.assertFalse(case.wire.resumed)
        self.assertFalse(case.receipt.exists())
        if empty_peers:
            self.assertEqual(case.fixture._connected_peers, set())
        else:
            self.assertLessEqual(
                case.fixture._connected_peers, {(PEER[1:][::-1].hex(), PEER[0])}
            )
        self.assertIs(case.fixture._session, session)
        self.assertIs(case.fixture._fixture_session, plan)
        self.assertFalse(
            any(
                f.service == 255
                and f.opcode
                in (
                    fault_control.Opcode.ARM_READ,
                    fault_control.Opcode.ARM_WRITE,
                    fault_control.Opcode.ARM_CCC,
                    fault_control.Opcode.ABORT,
                )
                for f in case.wire.commands
            )
        )
        self.assertFalse(
            any(
                f.service == GAP and f.opcode == GAP_DISCONNECT
                for f in case.wire.commands[case.preparation_offset() :]
            )
        )

    def test_production_writer_signals_pass_real_relay_and_failures_publish_failed(
        self,
    ):
        for outcome in (
            "passed",
            "held-failure",
            "close-failure",
            "closed-publication-failure",
        ):
            with self.subTest(outcome=outcome), suite_cancellation_scenario() as case:
                relay = controls.ControlRelay(case.attempt.root, case.attempt.control)
                control = case.attempt.control
                lines = [
                    control.marker("listener", "registered"),
                    control.marker("ready", "arm"),
                ]
                accepted = []
                publications = []
                initialized = False
                injected = False
                write = controls.write_document

                def observe(relay=relay, lines=lines):
                    relay.observe(
                        "".join("BLEHUB_NITRO_RUNTIME " + line + "\n" for line in lines)
                    )

                def guard(
                    outcome=outcome,
                    relay=relay,
                    control=control,
                    lines=lines,
                    accepted=accepted,
                    observe=observe,
                ):
                    nonlocal initialized, injected
                    if not case.wire.resumed:
                        return
                    if not initialized:
                        # The real relay authors the request before the writer reads it.
                        observe()
                        initialized = True
                    phase = relay.pending()
                    if phase is not None:
                        evidence = controls.read_document(case.attempt.path)
                        self.assertEqual(evidence["status"], "running")
                        self.assertNotIn(
                            "closed",
                            [row["boundary"] for row in evidence["boundaries"]],
                        )
                        accepted.append(phase)
                        relay.forwarded(phase)
                        lines.append(control.marker("observed", phase))
                        if phase == "armed":
                            case.wire.capture_read()
                            lines.append(control.marker("ready", "read-dispatched"))
                        elif phase == "held":
                            lines.extend(
                                [
                                    "rf:step:cancellation-terminal:passed:commandId=9028:error=cancelled",
                                    control.marker("ready", "release"),
                                ]
                            )
                        observe()
                    if outcome == "held-failure" and "held" in accepted:
                        injected = True
                        raise RuntimeError("synthetic held failure")
                    if (
                        "released" in accepted
                        and not (case.attempt.directory / "done").exists()
                    ):
                        if outcome == "close-failure":
                            injected = True
                            raise RuntimeError("synthetic close failure")
                        case.wire.gap(GAP_DEVICE_DISCONNECTED)
                        (case.attempt.directory / "done").touch()
                        (case.attempt.root / "flow-9-rf-watchdog/started").touch()

                def publish(
                    path,
                    document,
                    outcome=outcome,
                    write=write,
                    publications=publications,
                ):
                    nonlocal injected
                    if path == case.attempt.path:
                        if (
                            outcome == "closed-publication-failure"
                            and not injected
                            and document["boundaries"][-1]["boundary"] == "closed"
                        ):
                            self.assertEqual(document["status"], "passed")
                            injected = True
                            raise OSError("synthetic closed publication failure")
                        if injected:
                            self.assertEqual(document["status"], "failed")
                            self.assertIn("primary_error", document)
                    write(path, document)
                    if path == case.attempt.path:
                        publications.append(copy.deepcopy(document))

                with patch.object(controls, "write_document", side_effect=publish):
                    if outcome == "passed":
                        case.run_suite(guard)
                    else:
                        with self.assertRaisesRegex(
                            (RuntimeError, OSError), "synthetic .* failure"
                        ):
                            case.run_suite(guard)
                self.assertEqual(
                    accepted,
                    ["armed", "held"]
                    if outcome == "held-failure"
                    else ["armed", "held", "released"],
                )
                self.assertEqual(relay.sent, accepted)
                self.assertTrue(publications)
                if outcome == "passed":
                    self.assertEqual(publications[-1]["status"], "passed")
                    self.assertEqual(
                        publications[-1]["boundaries"][-1]["boundary"], "closed"
                    )
                    self.assertTrue(
                        all(p["status"] == "running" for p in publications[:-1])
                    )
                    self.assertEqual(case.fixture._connected_peers, set())
                else:
                    self.assertTrue(injected)
                    self.assertEqual(publications[-1]["status"], "failed")
                    cleanup = [
                        p
                        for p in publications
                        if p["boundaries"][-1]["boundary"] == "cleanup"
                    ]
                    self.assertTrue(cleanup)
                    self.assertTrue(all(p["status"] == "failed" for p in cleanup))
                    with self.assertRaisesRegex(
                        RuntimeError, "physical cancellation barrier failed"
                    ):
                        relay.pending()

    def test_real_dispatcher_restores_persistent_io_value_before_real_cancellation_arm(
        self,
    ):
        with suite_cancellation_scenario() as case:
            session = case.fixture._session
            case.run_suite().assert_called_once()
            self.assertEqual(
                case.wire.trace,
                [
                    "rf-io:01",
                    "flow7-close",
                    "stop",
                    "idle-1",
                    "attributes",
                    "before",
                    "idle-2",
                    "set",
                    "after",
                    "idle-3",
                    "resume",
                ],
            )
            receipt = controls.read_document(case.receipt)
            self.assertEqual(
                (receipt["before"], receipt["expected"], receipt["readback"]),
                ("01", "00", "00"),
            )
            self.assertEqual(receipt["control"], asdict(case.attempt.control))
            self.assertEqual(
                receipt["resource_identity"], case.attempt.plan.resource_identity
            )
            self.assertEqual(
                (
                    receipt["engine_flags"],
                    receipt["peer_present"],
                    receipt["peer_count"],
                ),
                (0, False, 0),
            )
            self.assertTrue(
                all(
                    l.boot_nonce.hex() == receipt["boot_nonce"]
                    for l in case.wire.prep_limits
                )
            )
            evidence = controls.read_document(case.attempt.path)
            self.assertEqual(evidence["status"], "passed")
            self.assertEqual(evidence["preparation"], receipt)
            close = receipt["passive_recovery_close"]
            self.assertEqual(
                [r["opcode"] for r in close["events"]],
                [GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED] * 2,
            )
            self.assertLessEqual(close["monotonic"], receipt["monotonic"])
            controls.check_physical_evidence(
                case.attempt.path, case.cancellation_log(), case.profile.service_uuid
            )
            self.assertEqual(
                case.wire.remote_reads, [(1, case.handles["read-write"], b"\x00")]
            )
            self.assertIs(case.fixture._session, session)
            case.fixture._transport.require.assert_called()
            writes = [
                f
                for f in case.wire.commands
                if f.service == GATT
                and f.opcode == GATT_SET_VALUE
                and struct.unpack("<H", f.payload[:2])[0] == case.handles["read-write"]
            ]
            self.assertEqual(len(writes), 1)

    def test_recovery_lifecycle_queued_delayed_or_split_across_done(self):
        for schedule in ("queued", "delayed", "split"):
            with self.subTest(schedule=schedule), suite_cancellation_scenario() as case:
                case.wire.recovery_schedule = schedule
                case.run_suite()
                receipt = controls.read_document(case.receipt)
                events = receipt["passive_recovery_close"]["events"]
                self.assertEqual(len(events), 4)
                self.assertEqual(case.wire.recovery_emitted, 2)
                self.assertEqual(
                    case.wire.done_when_recovery_emitted,
                    {"queued": 2, "delayed": 0, "split": 1}[schedule],
                )
                self.assertTrue(case.wire.recovery_stopped)
                self.assertEqual(case.fixture._connected_peers, set())
                self.assertEqual(receipt["before"], "01")
                self.assertEqual(
                    controls.read_document(case.attempt.path)["preparation"], receipt
                )
                controls.check_physical_evidence(
                    case.attempt.path,
                    case.cancellation_log(),
                    case.profile.service_uuid,
                )

    def test_recovery_close_rejects_foreign_duplicate_out_of_order_and_malformed_events(
        self,
    ):
        connect = BtpFrame(GAP, GAP_DEVICE_CONNECTED, 0, PEER + bytes(6))
        disconnect = BtpFrame(GAP, GAP_DEVICE_DISCONNECTED, 0, PEER)
        sequences = (
            [disconnect],
            [connect, connect],
            [connect, disconnect, disconnect],
            [connect, disconnect, connect],
            [BtpFrame(GAP, GAP_DEVICE_CONNECTED, 0, b"\x01" + bytes(12))],
            [connect, BtpFrame(GAP, GAP_DEVICE_DISCONNECTED, 0, b"\x01" + bytes(6))],
            [BtpFrame(GAP, GAP_DEVICE_CONNECTED, 0, PEER)],
            [BtpFrame(GAP, GAP_DEVICE_CONNECTED, 1, PEER + bytes(6))],
            [BtpFrame(GAP, GAP_DEVICE_CONNECTED, 0, b"\x04" + PEER[1:] + bytes(6))],
            [BtpFrame(GAP, GAP_DEVICE_CONNECTED, False, PEER + bytes(6))],
            [connect, BtpFrame(GAP, GAP_DEVICE_DISCONNECTED, 0, PEER + b"\x00")],
        )
        for events in sequences:
            with self.subTest(events=events), suite_cancellation_scenario() as case:
                case.wire.recovery_events = events
                self.assert_blocked(
                    case,
                    RuntimeError,
                    "suite passive recovery close.*(event=|event BtpFrame)",
                    empty_peers=False,
                )
                self.assertFalse(case.wire.preparing)

    def test_recovery_close_requires_both_events_even_after_done_and_obeys_guards(self):
        for kind in (
            "no-connect",
            "no-disconnect",
            "lease",
            "abort",
            "capture",
            "transport",
        ):
            with self.subTest(kind=kind), suite_cancellation_scenario() as case:
                if kind == "no-connect":
                    case.wire.recovery_events = []
                elif kind == "no-disconnect":
                    case.wire.recovery_events = case.wire.recovery_events[:1]
                else:
                    case.wire.recovery_schedule = "delayed"

                def guard(kind=kind):
                    case.guard()
                    if case.wire.recovery_started is not None:
                        if kind == "lease":
                            case.fixture._transport.require.side_effect = (
                                ResourceLeaseError("recovery lease lost")
                            )
                        elif kind == "abort":
                            (
                                case.attempt.root / "flow-7-rf-passive-disconnect/abort"
                            ).touch()
                        elif kind == "capture":
                            raise RuntimeError("recovery capture failed")
                        elif kind == "transport":
                            raise EOFError("recovery transport lost")

                self.assert_blocked(
                    case,
                    (RuntimeError, TimeoutError, EOFError),
                    "recovery close deadline|lease lost|aborted|capture failed|transport lost",
                    guard=guard,
                    empty_peers=False,
                )
                self.assertFalse(case.wire.preparing)
                if kind.startswith("no-"):
                    self.assertLess(case.clock.now, 161)

    def test_embedded_preparation_checker_rejects_shape_binding_and_wire_tampering(
        self,
    ):
        mutations = [
            lambda p: p.update(extra="unknown"),
            lambda p: p.pop("before"),
            lambda p: p.update(schema_version=True),
            lambda p: p.update(status="passed"),
            lambda p: p.update(target_handle=True),
            lambda p: p.update(target_handle=p["target_handle"] + 1),
            lambda p: p.update(target_uuid="foreign"),
            lambda p: p.update(fixture_pid=p["fixture_pid"] + 1),
            lambda p: p.update(resource_identity="jlink:foreign"),
            lambda p: p.update(profile_sha256="00" * 32),
            lambda p: p.update(contract_sha256="00" * 32),
            lambda p: p.update(session_id=p["control"]["token"]),
            lambda p: p["control"].update(token=p["control"]["session"]),
            lambda p: p.update(boot_nonce="00" * 16),
            lambda p: p.update(readback="01"),
            lambda p: p.update(expected="01"),
            lambda p: p.update(before="0A"),
            lambda p: p.update(engine_flags=False),
            lambda p: p.update(peer_present=0),
            lambda p: p.update(peer_count=True),
            lambda p: p.update(monotonic=float("nan")),
            lambda p: p.update(monotonic=p["monotonic"] + 100),
            lambda p: p["passive_recovery_close"].update(extra="unknown"),
            lambda p: p["passive_recovery_close"].update(advertising_stopped=False),
            lambda p: p["passive_recovery_close"].update(peer_count=True),
            lambda p: p["passive_recovery_close"].update(peer=["000000000000", 1]),
            lambda p: p["passive_recovery_close"]["events"].pop(),
            lambda p: p["passive_recovery_close"]["events"][2].update(index=True),
            lambda p: p["passive_recovery_close"]["events"][2].update(
                payload=PEER.hex()
            ),
            lambda p: p["passive_recovery_close"]["events"][2].update(
                opcode=GAP_DEVICE_DISCONNECTED
            ),
            lambda p: p["passive_recovery_close"]["events"][3].update(monotonic=0),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), suite_cancellation_scenario() as case:
                case.run_suite()
                evidence = controls.read_document(case.attempt.path)
                changed = copy.deepcopy(evidence)
                mutate(changed["preparation"])
                controls.write_document(case.attempt.path, changed)
                with self.assertRaises(ValueError):
                    controls.check_physical_evidence(
                        case.attempt.path,
                        case.cancellation_log(),
                        case.profile.service_uuid,
                    )
        with suite_cancellation_scenario() as case:
            case.run_suite()
            evidence = controls.read_document(case.attempt.path)
            changed = copy.deepcopy(evidence)
            changed.pop("preparation")
            controls.write_document(case.attempt.path, changed)
            with self.assertRaises(ValueError):
                controls.check_physical_evidence(
                    case.attempt.path,
                    case.cancellation_log(),
                    case.profile.service_uuid,
                )
            # The original standalone report shape is still accepted without suite scope.
            controls.check_physical_evidence(
                case.attempt.path,
                case.cancellation_log().split("\n", 1)[1],
                case.profile.service_uuid,
            )

    def test_peer_or_active_engine_at_either_prewrite_probe_never_mutates(self):
        for step in ("idle-1", "idle-2"):
            for kind in ("peer", "active"):
                with (
                    self.subTest(step=step, kind=kind),
                    suite_cancellation_scenario() as case,
                ):

                    def inject(current, step=step, kind=kind):
                        if current == step:
                            if kind == "peer":
                                case.wire.connected = True
                                case.wire.epoch = 1
                            else:
                                case.wire.phase = fault_control.Phase.ARMED

                    case.wire.inject = inject
                    self.assert_blocked(
                        case,
                        (RuntimeError, ValueError),
                        "peer/boot drift|foreign resources",
                    )

    def test_unowned_and_malformed_gap_at_every_prewrite_command_never_mutates(self):
        events = (
            BtpFrame(GAP, GAP_DEVICE_CONNECTED, 0, PEER + bytes(6)),
            BtpFrame(GAP, GAP_DEVICE_DISCONNECTED, 0, PEER),
            BtpFrame(GAP, GAP_DEVICE_CONNECTED, 0, PEER),
            BtpFrame(GAP, GAP_DEVICE_DISCONNECTED, 1, PEER),
            BtpFrame(GAP, GAP_DEVICE_CONNECTED, 0, b"\x04" + PEER[1:] + bytes(6)),
        )
        for step in ("stop", "idle-1", "attributes", "before", "idle-2"):
            for event in events:
                with (
                    self.subTest(step=step, event=event),
                    suite_cancellation_scenario() as case,
                ):
                    case.wire.inject = lambda current, step=step, event=event: (
                        case.wire.frames.append(event) if current == step else None
                    )
                    self.assert_blocked(
                        case, RuntimeError, "preparation GAP peer event"
                    )

    def test_transition_events_are_not_consumed_by_permissive_teardown(self):
        with suite_cancellation_scenario() as case:
            injected = False

            def guard():
                nonlocal injected
                case.guard()
                if (
                    not injected
                    and case.wire.recovery_stopped
                    and not case.wire.preparing
                ):
                    injected = True
                    case.wire.frames.append(
                        BtpFrame(GAP, GAP_DEVICE_CONNECTED, 0, PEER + bytes(6))
                    )

            self.assert_blocked(
                case, RuntimeError, "recovery close unexpected GAP.*event=", guard=guard
            )
            self.assertTrue(injected)

    def test_event_backlog_and_idle_poll_have_bounded_preparation_budget(self):
        for kind in ("backlog", "poll-deadline"):
            with self.subTest(kind=kind), suite_cancellation_scenario() as case:
                if kind == "backlog":

                    def inject(step):
                        if step == "stop":
                            case.wire.frames.extend(
                                BtpFrame(GATT, 0x80, 0, b"") for _ in range(256)
                            )

                    case.wire.inject = inject
                    self.assert_blocked(
                        case, RuntimeError, "event backlog did not reach idle"
                    )
                else:
                    receive = case.wire.receive

                    def delayed_poll(timeout, receive=receive):
                        if case.wire.preparing and not case.wire.frames:
                            case.clock.now += 10
                            raise TimeoutError("synthetic idle deadline")
                        return receive(timeout)

                    with patch.object(case.wire, "receive", side_effect=delayed_poll):
                        self.assert_blocked(
                            case, RuntimeError, "event deadline exceeded"
                        )
                self.assertEqual(case.wire.trace, ["rf-io:01", "flow7-close", "stop"])

    def test_real_full_suite_capture_guard_blocks_write_on_rewind_or_failure(self):
        for kind in ("rewind", "failed"):
            with self.subTest(kind=kind), suite_cancellation_scenario() as case:
                full_suite.prepare_control(case.attempt.root, case.attempt.plan)
                snapshot = case.attempt.root / "runtime.log"
                prefix = "BLEHUB_NITRO_RUNTIME "
                snapshot.write_text(prefix + "capture-start\n", encoding="utf-8")
                parent = full_suite.FullSuite(
                    case.fixture, case.attempt.root, case.handles
                )

                def inject(step, kind=kind, prefix=prefix, snapshot=snapshot):
                    if step == "before":
                        text = (
                            prefix + "changed\n"
                            if kind == "rewind"
                            else prefix
                            + "capture-start\n"
                            + prefix
                            + "rf:failed:synthetic\n"
                        )
                        snapshot.write_text(text, encoding="utf-8")

                case.wire.inject = inject
                self.assert_blocked(
                    case,
                    RuntimeError,
                    "snapshot rewound/changed|runtime reported failure",
                    guard=parent.base_guard,
                )

    def test_receipt_publication_failure_blocks_resume_and_arm(self):
        with suite_cancellation_scenario() as case:
            write = controls.write_document

            def publish(path, value):
                if path == case.receipt:
                    raise OSError("preparation publication failed")
                return write(path, value)

            with patch.object(controls, "write_document", side_effect=publish):
                self.assert_blocked(case, OSError, "publication failed", mutated=True)

    def test_target_att_and_transport_failure_before_write_fail_closed(self):
        for kind in ("permissions", "before-att", "transport", "boot"):
            with self.subTest(kind=kind), suite_cancellation_scenario() as case:

                def inject(step, kind=kind):
                    if step == "attributes" and kind == "permissions":
                        case.wire.target_permissions = 3
                    if step == "before" and kind == "before-att":
                        case.wire.read_att = 2
                    if step == "before" and kind == "transport":
                        raise EOFError("transport lost")
                    if step == "idle-2" and kind == "boot":
                        case.wire.boot_drift = True

                case.wire.inject = inject
                self.assert_blocked(
                    case,
                    EOFError if kind == "transport" else RuntimeError,
                    "target/profile|before ATT|transport lost|peer/boot drift",
                )

    def test_guard_lease_abort_capture_and_absolute_budget_before_write(self):
        for kind in ("lease", "abort", "stop", "capture", "deadline"):
            with self.subTest(kind=kind), suite_cancellation_scenario() as case:

                def inject(step, kind=kind):
                    if step == "before":
                        if kind == "lease":
                            case.fixture._transport.require.side_effect = (
                                ResourceLeaseError("lease lost")
                            )
                        elif kind == "abort":
                            (case.attempt.directory / "abort").touch()
                        elif kind == "stop":
                            case.fixture._stop_requested = True
                        elif kind == "deadline":
                            case.clock.now += 10

                def guard(kind=kind):
                    case.guard()
                    if kind == "capture" and case.wire.step == "before":
                        raise RuntimeError("capture rewound")

                case.wire.inject = inject
                self.assert_blocked(
                    case,
                    RuntimeError if kind != "deadline" else TimeoutError,
                    "lease lost|aborted|capture rewound|timed out|deadline",
                    guard=guard,
                )

    def test_postwrite_mismatch_gap_busy_peer_boot_and_guard_block_resume_and_arm(self):
        for kind in (
            "mismatch",
            "att",
            "gap-set",
            "gap-after",
            "gap-final",
            "busy",
            "peer",
            "boot",
            "capture",
            "lease",
        ):
            with self.subTest(kind=kind), suite_cancellation_scenario() as case:

                def inject(step, kind=kind):
                    if step == "set" and kind == "mismatch":
                        case.wire.ignore_write = True
                    if step == "after" and kind == "att":
                        case.wire.read_att = 2
                    if (step, kind) in (
                        ("set", "gap-set"),
                        ("after", "gap-after"),
                        ("idle-3", "gap-final"),
                    ):
                        case.wire.frames.append(
                            BtpFrame(GAP, GAP_DEVICE_CONNECTED, 0, PEER + bytes(6))
                        )
                    if step == "idle-3":
                        if kind == "busy":
                            case.wire.phase = fault_control.Phase.ARMED
                        elif kind == "peer":
                            case.wire.connected, case.wire.epoch = True, 1
                        elif kind == "boot":
                            case.wire.boot_drift = True
                        elif kind == "lease":
                            case.fixture._transport.require.side_effect = (
                                ResourceLeaseError("lease lost")
                            )

                def guard(kind=kind):
                    case.guard()
                    if kind == "capture" and case.wire.step == "idle-3":
                        raise RuntimeError("capture rewound")

                case.wire.inject = inject
                self.assert_blocked(
                    case,
                    (RuntimeError, ValueError),
                    "readback mismatch|preparation GAP|foreign resources|peer/boot drift|capture rewound|lease lost",
                    mutated=kind != "mismatch",
                    guard=guard,
                )
