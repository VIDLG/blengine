"""Production writer/relay/typed-BTP crossing; synthetic only, never RF."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from . import direct_fixture, provider
from . import write_reject as api
from .cancellation_control import Control, read_document
from .fault_control import (
    Opcode,
    Operation,
    Phase,
    StatusFlags,
    status_document,
    status_from_document,
)
from .support.write_reject_emulator import Harness


class WriteRejectTests(unittest.TestCase):
    def make(self, platform="android", fault=""):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Harness(Path(temporary.name), platform, fault)

    def test_real_writer_authorizes_real_relay_only_after_physical_boundaries(self):
        for platform in ("android", "ios"):
            with self.subTest(platform=platform):
                h = self.make(platform)
                h.run()
                report = read_document(h.root / api.PHYSICAL_NAME)
                self.assertEqual(report["status"], "passed")
                self.assertEqual(h.requests, list(api.REQUESTS))
                self.assertEqual(h.relay.sent, list(api.PHASES))
                self.assertEqual(h.production_boundaries, list(api.BOUNDARIES))
                self.assertEqual(h.wire.local_reads, [b"\x00", b"\x00", b"\x22"])
                self.assertEqual(h.wire.write_callbacks, 1)  # normal recovery only
                self.assertEqual(h.fixture._connected_peers, set())
                opcodes = [c.opcode for c in h.wire.commands if c.service == 255]
                self.assertEqual(opcodes.count(Opcode.ARM_WRITE), 1)
                self.assertEqual(opcodes.count(Opcode.RESOLVE), 1)
                self.assertNotIn(Opcode.ARM_READ, opcodes)
                self.assertNotIn(Opcode.ABORT, opcodes)
                rows = {r["boundary"]: r for r in report["boundaries"] if "status" in r}
                self.assertEqual(rows["rejected"]["status"], rows["recovery"]["status"])
                self.assertEqual(rows["recovery"]["status"], rows["closed"]["status"])
                original = status_from_document(rows["closed"]["status"])
                self.assertEqual(original.observed, b"\x11")
                self.assertFalse(
                    original.flags
                    & (StatusFlags.CALLBACK_INVOKED | StatusFlags.COMMIT_OBSERVED)
                )
                self.assertEqual(original.phase, Phase.RETIRED)

    def test_bad_capture_or_expired_budget_never_dispatches_reject(self):
        for fault in (
            "wrong-request",
            "wrong-ticket",
            "callback",
            "commit",
            "expired",
            "late-reject",
            "lease-loss",
            "local-att-error",
        ):
            with self.subTest(fault=fault):
                h = self.make(fault=fault)
                with self.assertRaises((ValueError, RuntimeError)):
                    h.run()
                report = read_document(h.root / api.PHYSICAL_NAME)
                self.assertEqual(report["status"], "failed")
                self.assertTrue(report["primary_error"])
                self.assertFalse((h.root / "signal-rejected.json").exists())
                self.assertFalse(
                    any(
                        c.opcode == Opcode.RESOLVE
                        for c in h.wire.commands
                        if c.service == 255
                    )
                )
                if fault in ("wrong-ticket", "lease-loss"):
                    self.assertTrue(report["cleanup_errors"])
                    self.assertFalse(
                        any(
                            c.opcode == Opcode.ABORT
                            for c in h.wire.commands
                            if c.service == 255
                        )
                    )

    def test_retained_drift_pinned_evidence_and_native_close_preserve_failure(self):
        for fault in ("retained-drift", "pinned-evidence", "native-close"):
            with self.subTest(fault=fault):
                h = self.make(fault=fault)
                with self.assertRaises((ValueError, RuntimeError)):
                    h.run()
                report = read_document(h.root / api.PHYSICAL_NAME)
                self.assertEqual(report["status"], "failed")
                self.assertIsNotNone(report["primary_error"])
                self.assertFalse(
                    any(
                        c.opcode == Opcode.ABORT
                        for c in h.wire.commands
                        if c.service == 255
                    )
                )
                self.assertEqual(
                    sum(
                        c.opcode == Opcode.ARM_WRITE
                        for c in h.wire.commands
                        if c.service == 255
                    ),
                    1,
                )

    def test_publication_failure_cannot_authorize_signal_or_hide_primary(self):
        h = self.make()
        original = api.write_document

        def publish(path, document):
            if path.name == api.PHYSICAL_NAME and any(
                r["boundary"] == "held" for r in document["boundaries"]
            ):
                raise OSError("injected publish failure")
            original(path, document)

        with (
            patch.object(api, "write_document", side_effect=publish),
            self.assertRaises(OSError) as raised,
        ):
            h.run()
        self.assertIn("publish failure", str(raised.exception))
        self.assertTrue(getattr(raised.exception, "__notes__", []))
        self.assertFalse((h.root / "signal-held.json").exists())
        self.assertNotEqual(
            read_document(h.root / api.PHYSICAL_NAME)["status"], "passed"
        )

    def test_final_publication_and_fixture_close_failures_cannot_leave_pass(self):
        h = self.make()
        original = api.write_document
        failed = False

        def publish(path, document):
            nonlocal failed
            if (
                path.name == api.PHYSICAL_NAME
                and document["status"] == "passed"
                and not failed
            ):
                failed = True
                raise OSError("final publication failed")
            original(path, document)

        with (
            patch.object(api, "write_document", side_effect=publish),
            self.assertRaises(OSError),
        ):
            h.run()
        report = read_document(h.root / api.PHYSICAL_NAME)
        self.assertEqual(report["status"], "failed")
        self.assertIn("final publication failed", report["primary_error"])
        self.assertFalse(
            any(c.opcode == Opcode.ABORT for c in h.wire.commands if c.service == 255)
        )

        h = self.make()
        h.run()
        fixture = Mock()
        fixture.start.return_value = {"read-write": 51}
        fixture._stop_requested = True
        fixture.stop.side_effect = OSError("fixture close failed")
        with (
            patch.object(direct_fixture, "DirectFixture", return_value=fixture),
            patch.object(direct_fixture.signal, "signal"),
            patch.object(api, "run"),
            self.assertRaises(OSError),
        ):
            direct_fixture.serve(
                h.plan.port,
                h.plan.profile_path,
                h.root,
                api.MODE,
                fixture_session=h.plan,
            )
        report = read_document(h.root / api.PHYSICAL_NAME)
        self.assertEqual(report["status"], "failed")
        self.assertIsNone(report["primary_error"])
        self.assertIn("fixture close failed", report["cleanup_errors"][0])

    def test_control_is_finite_and_existing_read_contracts_are_unchanged(self):
        h = self.make()
        self.assertEqual(
            h.control.uri("rejected"),
            f"blehub-rf://control/{h.plan.session_id}/10/rf-write-reject/{h.control.token}/rejected",
        )
        self.assertEqual(h.control.filename, api.CONTROL_NAME)
        for flow, mode in (
            (10, "rf-watchdog"),
            (9, "rf-write-reject"),
            (11, "rf-write-reject"),
        ):
            with self.assertRaises(ValueError):
                Control(h.plan.session_id, flow, mode, h.control.token)
        self.assertEqual(
            Control(h.plan.session_id, 8, "rf-cancellation", h.control.token).requests,
            ("arm", "read-dispatched", "release"),
        )
        self.assertEqual(
            Control(h.plan.session_id, 9, "rf-watchdog", h.control.token).phases,
            ("armed", "held", "released", "recovery"),
        )

    def test_relay_rejects_foreign_duplicate_early_and_corrupt_physical_signal(self):
        h = self.make()
        h.run()
        for mutation in ("early", "duplicate", "foreign"):
            relay = api.WriteRejectRelay(h.root)
            text = h.text
            if mutation == "duplicate":
                text += api.PREFIX + h.control.marker("observed", "armed") + "\n"
            elif mutation == "foreign":
                text = text.replace(h.control.token, h.control.session)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                relay.observe(text)
        relay = h.relay
        with self.assertRaises(ValueError):
            relay.forwarded("close-allowed")
        report = read_document(h.root / api.PHYSICAL_NAME)
        armed = next(r for r in report["boundaries"] if r["boundary"] == "armed")
        status = status_from_document(armed["status"])
        armed["status"] = status_document(
            replace(status, ticket=replace(status.ticket, operation=Operation.READ))
        )
        # A malformed operation must not become authorized by rewriting metadata.
        with self.assertRaises(ValueError):
            api.validate_boundary(report, h.control, "armed")

    def test_named_dispatch_and_missing_metadata_fail_before_startup(self):
        h = self.make()
        with (
            patch.object(provider, "_process_identity") as identity,
            self.assertRaises(provider.Nrf52840Error),
        ):
            provider.direct_start(str(h.root), api.MODE, fixture_request=None)
        identity.assert_not_called()
        fixture = Mock()
        fixture.start.return_value = {"read-write": 51}
        fixture._stop_requested = True
        with (
            patch.object(direct_fixture, "DirectFixture", return_value=fixture),
            patch.object(direct_fixture.signal, "signal"),
            patch.object(api, "run") as run,
        ):
            direct_fixture.serve(
                h.plan.port,
                h.plan.profile_path,
                h.root,
                api.MODE,
                fixture_session=h.plan,
            )
        run.assert_called_once_with(fixture, h.root, {"read-write": 51})
        self.assertTrue(fixture._strict_recovery_cleanup)
        fixture.stop.assert_called_once()
        fixture.run_cancellation_coordination.assert_not_called()
