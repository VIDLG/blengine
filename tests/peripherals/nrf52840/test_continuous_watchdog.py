"""Forward-only in-page watchdog regressions; synthetic BTP, never hardware/RF."""

from __future__ import annotations

import copy
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.react_native import run_rf_watchdog as standalone
from tools.tests.react_native.support.watchdog_runtime import Firmware, Runtime

from . import cancellation_control as controls
from . import continuous_watchdog as continuous
from . import fault_control as fault
from . import full_suite
from .btp_protocol import BtpFrame
from .btp_session import BtpSession
from .direct_fixture import DirectFixture
from .gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED
from .support.read_fault_emulator import PEER, Clock, read_receipt
from .support.session_receipts import fake_attempt
from .support.watchdog_reports import fake_watchdog


class ContinuousWatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.attempt = fake_attempt(Path(temporary.name))
        self.fake = fake_watchdog(self.attempt)
        self.ticket = fault.ticket_from_document(
            self.attempt.evidence["boundaries"][-1]["ticket"]
        )
        self.closed = fault.decode_status(
            bytes.fromhex(self.attempt.evidence["boundaries"][-1]["status_wire"])
        )
        self.handles = {v["role"]: v["handle"] for v in self.fake.restored}

    def check(self) -> None:
        continuous.check_physical_evidence(
            self.fake.path,
            self.fake.text,
            self.attempt.path,
            self.fake.profile.service_uuid,
        )

    def test_checker_binds_full_ticket_receipts_owner_route_budget_canonical_and_zero_resources(
        self,
    ) -> None:
        self.check()

        def receipt(evidence, name, change):
            row = next(b for b in evidence["boundaries"] if b["boundary"] == name)
            status = fault.decode_status(bytes.fromhex(row["status_wire"]))
            row["status_wire"] = fault.encode_status(change(status)).hex()

        mutations = [
            lambda e: e.update(schema_version=1),
            lambda e: e.update(schema_version=True),
            lambda e: e.update(extra="unknown"),
            lambda e: e.update(fixture_pid=e["fixture_pid"] + 1),
            lambda e: e.update(session_id=e["control"]["token"]),
            lambda e: e.update(target_native_permissions=3),
            lambda e: e["canonical_values"][1].update(value="ef", readback="ef"),
            lambda e: e["boundaries"][2].update(sequence=99),
            lambda e: e["boundaries"][4].update(native_route="31"),
            lambda e: e["boundaries"][4].update(
                monotonic=e["boundaries"][2]["monotonic"] + 16
            ),
            lambda e: e["boundaries"][5].update(observation_ms=999),
            lambda e: e["physical_cleanup"].update(hold_inactive=False),
            lambda e: e.update(physical_cleanup_errors=["missing disconnect"]),
            lambda e: receipt(
                e,
                "held",
                lambda s: replace(
                    s, ticket=replace(s.ticket, owner_nonce=bytes(range(16)))
                ),
            ),
            lambda e: receipt(
                e,
                "retired-after-watchdog",
                lambda s: replace(s, refs=replace(s.refs, evidence_pending=1)),
            ),
            lambda e: receipt(
                e,
                "retired-after-watchdog",
                lambda s: replace(s, flags=s.flags & ~fault.StatusFlags.REARMABLE),
            ),
            lambda e: receipt(
                e,
                "retired-after-watchdog",
                lambda s: replace(s, outcome=fault.Outcome.NORMAL_NO_COMMIT),
            ),
        ]
        for change in mutations:
            with self.subTest(change=change):
                evidence = copy.deepcopy(self.fake.evidence)
                change(evidence)
                controls.write_document(self.fake.path, evidence)
                with self.assertRaises((ValueError, RuntimeError)):
                    self.check()
        controls.write_document(self.fake.path, self.fake.evidence)
        original = self.fake.text
        for text in (
            original.replace(self.fake.control.token, self.fake.control.session),
            original + "BLEHUB_NITRO_RUNTIME rf-control:observed:foreign\n",
            original.replace("error=operation_aborted", "error=cancelled"),
            original.replace(self.fake.profile.local_name, "ForeignFixture"),
        ):
            self.fake.text = text
            with self.assertRaises((ValueError, RuntimeError)):
                self.check()
        self.fake.text = original

    def fixture(self, session=None):
        fixture = SimpleNamespace(
            _fixture_session=self.attempt.plan,
            _session=session or Mock(spec=BtpSession),
            _connected_peers=set(),
            _profile=self.fake.profile,
            _transport=Mock(),
            _gatt=Mock(),
            _gap=Mock(),
            _stop_requested=False,
        )
        if session is None:
            fixture._session.pop_event.return_value = None
            fixture._session.poll_event.return_value = None
        values = {
            self.handles[c.role]: c.initial_value
            for c in self.fake.profile.characteristics
        }
        fixture._gatt.get_attribute_value.side_effect = lambda handle: SimpleNamespace(
            att_response=0, value=values[handle]
        )
        rw = self.fake.profile.characteristics[0]
        fixture._gatt.get_attributes.return_value = [
            SimpleNamespace(
                handle=self.handles["read-write"], permissions=0x43, uuid=rw.uuid
            )
        ]
        return fixture

    def test_restore_exact_retained_receipt_before_reusing_handles_no_reset_or_peer_adoption(
        self,
    ) -> None:
        fixture = self.fixture()
        with patch.object(continuous, "FaultController") as controller:
            controller.return_value.status.return_value = self.closed
            controller.return_value.require_idle.return_value = SimpleNamespace(
                peer_present=False, boot_nonce=self.ticket.boot_nonce
            )
            self.assertEqual(
                continuous.restore_canonical(fixture, self.attempt.root, self.handles),
                self.fake.restored,
            )
            controller.return_value.status.assert_called_once_with(self.ticket)
            fixture._gatt.reset_server.assert_not_called()
            fixture._gap.disconnect.assert_not_called()
            for status in (
                replace(self.closed, phase=fault.Phase.HELD),
                replace(self.closed, refs=replace(self.closed.refs, tx_pending=1)),
            ):
                controller.return_value.status.return_value = status
                with self.assertRaisesRegex(RuntimeError, "resource-zero"):
                    continuous.restore_canonical(
                        fixture, self.attempt.root, self.handles
                    )

    def test_restore_rejects_gap_buffered_by_real_status_and_each_gatt_boundary(
        self,
    ) -> None:
        events = (
            BtpFrame(1, GAP_DEVICE_CONNECTED, 0, PEER + bytes(6)),
            BtpFrame(1, GAP_DEVICE_CONNECTED, 0, PEER),
            BtpFrame(1, GAP_DEVICE_DISCONNECTED, 0, PEER),
            BtpFrame(1, GAP_DEVICE_DISCONNECTED, 1, PEER),
            BtpFrame(1, GAP_DEVICE_CONNECTED, 0, b"\x04" + PEER[1:] + bytes(6)),
        )
        for event in events:
            frames = Mock()
            pending = []

            def send(frame, pending=pending, event=event):
                self.assertEqual(
                    (frame.service, frame.opcode, frame.index),
                    (255, fault.Opcode.STATUS, 255),
                )
                pending.extend(
                    [
                        event,
                        BtpFrame(
                            255,
                            fault.Opcode.STATUS,
                            255,
                            fault.encode_status(self.closed),
                        ),
                    ]
                )

            def receive(_, pending=pending):
                if pending:
                    return pending.pop(0)
                raise TimeoutError("idle")

            frames.send.side_effect, frames.receive.side_effect = send, receive
            fixture = self.fixture(BtpSession(frames))
            with (
                self.subTest(event=event),
                self.assertRaisesRegex(RuntimeError, "preparation GAP peer event"),
            ):
                continuous.restore_canonical(fixture, self.attempt.root, self.handles)
            fixture._gatt.set_value.assert_not_called()
            fixture._gap.disconnect.assert_not_called()
        for operation in ("set_value", "get_attribute_value"):
            fixture = self.fixture()
            queued = []
            fixture._session.pop_event.side_effect = lambda queued=queued: (
                queued.pop(0) if queued else None
            )

            def mutate(*_, queued=queued):
                queued.append(events[0])
                return SimpleNamespace(att_response=0, value=b"\x00")

            getattr(fixture._gatt, operation).side_effect = mutate
            with patch.object(continuous, "FaultController") as controller:
                controller.return_value.status.return_value = self.closed
                controller.return_value.require_idle.return_value = SimpleNamespace(
                    peer_present=False, boot_nonce=self.ticket.boot_nonce
                )
                with self.assertRaisesRegex(RuntimeError, "preparation GAP peer event"):
                    continuous.restore_canonical(
                        fixture, self.attempt.root, self.handles
                    )
                self.assertEqual(fixture._gatt.set_value.call_count, 1)
                fixture._gap.disconnect.assert_not_called()

    def test_existing_suite_advertises_only_after_canonical_preparation(self) -> None:
        modes = (
            "rf",
            "rf-connect",
            "rf-gatt",
            "rf-io",
            "rf-rssi",
            "rf-notification",
            "rf-indication",
            "rf-passive-disconnect",
            "rf-cancellation",
            "rf-watchdog",
        )
        for i, mode in enumerate(modes):
            directory = self.attempt.root / f"flow-{i}-{mode}"
            directory.mkdir(exist_ok=True)
            (directory / "started").touch()
            if i < 8:
                (directory / "abort").touch()
        for failed in (False, True):
            fixture = DirectFixture.__new__(DirectFixture)
            fixture._transport, fixture._gap = Mock(), Mock()
            fixture._stop_requested = False
            fixture._disconnect_connected_peers, fixture._restart_advertising = (
                Mock(),
                Mock(),
            )
            fixture.run_cancellation_coordination = Mock()
            with (
                patch.object(full_suite, "_prepare_suite_cancellation"),
                patch.object(DirectFixture, "_start_advertising"),
                patch.object(
                    continuous,
                    "restore_canonical",
                    side_effect=RuntimeError("preparation failed") if failed else None,
                    return_value=[],
                ) as prepare,
                patch.object(continuous, "run_connected") as connected,
            ):
                if failed:
                    with self.assertRaisesRegex(RuntimeError, "preparation failed"):
                        full_suite.run_continuous(fixture, self.attempt.root, {})
                    connected.assert_not_called()
                    self.assertEqual(fixture._restart_advertising.call_count, 16)
                else:
                    full_suite.run_continuous(fixture, self.attempt.root, {})
                    prepare.assert_called_once()
                    connected.assert_called_once()

    def test_live_event_types_absolute_deadline_and_backlog_fail_closed(self) -> None:
        fixture = self.fixture()
        for changes in (
            {},
            {"service": True},
            {"opcode": True},
            {"index": False},
            {"payload": bytearray(PEER)},
        ):
            event = SimpleNamespace(
                service=1, opcode=GAP_DEVICE_DISCONNECTED, index=0, payload=PEER
            )
            for key, value in changes.items():
                setattr(event, key, value)
            fixture._session.poll_event.return_value = event
            with self.assertRaises(RuntimeError):
                continuous._require_no_peer(fixture, time.monotonic() + 1)
        fixture._session.poll_event.return_value = None
        with (
            patch.object(continuous.time, "monotonic", side_effect=[100, 100, 101]),
            self.assertRaisesRegex(RuntimeError, "deadline"),
        ):
            continuous._require_no_peer(fixture, 101)
        fixture._session.pop_event.return_value = BtpFrame(2, 0x81, 0, b"")
        with self.assertRaisesRegex(RuntimeError, "backlog"):
            continuous._require_no_peer(fixture, time.monotonic() + 10)

    def test_in_page_executor_same_session_discovery_arm_no_launch_no_resolve_after_disconnect(
        self,
    ) -> None:
        clock = Clock()
        clock.now = self.attempt.evidence["boundaries"][-1]["monotonic"] + 1.5
        wire = Firmware(clock, "")
        wire.ticket, wire.sequence, wire.epoch = self.ticket, self.ticket.sequence, 1
        wire.phase, wire.winner, wire.captured, wire.resolving = (
            fault.Phase.RETIRED,
            fault.Winner.COMMIT,
            True,
            True,
        )
        central = Runtime(wire, self.fake.profile.service_uuid, self.fake.control)
        advertised = f'"advertisedServices": ["{self.fake.profile.service_uuid}"]'
        central.setup = central.setup.replace(
            advertised, f'"localName": "{self.fake.profile.local_name}", {advertised}'
        )
        central.launch()  # Existing runtime discovery occurs before host handshake.
        central.request = central.wait_control_request
        fixture = self.fixture(BtpSession(wire))
        with (
            patch.object(continuous, "ContinuousRuntime", return_value=central),
            patch.object(standalone, "GapCommands", return_value=fixture._gap),
            patch.object(standalone, "GattCommands", return_value=fixture._gatt),
            patch.object(standalone.Runtime, "launch") as launch,
            patch.object(standalone.time, "monotonic", side_effect=clock.monotonic),
            patch.object(standalone.time, "sleep", side_effect=clock.sleep),
        ):
            continuous.run_connected(
                fixture, self.attempt.root, self.handles, self.fake.restored
            )
            launch.assert_not_called()
        fixture._gatt.reset_server.assert_not_called()
        self.assertEqual(fixture._connected_peers, set())
        self.assertEqual(central.signals, ["armed", "held", "released", "recovery"])
        self.assertFalse(
            any(
                f.opcode in (fault.Opcode.RESOLVE, fault.Opcode.ABORT)
                for f in wire.commands
            )
        )
        report = controls.read_document(self.fake.path)
        self.assertEqual(report["status"], "passed")
        continuous.check_physical_evidence(
            self.fake.path,
            central.text,
            self.attempt.path,
            self.fake.profile.service_uuid,
        )
        self.assertTrue(
            fault.cleanup_ready(
                fault.decode_status(
                    bytes.fromhex(report["physical_cleanup"]["status_wire"])
                )
            )
        )

    def owner(self):
        owner = continuous.ContinuousExperiment(
            Mock(spec=BtpSession),
            self.fake.profile,
            Mock(),
            {"session_id": self.attempt.plan.session_id},
        )
        owner.mutation_allowed = True
        owner.ticket, owner.fault, owner.gap = self.ticket, Mock(), Mock()
        owner.fault.require_idle.return_value = SimpleNamespace(
            peer_present=False, boot_nonce=self.ticket.boot_nonce
        )
        owner.session.pop_event.return_value = None
        owner.session.poll_event.return_value = None
        return owner

    def test_cleanup_foreign_receipt_and_post_abort_drift_stop_before_gap_and_keep_primary(
        self,
    ) -> None:
        for owned in (False, True):
            owner = self.owner()
            owner.report["primary_error"] = "original failure"
            if not owned:
                owner.ticket = None
                owner.fault.require_idle.side_effect = fault.FaultWireError(
                    "foreign resources"
                )
            else:
                owner.fault.status.side_effect = fault.FaultWireError("foreign ticket")
            with self.assertRaisesRegex((RuntimeError, ValueError), "foreign"):
                owner.cleanup()
            self.assertEqual(owner.gap.mock_calls, [])
            owner.session.pop_event.assert_not_called()
            owner.fault.abort.assert_not_called()
            self.assertEqual(owner.report["primary_error"], "original failure")
        owner = self.owner()
        owner.fault.status.side_effect = [
            read_receipt(self.ticket, fault.Phase.HELD),
            fault.FaultWireError("ticket drift"),
        ]
        owner.fault.get_limits.return_value = SimpleNamespace(
            peer_present=True,
            peer=PEER,
            boot_nonce=self.ticket.boot_nonce,
            connection_epoch=1,
            bearer_epoch=1,
            database_epoch=1,
        )
        with self.assertRaisesRegex((RuntimeError, ValueError), "ticket drift"):
            owner.cleanup()
        owner.fault.abort.assert_called_once_with(self.ticket)
        self.assertEqual(owner.gap.mock_calls, [])

    def test_cleanup_retained_disconnected_owner_status_only_and_pins_never_pass(
        self,
    ) -> None:
        owner = self.owner()
        owner.fault.status.return_value = self.closed
        owner.cleanup()
        owner.fault.abort.assert_not_called()
        self.assertTrue(owner.report["physical_cleanup"]["hold_inactive"])
        for refs in (
            replace(self.closed.refs, tx_pending=1),
            replace(self.closed.refs, evidence_pending=1),
        ):
            owner = self.owner()
            owner.fault.status.return_value = replace(self.closed, refs=refs)
            clock = Clock()
            with (
                patch.object(continuous.time, "monotonic", side_effect=clock.monotonic),
                patch.object(continuous.time, "sleep", side_effect=clock.sleep),
                self.assertRaisesRegex(RuntimeError, "retirement deadline"),
            ):
                owner.cleanup()
            owner.fault.abort.assert_not_called()
            self.assertEqual(owner.gap.mock_calls, [])

    def test_cleanup_rechecks_resources_after_gap_and_never_reuses_earlier_zero(
        self,
    ) -> None:
        for drains in (True, False):
            with self.subTest(drains=drains):
                owner = self.owner()
                clock = Clock()
                state = {"mutated": False, "samples": 0}
                pinned = replace(
                    self.closed, refs=replace(self.closed.refs, evidence_pending=1)
                )

                def stop(state=state) -> None:
                    state["mutated"] = True

                def status(_, state=state, drains=drains, pinned=pinned):
                    if state["mutated"]:
                        state["samples"] += 1
                        if not drains or state["samples"] == 1:
                            return pinned
                    return self.closed

                owner.stop_advertising_for_cleanup = Mock(side_effect=stop)
                owner.fault.status.side_effect = status
                with (
                    patch.object(
                        continuous.time, "monotonic", side_effect=clock.monotonic
                    ),
                    patch.object(continuous.time, "sleep", side_effect=clock.sleep),
                ):
                    if drains:
                        owner.cleanup()
                        self.assertTrue(
                            owner.report["physical_cleanup"]["hold_inactive"]
                        )
                        self.assertEqual(state["samples"], 2)
                    else:
                        with self.assertRaisesRegex(
                            RuntimeError, "final resource-zero deadline"
                        ):
                            owner.cleanup()
                        self.assertFalse(
                            owner.report["physical_cleanup"]["hold_inactive"]
                        )
                        self.assertTrue(owner.report["physical_cleanup_errors"])
                owner.fault.abort.assert_not_called()

    def test_watchdog_released_barrier_cannot_borrow_recovery_budget(self) -> None:
        control = self.fake.control
        relay = controls.ControlRelay(self.attempt.root, control)
        relay.sent = ["armed", "held"]
        prefix = self.fake.text.split(
            "BLEHUB_NITRO_RUNTIME " + control.marker("observed", "released")
        )[0]
        relay.observe(prefix)
        controls.write_document(
            self.fake.directory / "signal-released.json", asdict(control)
        )
        held = next(
            b for b in self.fake.evidence["boundaries"] if b["boundary"] == "held"
        )
        with patch.object(
            controls.time, "monotonic", return_value=held["monotonic"] + 15.9
        ):
            self.assertEqual(relay.pending(), "released")
        for delay in (16.0, 70.0):
            with (
                self.subTest(delay=delay),
                patch.object(
                    controls.time, "monotonic", return_value=held["monotonic"] + delay
                ),
                self.assertRaisesRegex(TimeoutError, "absolute hold/host budget"),
            ):
                relay.pending()
        self.assertEqual(relay.sent, ["armed", "held"])

    def test_request_deadline_token_scope_and_bundle_order_remain_strict(self) -> None:
        runtime = continuous.ContinuousRuntime(
            self.attempt.root, self.fake.control, Mock(), {}
        )
        with (
            patch.object(continuous.time, "monotonic", return_value=10),
            self.assertRaises(TimeoutError),
        ):
            runtime.request("arm", 10)
        controls.write_document(
            self.fake.directory / "request-arm.json", asdict(self.attempt.control)
        )
        with self.assertRaisesRegex(RuntimeError, "generation"):
            runtime.request("arm", time.monotonic() + 1)
        path = self.attempt.root / "runtime.log"
        path.write_text(
            "BLEHUB_NITRO_RUNTIME rf-lab-suite:flow-started:index=8:mode=rf-cancellation\n"
        )
        with self.assertRaisesRegex(RuntimeError, "scope"):
            runtime.snapshot()
        path.write_text(
            "BLEHUB_NITRO_RUNTIME rf-lab-suite:flow-started:index=9:mode=rf-watchdog\n"
            + self.fake.text
        )
        self.assertEqual(runtime.snapshot(), self.fake.text)
        relay = controls.SuiteControlRelay(self.attempt.root)
        with self.assertRaisesRegex(ValueError, "early"):
            relay.observe(
                "BLEHUB_NITRO_RUNTIME "
                + self.fake.control.marker("listener", "registered")
            )


if __name__ == "__main__":
    unittest.main()
