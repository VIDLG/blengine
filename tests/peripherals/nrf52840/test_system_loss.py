"""Provider-only system loss regressions; real BTP dispatch, synthetic native facts."""

from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from . import fault_control as f
from . import system_loss as api
from .cancellation_control import read_document, write_document
from .support.ccc_wire import CCC_HANDLE, READ_HANDLE
from .support.fault_emulator import PEER
from .support.system_loss_emulator import Harness


class SystemLossTests(unittest.TestCase):
    def make(self, case=api.WRITE_CCC_ENABLE):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        h = Harness(Path(directory.name), case)
        clock = patch.object(api.time, "monotonic", side_effect=h.clock.monotonic)
        sleep = patch.object(api.time, "sleep", side_effect=h.clock.sleep)
        clock.start()
        sleep.start()
        self.addCleanup(clock.stop)
        self.addCleanup(sleep.stop)
        return h

    def armed(self, case=api.WRITE_CCC_ENABLE):
        h = self.make(case)
        self.assertEqual(h.owner.arm(h.witness)["state"], "armed")
        h.pending()
        return h

    def ready(self, case=api.WRITE_CCC_ENABLE):
        h = self.armed(case)
        self.assertEqual(h.owner.observe(h.witness)["state"], "held-pending")
        return h

    def test_arm_without_future_ids_then_bind_actual_nonfixed_ids_once(self):
        for case in (api.WRITE_CCC_ENABLE, api.CCC_DISABLE_WRITE):
            with self.subTest(case=case):
                h = self.make(case)
                control_raw = (h.root / api.CONTROL_NAME).read_bytes()
                self.assertNotIn("head_command_id", h.control)
                self.assertNotIn("sibling_command_id", h.control)
                self.assertNotIn("subscription_id", h.control)
                self.assertEqual(h.owner.arm(h.witness)["state"], "armed")
                deadline = h.owner.document["off_deadline"]
                self.assertFalse((h.root / api.BINDING_NAME).exists())
                subscription = "93017" if case == api.WRITE_CCC_ENABLE else "7"
                h.pending("71039", "88241", subscription)
                report = h.owner.observe(h.witness)
                binding = read_document(h.root / api.BINDING_NAME)
                self.assertEqual(binding["head_command_id"], "71039")
                self.assertEqual(binding["sibling_command_id"], "88241")
                self.assertEqual(binding["subscription_id"], subscription)
                self.assertEqual(
                    binding["witness_sha256"], api.witness_digest(h.witness)
                )
                self.assertEqual(report["binding_sha256"], api.binding_digest(binding))
                binding_raw = (h.root / api.BINDING_NAME).read_bytes()
                h.clock.now += 0.1
                h.owner.observe(h.witness)
                self.assertEqual((h.root / api.BINDING_NAME).read_bytes(), binding_raw)
                self.assertEqual((h.root / api.CONTROL_NAME).read_bytes(), control_raw)
                self.assertEqual(h.owner.document["off_deadline"], deadline)
                h.owner.authorize_off(h.off_request(), h.witness)
                h.disconnect()
                h.owner.poll_disconnect(h.witness)
                self.assertEqual(
                    read_document(h.root / api.RECEIPT_NAME)["binding_sha256"],
                    report["binding_sha256"],
                )

    def test_completed_evidence_is_not_rewritten_by_later_failure_or_invalid_calls(
        self,
    ):
        h = self.ready()
        h.owner.authorize_off(h.off_request(), h.witness)
        h.disconnect()
        completed = h.owner.poll_disconnect(h.witness)
        files = {
            name: (h.root / name).read_bytes()
            for name in (api.PHYSICAL_NAME, api.RECEIPT_NAME, api.BINDING_NAME)
        }
        commands = len(h.wire.commands)
        self.assertEqual(
            h.owner.finish_failure(RuntimeError("late caller error")), completed
        )
        for action in (
            lambda: h.owner.arm(h.witness),
            lambda: h.owner.observe(h.witness),
            lambda: h.owner.authorize_off(h.off_request(), h.witness),
            lambda: h.owner.poll_disconnect(h.witness),
        ):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(h.owner.document, completed)
        self.assertEqual(len(h.wire.commands), commands)
        for name, raw in files.items():
            self.assertEqual((h.root / name).read_bytes(), raw)

    def test_binding_rejects_id_aliases_drift_and_file_or_request_mutation(self):
        for case in (api.WRITE_CCC_ENABLE, api.CCC_DISABLE_WRITE):
            pairs = [("10", "1"), ("10", "10"), ("1", "11")]
            if case == api.CCC_DISABLE_WRITE:
                pairs.extend([("8", "11"), ("10", "8")])
            for head, sibling in pairs:
                with self.subTest(case=case, head=head, sibling=sibling):
                    h = self.make(case)
                    h.owner.arm(h.witness)
                    h.pending(head, sibling)
                    with self.assertRaises(ValueError):
                        h.owner.observe(h.witness)
                    self.assertFalse((h.root / api.BINDING_NAME).exists())
        h = self.make(api.CCC_DISABLE_WRITE)
        h.owner.arm(h.witness)
        h.pending("71039", "88241", "93017")
        with self.assertRaisesRegex(ValueError, "baseline subscription drift"):
            h.owner.observe(h.witness)
        self.assertFalse((h.root / api.BINDING_NAME).exists())
        for fault in ("head", "sibling", "subscription", "lane", "file", "request"):
            with self.subTest(fault=fault):
                h = self.ready()
                binding_raw = (h.root / api.BINDING_NAME).read_bytes()
                request = h.off_request()
                if fault == "head":
                    h.witness["events"][1]["command_id"] = "123456"
                    h.witness["events"][-1]["head_command_id"] = "123456"
                elif fault == "sibling":
                    h.witness["events"][2]["command_id"] = "234567"
                    h.witness["events"][-1]["sibling_command_id"] = "234567"
                elif fault == "subscription":
                    h.witness["events"][2]["subscription_id"] = "345678"
                    h.witness["events"][-1]["subscription_id"] = "345678"
                elif fault == "lane":
                    h.witness["events"][-1]["subscription_id"] = "345678"
                elif fault == "file":
                    (h.root / api.BINDING_NAME).write_bytes(binding_raw + b"\n")
                else:
                    request["binding_sha256"] = "00" * 32
                request["witness_sha256"] = api.witness_digest(h.witness)
                with self.assertRaises(ValueError):
                    h.owner.authorize_off(request, h.witness)
                if fault != "file":
                    self.assertEqual(
                        (h.root / api.BINDING_NAME).read_bytes(), binding_raw
                    )

    def test_failed_binding_publication_cannot_authorize_off_or_claim_bound_ids(self):
        h = self.armed()
        original = api.write_document

        def publish(path, value):
            if path.name == api.BINDING_NAME:
                raise OSError("binding persistence failed")
            original(path, value)

        with (
            patch.object(api, "write_document", side_effect=publish),
            self.assertRaisesRegex(OSError, "binding persistence failed"),
        ):
            h.owner.observe(h.witness)
        self.assertEqual(h.owner.document["state"], "failed")
        self.assertEqual(
            h.owner.document["primary_error"], "binding persistence failed"
        )
        self.assertIsNone(h.owner.document["binding_sha256"])
        self.assertFalse((h.root / api.BINDING_NAME).exists())
        self.assertFalse(h.owner.document["radio_authorized"])

    def test_both_cases_real_sdk_held_disconnect_receipt_not_recovery_verdict(self):
        for case, operation, handle, before, requested in (
            (
                api.WRITE_CCC_ENABLE,
                f.Operation.RESPONSE_WRITE,
                READ_HANDLE,
                b"\x00",
                b"\x10",
            ),
            (
                api.CCC_DISABLE_WRITE,
                f.Operation.CCC_WRITE,
                CCC_HANDLE,
                b"\x01\x00",
                b"\x00\x00",
            ),
        ):
            with self.subTest(case=case):
                h = self.ready(case)
                reads = h.wire.status_reads
                result = h.owner.authorize_off(h.off_request(), h.witness)
                self.assertEqual(result["state"], "off-ready")
                self.assertGreater(h.wire.status_reads, reads)
                request = h.wire.request
                self.assertEqual(
                    (
                        request.operation,
                        request.common.handle,
                        request.before,
                        request.requested,
                    ),
                    (operation, handle, before, requested),
                )
                h.disconnect()
                report = h.owner.poll_disconnect(h.witness)
                self.assertEqual(report["state"], "disconnected-retired")
                self.assertEqual(report["recovery"], "not-evaluated")
                self.assertFalse(report["radio_authorized"])
                retained = read_document(h.root / api.RECEIPT_NAME)
                self.assertEqual(
                    f.status_from_document(retained["status"]).winner,
                    f.Winner.DISCONNECT,
                )
                self.assertEqual(
                    retained["control_sha256"], api.control_digest(h.control)
                )
                self.assertFalse(
                    any(
                        c.service == 255
                        and c.opcode in (f.Opcode.RESOLVE, f.Opcode.ABORT)
                        for c in h.wire.commands
                    )
                )
                self.assertFalse(any(c.service == 1 for c in h.wire.commands))
                with self.assertRaises(ValueError):
                    h.owner.arm(h.witness)
                self.assertEqual(read_document(h.root / api.RECEIPT_NAME), retained)

    def test_early_off_and_begin_only_do_not_authorize(self):
        h = self.make()
        with self.assertRaisesRegex(ValueError, "state"):
            h.owner.authorize_off(h.off_request(), h.witness)
        h = self.armed()
        h.witness["events"][2]["source"] = "bleng-begin-return"
        with self.assertRaisesRegex(ValueError, "dispatch"):
            h.owner.authorize_off(h.off_request(), h.witness)
        self.assertNotEqual(h.owner.document["state"], "off-ready")

    def test_pending_witness_required_and_dispatch_operation_role_ids_exact(self):
        mutations = [
            lambda w: w["events"].pop(),
            lambda w: w["events"][2].update(command_id="10"),
            lambda w: w["events"][2].update(operation="ccc-disable"),
            lambda w: w["events"][2].update(characteristic_id="4"),
            lambda w: w["events"][2].update(accepted=False),
            lambda w: w["events"][2].update(requested_hex="0200"),
            lambda w: w["events"][2].update(subscription_id="99"),
            lambda w: w["events"][2].update(descriptor_id="99"),
            lambda w: w["events"][2].update(connection_id="99"),
            lambda w: w["events"][1].update(before_hex="01"),
            lambda w: w["events"][-1].update(sibling_state="submitted"),
            lambda w: w["events"][-1].update(source="bleng-begin-return"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                h = self.armed()
                mutate(h.witness)
                with self.assertRaises(ValueError):
                    h.owner.authorize_off(h.off_request(), h.witness)
                self.assertFalse(h.owner.document["radio_authorized"])

    def test_stale_request_and_absolute_budget_are_not_renewed(self):
        h = self.ready()
        request = h.off_request()
        request["witness_sha256"] = "00" * 32
        with self.assertRaisesRegex(ValueError, "request"):
            h.owner.authorize_off(request, h.witness)
        h = self.ready()
        deadline = h.owner.document["off_deadline"]
        h.clock.now = deadline - 0.1
        h.owner.observe(h.witness)
        self.assertEqual(h.owner.document["off_deadline"], deadline)
        h.clock.now = deadline
        with self.assertRaisesRegex((ValueError, TimeoutError), "deadline"):
            h.owner.authorize_off(h.off_request(), h.witness)

    def test_fresh_status_not_cached_held_authorizes_off(self):
        h = self.ready()
        h.wire.on_status = h.disconnect
        with self.assertRaises(ValueError):
            h.owner.authorize_off(h.off_request(), h.witness)
        self.assertNotEqual(h.owner.document["state"], "off-ready")

    def test_wrong_status_operation_peer_epochs_bytes_or_effects_rejected(self):
        mutations = [
            lambda h: {"ticket": replace(h.wire.ticket, operation=f.Operation.READ)},
            lambda h: {"ticket": replace(h.wire.ticket, database_epoch=9)},
            lambda h: {"ticket": replace(h.wire.ticket, connection_epoch=9)},
            lambda h: {"ticket": replace(h.wire.ticket, bearer_epoch=9)},
            lambda h: {"ticket": replace(h.wire.ticket, handle=CCC_HANDLE)},
            lambda h: {"peer": bytes.fromhex("01010203040506")},
            lambda h: {"observed": b"\x20"},
            lambda h: {"before": b"\x01"},
            lambda h: {"after": b"\x10"},
            lambda h: {
                "flags": f.StatusFlags.REQUEST_CAPTURED | f.StatusFlags.CALLBACK_INVOKED
            },
            lambda h: {"remaining": f.Remaining(0, 0, 0, 0)},
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                h = self.ready()
                h.wire.changes = mutate(h)
                with self.assertRaises(ValueError):
                    h.owner.authorize_off(h.off_request(), h.witness)
                self.assertNotEqual(h.owner.document["state"], "off-ready")

    def test_wrong_descriptor_and_discovery_route_fail_before_arm(self):
        for field, value in (
            ("ccc_uuid", "00002901-0000-1000-8000-00805f9b34fb"),
            ("service_uuid", "00000000-0000-4000-8000-000000000000"),
            ("read_properties", ["read"]),
            ("ccc_descriptor_id", "5"),
        ):
            with self.subTest(field=field):
                h = self.make()
                h.witness["events"][0]["route"][field] = value
                with self.assertRaises(ValueError):
                    h.owner.arm(h.witness)
                self.assertIsNone(h.wire.ticket)
        h = self.make(api.CCC_DISABLE_WRITE)
        h.handles["updates"] = 52
        with self.assertRaises(ValueError):
            h.owner.arm(h.witness)
        self.assertIsNone(h.wire.ticket)

    def test_baseline_subscription_must_be_real_enabled_before_disable(self):
        for fault in ("getter", "witness"):
            h = self.make(api.CCC_DISABLE_WRITE)
            if fault == "getter":
                h.wire.ccc = b"\x00\x00"
            else:
                h.witness["events"].pop()
            with self.assertRaises(ValueError):
                h.owner.arm(h.witness)
            self.assertIsNone(h.wire.ticket)

    def test_identity_control_receipt_and_cumulative_witness_are_immutable(self):
        for field in (
            "pid",
            "boot_id",
            "owner_id",
            "backend_id",
            "initialize_command_id",
        ):
            with self.subTest(field=field):
                h = self.ready()
                h.witness["identity"][field] = "foreign"
                with self.assertRaises(ValueError):
                    h.owner.authorize_off(h.off_request(), h.witness)
        for fault in ("control", "receipt", "prefix", "rewind", "foreign-session"):
            with self.subTest(fault=fault):
                h = self.ready()
                if fault in ("control", "receipt"):
                    path = (
                        h.root / api.CONTROL_NAME
                        if fault == "control"
                        else h.plan.receipt_path
                    )
                    path.write_bytes(path.read_bytes() + b"\n")
                elif fault == "prefix":
                    h.witness["events"][1]["accepted"] = False
                elif fault == "rewind":
                    h.witness["events"].pop()
                else:
                    h.witness["control_sha256"] = "00" * 32
                with self.assertRaises(ValueError):
                    h.owner.authorize_off(h.off_request(), h.witness)

    def test_disconnect_requires_gap_and_exact_disconnect_outcome_not_expiry(self):
        for fault in ("no-gap", "expiry", "pinned", "wrong-peer"):
            with self.subTest(fault=fault):
                h = self.ready()
                h.owner.authorize_off(h.off_request(), h.witness)
                if fault == "no-gap":
                    h.wire.phase, h.wire.winner = f.Phase.RETIRED, f.Winner.DISCONNECT
                    self.assertEqual(
                        h.owner.poll_disconnect(h.witness)["state"], "off-ready"
                    )
                    self.assertFalse((h.root / api.RECEIPT_NAME).exists())
                    continue
                h.disconnect()
                if fault == "expiry":
                    h.wire.winner = f.Winner.HOLD_EXPIRE
                elif fault == "pinned":
                    h.wire.changes["refs"] = f.RefCounters(0, 0, 0, 0, 0, 1)
                else:
                    h.wire.changes["peer"] = PEER[::-1]
                if fault == "pinned":
                    h.clock.now = h.owner.document["retire_deadline"]
                with self.assertRaises((ValueError, TimeoutError)):
                    h.owner.poll_disconnect(h.witness)
                self.assertFalse((h.root / api.RECEIPT_NAME).exists())

    def test_failure_cleanup_aborts_only_owned_live_hold_and_keeps_primary(self):
        h = self.ready()
        primary = RuntimeError("native pending witness failed")
        h.wire.fault = "cleanup-failure"
        report = h.owner.finish_failure(primary)
        self.assertEqual(report["state"], "failed")
        self.assertEqual(report["primary_error"], str(primary))
        self.assertTrue(report["cleanup_errors"])
        self.assertTrue(primary.__notes__)
        h = self.ready()
        report = h.owner.finish_failure(RuntimeError("original"))
        self.assertEqual(report["primary_error"], "original")
        self.assertEqual(report["cleanup_errors"], [])
        self.assertTrue(any(c.opcode == f.Opcode.ABORT for c in h.wire.commands))
        self.assertFalse((h.root / api.RECEIPT_NAME).exists())

    def test_quarantined_limits_cannot_authorize_even_with_held_receipt(self):
        h = self.ready()
        h.wire.limit_flags = int(f.EngineFlags.BUSY | f.EngineFlags.QUARANTINED)
        with self.assertRaisesRegex(ValueError, "quarantined"):
            h.owner.authorize_off(h.off_request(), h.witness)
        self.assertNotEqual(h.owner.document["state"], "off-ready")
        self.assertFalse(any(c.opcode == f.Opcode.ABORT for c in h.wire.commands))

    def test_final_evidence_publication_failure_preserves_saved_receipt_and_primary(
        self,
    ):
        h = self.ready()
        h.owner.authorize_off(h.off_request(), h.witness)
        h.disconnect()
        original = api.write_document

        def publish(path, value):
            if (
                path.name == api.PHYSICAL_NAME
                and value["state"] == "disconnected-retired"
            ):
                raise OSError("final evidence failed")
            original(path, value)

        with (
            patch.object(api, "write_document", side_effect=publish),
            self.assertRaisesRegex(OSError, "final evidence failed"),
        ):
            h.owner.poll_disconnect(h.witness)
        self.assertEqual(h.owner.document["state"], "failed")
        self.assertEqual(h.owner.document["primary_error"], "final evidence failed")
        self.assertTrue((h.root / api.RECEIPT_NAME).exists())

    def test_pending_must_be_held_and_current_epoch_not_just_accepted(self):
        for fault in ("armed", "epoch", "peer"):
            with self.subTest(fault=fault):
                h = self.armed()
                if fault == "armed":
                    h.wire.phase = f.Phase.ARMED
                    h.wire.captured = False
                elif fault == "epoch":
                    h.wire.epoch += 1
                else:
                    h.wire.connected = False
                with self.assertRaises(ValueError):
                    h.owner.observe(h.witness)
                self.assertNotEqual(h.owner.document["state"], "off-ready")

    def test_sibling_ccc_descriptor_is_not_a_target_descriptor(self):
        h = self.make(api.CCC_DISABLE_WRITE)
        h.wire.attrs = [a for a in h.wire.attrs if a.handle != CCC_HANDLE]
        with self.assertRaisesRegex(ValueError, "unique target CCC"):
            h.owner.arm(h.witness)
        self.assertIsNone(h.wire.ticket)

    def test_cleanup_interruption_does_not_replace_primary(self):
        h = self.ready()
        h.fixture._transport.require.side_effect = KeyboardInterrupt(
            "cleanup interrupted"
        )
        report = h.owner.finish_failure(RuntimeError("primary native failure"))
        self.assertEqual(report["primary_error"], "primary native failure")
        self.assertIn("cleanup interrupted", report["cleanup_errors"][0])

    def test_lease_loss_blocks_btp_and_cleanup_without_replacing_primary(self):
        h = self.ready()
        commands = len(h.wire.commands)
        h.fixture._transport.require.side_effect = RuntimeError("physical lease lost")
        with self.assertRaisesRegex(RuntimeError, "physical lease lost"):
            h.owner.authorize_off(h.off_request(), h.witness)
        self.assertEqual(len(h.wire.commands), commands)
        self.assertIn("physical lease lost", h.owner.document["primary_error"])
        self.assertTrue(h.owner.document["cleanup_errors"])

    def test_receipt_publication_failure_does_not_advance_or_abort_detached_owner(self):
        h = self.ready()
        h.owner.authorize_off(h.off_request(), h.witness)
        h.disconnect()
        original = api.write_document

        def publish(path, value):
            if path.name == api.RECEIPT_NAME:
                raise OSError("receipt persistence failed")
            original(path, value)

        with (
            patch.object(api, "write_document", side_effect=publish),
            self.assertRaisesRegex(OSError, "receipt persistence failed"),
        ):
            h.owner.poll_disconnect(h.witness)
        self.assertEqual(h.owner.document["state"], "failed")
        self.assertFalse(any(c.opcode == f.Opcode.ABORT for c in h.wire.commands))
        self.assertFalse((h.root / api.RECEIPT_NAME).exists())

    def test_foreign_cleanup_owner_never_aborted(self):
        h = self.ready()
        h.wire.epoch += 1
        report = h.owner.finish_failure(RuntimeError("original"))
        self.assertTrue(report["cleanup_errors"])
        self.assertFalse(any(c.opcode == f.Opcode.ABORT for c in h.wire.commands))

    def test_control_invalid_identity_ids_and_cases_cannot_open_an_owner(self):
        h = self.make()
        for field, value in (
            ("case", "read-loss"),
            ("sibling_command_id", "10"),
            ("session_id", "00000000-0000-4000-8000-000000000000"),
        ):
            control = copy.deepcopy(h.control)
            control[field] = value
            write_document(h.root / api.CONTROL_NAME, control)
            with self.assertRaises(ValueError):
                api.SystemLossCoordinator(h.fixture, h.root, h.handles)


if __name__ == "__main__":
    unittest.main()
