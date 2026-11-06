"""Synthetic composition tests only; never open serial, reset, flash or run RF."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840.support.direct_fixture_resources import (
    resources as fixture_resources,
)
from tests.peripherals.nrf52840.support.direct_fixture_resources import (
    sample_plan,
    startup_mocks,
)
from tests.peripherals.nrf52840.support.full_suite_emulator import FullHarness
from tests.peripherals.nrf52840.support.recovery_emulator import HANDLES
from tools.react_native import recovery_ten_ledger as ledger

from . import cancellation_control as controls
from . import direct_fixture, provider
from . import full_suite as api
from . import recovery_ten as recovery
from .btp_protocol import BtpFrame
from .fault_control import Opcode, Phase, ticket_document

ARM, ABORT = Opcode.ARM_READ, Opcode.ABORT


class FullSuiteTests(unittest.TestCase):
    def make(self, platform: str = "ios") -> FullHarness:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return FullHarness(Path(temporary.name), platform)

    def test_one_session_real_dispatch_canonical_handoff_and_ten_recovery_cycles(
        self,
    ) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform):
                h = self.make(platform)
                session, handles, peers = (
                    h.fixture._session,
                    dict(HANDLES),
                    h.fixture._connected_peers,
                )
                h.run()
                report = controls.read_document(h.root / api.PHYSICAL_NAME)
                self.assertEqual(
                    (report["status"], report["recovery_status"]), ("passed", "passed")
                )
                ready = controls.read_document(h.root / api.HANDOFF_NAME)
                self.assertEqual(ready["status"], "ready")
                self.assertEqual(
                    (h.root / api.HANDOFF_NAME).read_bytes(), h.ready_bytes
                )
                self.assertEqual(
                    ready["fault_ticket"], ticket_document(h.status.ticket)
                )
                self.assertEqual(ready["run_id"], h.h.plan.session_id)
                self.assertTrue(ready["canonical_values_restored"])
                self.assertEqual(len(ready["canonical_values"]), 5)
                self.assertEqual(
                    h.values,
                    {
                        HANDLES[c.role]: c.initial_value
                        for c in h.fixture._profile.characteristics
                    },
                )
                self.assertIs(h.fixture._session, session)
                self.assertIs(h.full.recovery.fixture._session, session)
                self.assertEqual(h.full.recovery.retained_ticket, h.full.owned_hold)
                self.assertIs(h.fixture._connected_peers, peers)
                self.assertEqual(HANDLES, handles)
                self.assertEqual(peers, set())
                h.base_coordinator.assert_called_once_with(
                    h.fixture, h.root, HANDLES, parent_guard=h.full.base_guard
                )
                physical = controls.read_document(h.h.root / recovery.PHYSICAL_NAME)
                self.assertEqual(
                    physical["retained_ticket"], ticket_document(h.full.owned_hold)
                )
                self.assertEqual(ledger._retained_samples(physical), h.full.owned_hold)
                self.assertEqual(
                    physical["final_groups"],
                    {
                        "cycles": 10,
                        "connections": 20,
                        "disconnections": 20,
                        "closed_snapshots": 10,
                    },
                )
                self.assertEqual(len(h.h.wire.remote_reads), 10)
                self.assertFalse(
                    any(
                        f.service == 255
                        and f.opcode
                        in (
                            Opcode.ARM_READ,
                            Opcode.ARM_WRITE,
                            Opcode.ARM_CCC,
                            Opcode.RESOLVE,
                            Opcode.ABORT,
                        )
                        for f in h.h.wire.commands
                    )
                )
                self.assertEqual(
                    controls.read_document(h.watchdog.path), h.watchdog.evidence
                )
                self.assertEqual(
                    controls.read_document(h.attempt.path), h.attempt.evidence
                )

    def test_foreign_generation_peer_readback_and_first_phase_failures_block_recovery(
        self,
    ) -> None:
        for fault in (
            "generation",
            "foreign-hold",
            "known-peer",
            "new-peer",
            "foreign-disconnect",
            "malformed-gap",
            "readback",
            "base",
            "physical",
            "close",
            "missing-tracker",
        ):
            with self.subTest(fault=fault):
                h = self.make()
                if fault == "generation":
                    h.status = replace(
                        h.status, ticket=replace(h.status.ticket, sequence=9)
                    )
                elif fault == "foreign-hold":
                    h.status = replace(h.status, phase=Phase.HELD)
                elif fault == "known-peer":
                    h.fixture._connected_peers.add(("112233445566", 1))
                elif fault in ("new-peer", "foreign-disconnect", "malformed-gap"):
                    h.h.wire.gap(
                        fault != "foreign-disconnect",
                        b"\x01" + bytes(6),
                        "malformed" if fault == "malformed-gap" else "",
                    )
                elif fault == "readback":
                    h.fixture._gatt.get_attribute_value.side_effect = None
                    h.fixture._gatt.get_attribute_value.return_value = SimpleNamespace(
                        att_response=0, value=b"wrong"
                    )
                elif fault == "base":
                    h.base_coordinator.side_effect = RuntimeError(
                        "original first-phase failure"
                    )
                elif fault == "physical":
                    value = dict(h.watchdog.evidence, status="failed")
                    controls.write_document(h.watchdog.path, value)
                elif fault == "close":
                    h.base_lines = [
                        line.replace("phase=close", "phase=wrong")
                        for line in h.base_lines
                    ]
                    h.write_root()
                elif fault == "missing-tracker":
                    del h.fixture._connected_peers
                with self.assertRaises((RuntimeError, ValueError, AttributeError)):
                    h.run()
                self.assertIsNone(h.full.recovery)
                self.assertFalse((h.root / api.HANDOFF_NAME).exists())
                self.assertEqual(
                    controls.read_document(h.h.root / recovery.PHYSICAL_NAME)["status"],
                    "blocked",
                )
                self.assertEqual(
                    controls.read_document(h.root / api.PHYSICAL_NAME)[
                        "recovery_status"
                    ],
                    "blocked",
                )
                self.assertFalse(
                    any(
                        f.service == 255 and f.opcode in (ARM, ABORT)
                        for f in h.h.wire.commands
                    )
                )
                if fault not in ("readback",):
                    self.assertFalse(h.values)

    def test_begin_requires_fresh_ordered_native_url_and_js_ack(self) -> None:
        for fault in (
            "wrong-url",
            "foreign-run",
            "bad-pid",
            "missing-native",
            "reversed",
            "duplicate",
            "early",
        ):
            with self.subTest(fault=fault):
                h = self.make()
                h.ack_fault = fault
                if fault == "early":
                    h.base_lines.append(
                        api.PREFIX
                        + "rf-lab-full:native:signal:"
                        + json.dumps({**h.identity, "url": h.full.url})
                    )
                    h.write_root()
                with self.assertRaises((RuntimeError, ValueError)):
                    h.run()
                self.assertIsNone(h.full.recovery)
                self.assertEqual(
                    controls.read_document(h.h.root / recovery.PHYSICAL_NAME)["status"],
                    "blocked",
                )
                if fault != "early":
                    self.assertEqual(
                        controls.read_document(h.root / api.HANDOFF_NAME)["status"],
                        "ready",
                    )

    def test_abort_propagates_during_base_and_recovery_and_cleanup_preserves_primary(
        self,
    ) -> None:
        for stage in ("base", "recovery", "cleanup"):
            with self.subTest(stage=stage):
                h = self.make()
                if stage == "base":

                    def base(
                        *_: object, harness: FullHarness = h, **kw: object
                    ) -> None:
                        (harness.root / "abort").touch()
                        kw["parent_guard"]()

                    h.base_coordinator.side_effect = base
                elif stage == "recovery":
                    original = h.h.begin

                    def begin(original=original, harness: FullHarness = h) -> None:
                        original()
                        (harness.root / "abort").touch()

                    h.h.begin = begin
                else:
                    h.base_coordinator.side_effect = RuntimeError("primary first phase")
                    h.fixture._gap.stop_advertising = Mock(
                        side_effect=RuntimeError("cleanup failure")
                    )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "primary first phase" if stage == "cleanup" else "aborted",
                ):
                    h.run()
                if stage != "cleanup":
                    self.assertTrue((h.h.root / "abort").exists())
                report = controls.read_document(h.root / api.PHYSICAL_NAME)
                if stage == "cleanup":
                    self.assertEqual(report["primary_error"], "primary first phase")
                    self.assertEqual(report["cleanup_errors"], ["cleanup failure"])
                if stage == "recovery":
                    self.assertEqual(
                        controls.read_document(h.h.root / recovery.PHYSICAL_NAME)[
                            "status"
                        ],
                        "failed",
                    )
                    ready = (h.root / api.HANDOFF_NAME).read_bytes()
                    self.assertIn(b'"status": "ready"', ready)

    def test_missing_begin_ack_times_out_and_late_generation_change_blocks_start(
        self,
    ) -> None:
        for fault in (
            "missing",
            "late-generation",
            "late-peer",
            "rewind",
            "native-bootstrap",
        ):
            with self.subTest(fault=fault):
                h = self.make()
                h.ack_fault = "missing" if fault == "missing" else ""
                publish = h.publish_handoff

                def publication(
                    path: Path,
                    document: dict[str, object],
                    harness: FullHarness = h,
                    fault: str = fault,
                    publish=publish,
                ) -> None:
                    publish(path, document)
                    if path.name != api.HANDOFF_NAME:
                        return
                    if fault == "late-generation":
                        harness.status = replace(
                            harness.status,
                            ticket=replace(harness.status.ticket, sequence=9),
                        )
                    elif fault == "late-peer":
                        harness.h.wire.gap(True, b"\x01" + bytes(6))
                    elif fault == "rewind":
                        (harness.root / "runtime.log").write_text("", encoding="utf-8")
                    elif fault == "native-bootstrap":
                        identity = dict(harness.identity, pid=43)
                        harness.base_lines.insert(
                            0,
                            api.PREFIX
                            + "rf-lab-full:native:bootstrap:"
                            + json.dumps(identity),
                        )
                        harness.write_root()

                h.publish_handoff = publication
                with (
                    patch.object(api, "PHASE_SECONDS", 0.25),
                    self.assertRaises((RuntimeError, ValueError, TimeoutError)),
                ):
                    h.run()
                self.assertIsNone(h.full.recovery)
                self.assertEqual(
                    controls.read_document(h.root / api.HANDOFF_NAME)["status"], "ready"
                )
                self.assertEqual(
                    controls.read_document(h.h.root / recovery.PHYSICAL_NAME)["status"],
                    "blocked",
                )

    def test_exact_shared_watchdog_tracker_and_foreign_disconnect_never_clear_peers(
        self,
    ) -> None:
        h = self.make()
        session = Mock()
        peer = b"\x01" + bytes.fromhex("112233445566")
        event = BtpFrame(1, recovery.GAP_DEVICE_CONNECTED, 0, peer + bytes(6))
        experiment = api.watchdog.ContinuousExperiment(
            session, h.fixture._profile, Mock(), {}
        )
        experiment.strict_peer_events = True
        experiment.fixture_peers = h.fixture._connected_peers
        session.pop_event.side_effect = [event, None]
        experiment.events()
        expected = (peer[1:7][::-1].hex(), peer[0])
        self.assertEqual(experiment.peers, {peer})
        self.assertEqual(h.fixture._connected_peers, {expected})
        foreign = b"\x01" + bytes.fromhex("665544332211")
        session.pop_event.side_effect = [
            BtpFrame(1, recovery.GAP_DEVICE_DISCONNECTED, 0, foreign),
            None,
        ]
        with self.assertRaisesRegex(RuntimeError, "foreign"):
            experiment.events()
        self.assertEqual(h.fixture._connected_peers, {expected})
        session.pop_event.side_effect = [
            BtpFrame(1, recovery.GAP_DEVICE_DISCONNECTED, 0, peer),
            None,
        ]
        experiment.events()
        self.assertFalse(h.fixture._connected_peers)
        self.assertFalse(experiment.peers)
        h.fixture._connected_peers.add(expected)
        session.pop_event.side_effect = [event, None]
        with self.assertRaisesRegex(RuntimeError, "tracker drift"):
            experiment.events()
        self.assertEqual(h.fixture._connected_peers, {expected})
        # The ordinary fixture tracker must not equate any disconnect with no peers.
        fixture = direct_fixture.DirectFixture.__new__(direct_fixture.DirectFixture)
        fixture._connected_peers = {expected, ("ffffffffffff", 1)}
        fixture._observe_gap_event(
            BtpFrame(1, recovery.GAP_DEVICE_DISCONNECTED, 0, peer)
        )
        self.assertEqual(fixture._connected_peers, {("ffffffffffff", 1)})

    def test_open_or_initial_start_failure_still_publishes_blocked_without_masking_primary(
        self,
    ) -> None:
        for stage in ("open", "start"):
            with self.subTest(stage=stage):
                h = self.make()
                fixture = Mock()
                fixture.start.side_effect = TimeoutError("initial start failed")
                fixture.stop.side_effect = RuntimeError("secondary cleanup")
                with (
                    # Isolated historical ownership mechanics, not live gate authorization.
                    patch.object(direct_fixture, "require_v2_coordination"),
                    patch.object(api, "validate_control"),
                    patch.object(
                        direct_fixture,
                        "DirectFixture",
                        side_effect=OSError("serial open failed")
                        if stage == "open"
                        else None,
                        return_value=fixture,
                    ),
                    self.assertRaisesRegex(
                        (OSError, TimeoutError),
                        "serial open failed"
                        if stage == "open"
                        else "initial start failed",
                    ) as caught,
                ):
                    direct_fixture.serve(
                        h.h.plan.port,
                        h.h.plan.profile_path,
                        h.root,
                        api.MODE,
                        fixture_session=h.h.plan,
                    )
                self.assertEqual(
                    controls.read_document(h.root / api.PHYSICAL_NAME)[
                        "recovery_status"
                    ],
                    "blocked",
                )
                self.assertEqual(
                    controls.read_document(h.h.root / recovery.PHYSICAL_NAME)["status"],
                    "blocked",
                )
                if stage == "start":
                    self.assertIn(
                        "secondary cleanup", " ".join(caught.exception.__notes__)
                    )
                else:
                    fixture.start.assert_not_called()
                self.assertFalse((h.root / api.HANDOFF_NAME).exists())

    def test_preflight_cli_and_immutable_evidence_refuse_stale_attempt_before_serial(
        self,
    ) -> None:
        h = self.make()
        for c in h.full.controls:
            shutil.rmtree(h.root / c.directory)
        api.validate_control(h.root, h.h.plan)
        for command in ("direct-start", "direct-serve"):
            with patch(
                "sys.argv", ["provider", command, "--coordination-mode", api.MODE]
            ):
                self.assertEqual(provider.parse_args().coordination_mode, api.MODE)
        for fault in ("missing", "cycles", "session", "stale", "abort"):
            path = h.h.root / recovery.CONTROL_NAME
            original = path.read_bytes()
            with self.subTest(fault=fault):
                if fault == "missing":
                    path.unlink()
                elif fault in ("cycles", "session"):
                    value = controls.read_document(path)
                    value["cycles" if fault == "cycles" else "run_id"] = 9
                    controls.write_document(path, value)
                elif fault == "stale":
                    controls.write_document(
                        h.root / api.HANDOFF_NAME, {"status": "old"}
                    )
                else:
                    (h.root / "abort").touch()
                with (
                    patch.object(provider, "_process_identity") as process,
                    patch.object(direct_fixture, "DirectFixture") as fixture,
                ):
                    with self.assertRaises((ValueError, RuntimeError)):
                        provider.direct_start(
                            str(h.root), api.MODE, h.h.plan.request_path
                        )
                    with self.assertRaises((ValueError, RuntimeError)):
                        direct_fixture.serve(
                            h.h.plan.port,
                            h.h.plan.profile_path,
                            h.root,
                            api.MODE,
                            fixture_session=h.h.plan,
                        )
                    process.assert_not_called()
                    fixture.assert_not_called()
                path.write_bytes(original)
                (h.root / api.HANDOFF_NAME).unlink(missing_ok=True)
                (h.root / "abort").unlink(missing_ok=True)
        # Publishing new full/blocked receipts must not replace prior physical evidence.
        old = {"status": "old-physical"}
        controls.write_document(h.h.root / recovery.PHYSICAL_NAME, old)
        h.base_coordinator.side_effect = RuntimeError("primary")
        with self.assertRaisesRegex(RuntimeError, "primary"):
            h.run()
        self.assertEqual(controls.read_document(h.h.root / recovery.PHYSICAL_NAME), old)


class FullServeTests(unittest.TestCase):
    def test_full_mode_one_start_one_lease_one_btp_no_reset_even_on_error(self) -> None:
        for fail in (False, True):
            events: list[str] = []
            plan = sample_plan()
            profile = plan.profile_path

            def finish(
                fixture: direct_fixture.DirectFixture,
                root: Path,
                handles: dict[str, int],
                fail: bool = fail,
            ) -> None:
                self.assertIs(fixture._lease, resources.owner)
                self.assertIs(fixture._session, resources.session)
                self.assertEqual(handles, HANDLES)
                self.assertTrue(fixture._preserve_gatt)
                self.assertTrue(fixture._strict_recovery_cleanup)
                fixture._stop_requested = True
                if fail:
                    fixture._transport.require = Mock(
                        side_effect=RuntimeError("outer cleanup")
                    )
                    raise RuntimeError("full primary")

            with (
                self.subTest(fail=fail),
                fixture_resources(events) as resources,
                patch.object(direct_fixture, "require_v2_coordination"),
                patch.object(api, "validate_control"),
                patch.object(api, "run", side_effect=finish),
                startup_mocks(resources, HANDLES) as startup,
                patch.object(
                    direct_fixture.DirectFixture, "_disconnect_connected_peers"
                ) as disconnect,
            ):
                if fail:
                    with self.assertRaisesRegex(RuntimeError, "full primary") as caught:
                        direct_fixture.serve(
                            "test-port",
                            profile,
                            Path("coordination"),
                            api.MODE,
                            fixture_session=plan,
                        )
                    self.assertIn("outer cleanup", " ".join(caught.exception.__notes__))
                else:
                    direct_fixture.serve(
                        "test-port",
                        profile,
                        Path("coordination"),
                        api.MODE,
                        fixture_session=plan,
                    )
                startup.doctor.return_value.run.assert_called_once()
                resources.open_serial.assert_called_once()
                self.assertEqual(events.count("session"), 1)
                self.assertEqual(events.count("acquire"), 1)
                self.assertEqual(events.count("release"), 1)
                resources.gatt.reset_server.assert_not_called()
                disconnect.assert_not_called()
