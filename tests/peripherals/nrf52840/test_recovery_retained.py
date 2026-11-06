"""Read-only retained-witness regressions over synthetic BTP, never hardware."""

from __future__ import annotations

import copy
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tools.react_native import recovery_ten_ledger as ledger

from . import fault_control as fault
from . import recovery_ten as api
from .gap_commands import GAP_DISCONNECT
from .support.recovery_emulator import HANDLES, Harness


class RecoveryRetainedTests(unittest.TestCase):
    def make(self, platform="ios"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Harness(Path(temporary.name), platform)

    @contextmanager
    def scenario(self, h, *, parent_guard=lambda: None):
        with (
            patch.object(api.time, "monotonic", h.clock.monotonic),
            patch.object(api.time, "sleep", h.clock.sleep),
            patch.object(api, "write_document", h.progress),
        ):
            yield api.RecoveryTen(
                h.fixture,
                h.root,
                HANDLES,
                retained_ticket=h.retained_ticket,
                parent_guard=parent_guard,
            )

    def readonly(self, commands):
        self.assertFalse(
            any(
                c.service == 255
                and c.opcode
                in (
                    fault.Opcode.ARM_READ,
                    fault.Opcode.ARM_WRITE,
                    fault.Opcode.ARM_CCC,
                    fault.Opcode.ABORT,
                    fault.Opcode.RESOLVE,
                )
                for c in commands
            )
        )

    def test_normal_gap_tx_and_previous_limits_reply_drain_only_under_retired_witness(
        self,
    ):
        for source in ("gap", "previous-limits", "racing-status-reply"):
            with self.subTest(source=source):
                h = self.make()
                h.retain_watchdog()
                with self.scenario(h) as runner:
                    if source == "gap":
                        runner.allow_connection = True
                        h.wire.evidence_on_gap = 2
                        h.wire.gap(True, h.old_peer)
                    elif source == "previous-limits":
                        runner.fault.get_limits()
                        # The host has received the reply; the UART worker has not
                        # yet decremented the ordinary reply's pending TX owner.
                        h.wire.pending_evidence = 1
                    else:
                        h.wire.busy_limits = 1
                    offset = len(h.wire.commands)
                    deadline = h.clock.now + 0.5
                    runner.sample(deadline)
                    samples = runner.evidence["fault_samples"]
                    self.assertGreaterEqual(len(samples), 2)
                    self.assertTrue(all(s["deadline"] == deadline for s in samples))
                    self.assertTrue(
                        all(
                            c.service == 255
                            and c.opcode
                            in (fault.Opcode.STATUS, fault.Opcode.GET_LIMITS)
                            for c in h.wire.commands[offset:]
                        )
                    )
                    if source != "racing-status-reply":
                        first = fault.decode_status(
                            bytes.fromhex(samples[0]["status_wire"])
                        )
                        self.assertGreater(first.refs.evidence_pending, 0)
                    else:
                        self.assertEqual(samples[0]["limits"]["engine_flags"], 4)
                    ledger._hold(runner.evidence["hold_status"], h.retained_ticket)
                    self.assertFalse(
                        any(
                            b.get("hold_inactive")
                            for b in runner.evidence["boundaries"]
                        )
                    )
                    self.readonly(h.wire.commands)

    def test_permanent_evidence_uses_original_absolute_deadline_without_checkpoint(
        self,
    ):
        h = self.make()
        h.retain_watchdog()
        send = h.wire.send

        def pending(frame):
            if frame.service == 255 and frame.opcode == fault.Opcode.STATUS:
                h.wire.pending_evidence = 1
            send(frame)

        h.wire.send = pending
        with self.scenario(h) as runner:
            deadline = h.clock.now + 0.2
            runner.whole_deadline = deadline
            with self.assertRaises(TimeoutError):
                runner.sample(deadline + 1)
            self.assertLessEqual(h.clock.now, deadline + 0.001)
            samples = runner.evidence["fault_samples"]
            self.assertGreater(len(samples), 1)
            self.assertTrue(all(s["deadline"] == deadline for s in samples))
            self.assertTrue(
                all(
                    fault.decode_status(
                        bytes.fromhex(s["status_wire"])
                    ).refs.evidence_pending
                    == 1
                    for s in samples
                )
            )
            self.assertFalse(runner.evidence["boundaries"])
            self.assertEqual(
                runner.evidence["hold_status"], {"state": None, "identity": None}
            )
            self.readonly(h.wire.commands)

    def test_readonly_diagnostics_are_bounded_during_longer_legitimate_drain(self):
        h = self.make()
        h.retain_watchdog()
        send = h.wire.send
        count = 0

        def delayed(frame):
            nonlocal count
            if frame.service == 255 and frame.opcode == fault.Opcode.STATUS:
                count += 1
                h.wire.pending_evidence = int(count <= api.FAULT_SAMPLES_MAX + 2)
            send(frame)

        h.wire.send = delayed
        with self.scenario(h) as runner:
            runner.sample(h.clock.now + 10)
            self.assertEqual(
                len(runner.evidence["fault_samples"]), api.FAULT_SAMPLES_MAX
            )
            self.assertGreater(count, api.FAULT_SAMPLES_MAX)
            ledger._hold(runner.evidence["hold_status"], h.retained_ticket)
            self.assertFalse(runner.evidence["boundaries"])
            self.readonly(h.wire.commands)

    def test_standalone_busy_is_not_retried_or_given_a_witness(self):
        h = self.make()
        h.wire.limits_flags = 4
        with self.scenario(h) as runner:
            with self.assertRaisesRegex(ValueError, "foreign resources"):
                runner.sample(h.clock.now + 1)
            self.assertEqual(
                [c.opcode for c in h.wire.commands], [fault.Opcode.GET_LIMITS]
            )
            self.assertNotIn("fault_samples", runner.evidence)
            self.assertNotIn("retained_ticket", runner.evidence)

    def test_constructor_binds_exact_session_read_and_target_without_commands(self):
        for changed in (
            {"owner_nonce": bytes(range(16))},
            {"operation": fault.Operation.RESPONSE_WRITE},
            {"handle": 99},
        ):
            with self.subTest(changed=changed):
                h = self.make()
                h.retain_watchdog()
                h.retained_ticket = replace(h.retained_ticket, **changed)
                with (
                    self.assertRaisesRegex(ValueError, "owner/READ/target"),
                    self.scenario(h),
                ):
                    pass
                self.assertFalse(h.wire.commands)

    def test_invalid_retained_status_or_limits_fail_immediately_and_keep_actual_samples(
        self,
    ):
        changes = [
            ("replaced", lambda s: replace(s, ticket=replace(s.ticket, sequence=9))),
            ("active", lambda s: replace(s, phase=fault.Phase.HELD)),
            ("retiring", lambda s: replace(s, phase=fault.Phase.RETIRING)),
            ("quarantined", lambda s: replace(s, phase=fault.Phase.QUARANTINED)),
            (
                "no-rearm",
                lambda s: replace(s, flags=s.flags & ~fault.StatusFlags.REARMABLE),
            ),
            ("winner", lambda s: replace(s, winner=fault.Winner.COMMIT)),
            ("outcome", lambda s: replace(s, outcome=fault.Outcome.NORMAL_NO_COMMIT)),
            (
                "infra",
                lambda s: replace(
                    s, infrastructure_error=fault.InfrastructureError.EVIDENCE_OVERFLOW
                ),
            ),
            (
                "infra-flag",
                lambda s: replace(
                    s, flags=s.flags | fault.StatusFlags.INFRASTRUCTURE_FAILED
                ),
            ),
        ]
        for name in (
            "conn_refs",
            "bearer_pins",
            "response_buffers",
            "resolver_work",
            "tx_pending",
        ):
            changes.append(
                (
                    name,
                    lambda s, name=name: replace(s, refs=replace(s.refs, **{name: 1})),
                )
            )
        changes.extend(
            (name, lambda s: s)
            for name in ("limits-quarantine", "limits-boot", "foreign-reply")
        )
        for name, change in changes:
            with self.subTest(name=name):
                h = self.make()
                h.retain_watchdog()
                h.wire.retained_status = change(h.wire.retained_status)
                if name == "limits-quarantine":
                    h.wire.limits_flags = 5
                elif name == "limits-boot":
                    h.wire.limits_boot = bytes(range(16))
                elif name == "foreign-reply":
                    send = h.wire.send

                    def foreign(frame, send=send, h=h):
                        send(frame)
                        if frame.service == 255 and frame.opcode == fault.Opcode.STATUS:
                            reply = h.wire.frames.pop()
                            status = replace(
                                h.wire.retained_status,
                                ticket=replace(h.retained_ticket, sequence=9),
                            )
                            h.wire.frames.append(
                                replace(reply, payload=fault.encode_status(status))
                            )

                    h.wire.send = foreign
                with self.scenario(h) as runner:
                    with self.assertRaises((RuntimeError, ValueError)):
                        runner.sample(h.clock.now + 1)
                    samples = runner.evidence["fault_samples"]
                    self.assertEqual(len(samples), 1)
                    sample = samples[0]
                    self.assertIsNotNone(sample["error"])
                    self.assertIsNotNone(sample["status_wire"])
                    self.assertFalse(runner.evidence["boundaries"])
                    if name.startswith("limits-"):
                        self.assertIsNotNone(sample["limits"])
                        self.assertEqual(len(h.wire.commands), 2)
                        if name == "limits-quarantine":
                            self.assertEqual(sample["limits"]["engine_flags"], 5)
                        else:
                            self.assertEqual(
                                sample["limits"]["boot_nonce"], h.wire.limits_boot.hex()
                            )
                    else:
                        self.assertIsNone(sample["limits"])
                        self.assertEqual(len(h.wire.commands), 1)
                        if name == "replaced":
                            self.assertEqual(sample["status_wire"], "0203")
                    self.readonly(h.wire.commands)

    def test_witness_survives_ten_real_dispatch_cycles_without_weakening_zero_checkpoints(
        self,
    ):
        for platform in ("ios", "android"):
            with self.subTest(platform=platform):
                h = self.make(platform)
                ticket = h.retain_watchdog()
                h.wire.evidence_on_gap = 2
                document = h.run()
                self.assertEqual(document["status"], "passed")
                self.assertEqual(ledger._retained_samples(document), ticket)
                for row in document["cycles"]:
                    ledger._hold(row["initial_hold_status"], ticket)
                    ledger._hold(row["final_hold_status"], ticket)
                self.readonly(h.wire.commands)
                for name in (
                    "conn_refs",
                    "bearer_pins",
                    "response_buffers",
                    "resolver_work",
                    "tx_pending",
                    "evidence_pending",
                ):
                    with self.subTest(counter=name):
                        changed = copy.deepcopy(document["hold_status"])
                        status = fault.status_from_document(changed["retained_status"])
                        changed["retained_status"] = fault.status_document(
                            replace(status, refs=replace(status.refs, **{name: 1}))
                        )
                        with self.assertRaises(RuntimeError):
                            ledger._hold(changed, ticket)

    def test_checker_rejects_changed_witness_samples_and_pending_final_checkpoint(self):
        h = self.make()
        h.retain_watchdog()
        original = h.run()
        for change in (
            "ticket-owner",
            "limits-boot",
            "quarantine",
            "pending-final",
            "busy-final",
            "sample-error",
            "sample-shape",
            "deadline",
        ):
            with self.subTest(change=change):
                document = copy.deepcopy(original)
                sample = document["fault_samples"][-1]
                if change == "ticket-owner":
                    document["retained_ticket"] = fault.ticket_document(
                        replace(h.retained_ticket, owner_nonce=bytes(range(16)))
                    )
                elif change == "limits-boot":
                    sample["limits"]["boot_nonce"] = bytes(range(16)).hex()
                elif change == "quarantine":
                    sample["limits"]["engine_flags"] = 5
                elif change == "busy-final":
                    sample["limits"]["engine_flags"] = 4
                elif change == "pending-final":
                    status = fault.decode_status(bytes.fromhex(sample["status_wire"]))
                    sample["status_wire"] = fault.encode_status(
                        replace(status, refs=replace(status.refs, evidence_pending=1))
                    ).hex()
                elif change == "sample-error":
                    sample["error"] = "ignored failure"
                elif change == "sample-shape":
                    sample["ignored"] = True
                else:
                    sample["deadline"] = sample["monotonic"]
                with self.assertRaises(RuntimeError):
                    ledger._retained_samples(document)

    def test_cleanup_consumes_buffered_owned_gap_before_deciding_disconnect(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                h = self.make()
                h.retain_watchdog()
                fired = False

                def parent(h=h, invalid=invalid):
                    nonlocal fired
                    if h.phase == "subscription-enabled" and not fired:
                        fired = True
                        if invalid:
                            h.wire.gap(False, h.new_peer)
                        raise RuntimeError("original capture failure")

                with self.scenario(h, parent_guard=parent) as runner:
                    with self.assertRaisesRegex(
                        RuntimeError, "original capture failure"
                    ):
                        runner.run()
                    document = api.read_document(h.root / api.PHYSICAL_NAME)
                    self.assertEqual(
                        document["primary_error"], "original capture failure"
                    )
                    disconnects = [
                        c
                        for c in h.wire.commands
                        if c.service == 1 and c.opcode == GAP_DISCONNECT
                    ]
                    if invalid:
                        self.assertTrue(
                            any(
                                "owned-peer-disconnect:" in error
                                for error in document["cleanup_errors"]
                            )
                        )
                        self.assertFalse(disconnects)
                    else:
                        self.assertEqual(len(disconnects), 1)
                        self.assertEqual(document["cleanup_errors"], [])
                        self.assertEqual(h.fixture._connected_peers, set())
                        self.assertIsNone(runner.peer)
                    self.readonly(h.wire.commands)

    def test_failed_limits_are_published_even_if_cleanup_later_observes_idle(self):
        h = self.make()
        h.retain_watchdog()
        begin, send = h.begin, h.wire.send

        def begin_busy():
            begin()
            h.wire.limits_flags = 5

        def cleaned(frame):
            if frame.service == 1 and frame.opcode == GAP_DISCONNECT:
                h.wire.limits_flags = 0
            send(frame)

        h.begin, h.wire.send = begin_busy, cleaned
        with (
            self.scenario(h) as runner,
            self.assertRaisesRegex(RuntimeError, "limits boot/quarantine drift"),
        ):
            runner.run()
        document = api.read_document(h.root / api.PHYSICAL_NAME)
        self.assertEqual(document["status"], "failed")
        self.assertEqual(
            document["primary_error"], "recovery retained limits boot/quarantine drift"
        )
        failed = [s for s in document["fault_samples"] if s["error"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["limits"]["engine_flags"], 5)
        self.assertEqual(
            failed[0]["limits"]["boot_nonce"], h.retained_ticket.boot_nonce.hex()
        )
        self.assertEqual(
            fault.decode_status(bytes.fromhex(failed[0]["status_wire"])).ticket,
            h.retained_ticket,
        )
        self.assertEqual(document["cleanup_errors"], [])
        self.assertEqual(document["cleanup"]["peer_count"], 0)
        ledger._hold(document["hold_status"], h.retained_ticket)
        self.readonly(h.wire.commands)
