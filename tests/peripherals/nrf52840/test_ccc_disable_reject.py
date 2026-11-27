"""Production CCC disable writer/relay against bounded synthetic BTP and phone I/O."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import ccc_disable_reject as api
from tests.peripherals.nrf52840 import direct_fixture, provider
from tests.peripherals.nrf52840.cancellation_control import Control, read_document
from tests.peripherals.nrf52840.fault_control import Opcode, status_from_document
from tests.peripherals.nrf52840.support.ccc_disable_reject_emulator import Harness
from tests.peripherals.nrf52840.support.ccc_wire import (
    CCC_HANDLE,
    VALUE_HANDLE,
)


class CccDisableRejectTests(unittest.TestCase):
    def make(self, platform="android", fault="", **options):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Harness(Path(temporary.name), platform, fault, **options)

    def test_eight_barriers_same_subscription_enabled_readback_and_retained_receipt(
        self,
    ):
        for platform in ("android", "ios"):
            for inline in (False, True):
                with self.subTest(platform=platform, inline=inline):
                    h = self.make(platform, inline=inline)
                    h.run()
                    report = read_document(h.root / api.PHYSICAL_NAME)
                    self.assertEqual(report["status"], "passed")
                    self.assertEqual(h.requests, list(api.REQUESTS))
                    self.assertEqual(h.relay.sent, list(api.PHASES))
                    self.assertEqual(report["target_handle"], CCC_HANDLE)
                    self.assertNotEqual(CCC_HANDLE, VALUE_HANDLE + 1)
                    self.assertEqual(h.wire.declaration_reads, [])
                    self.assertEqual(
                        h.wire.uuid_lookups, [api.READ_UUID, api.UPDATES_UUID]
                    )
                    self.assertEqual(
                        h.wire.submissions,
                        [
                            (VALUE_HANDLE, b"\xef"),
                            (VALUE_HANDLE, b"\xe1"),
                            (VALUE_HANDLE, b"\xf0"),
                        ],
                    )
                    self.assertEqual(
                        h.wire.local_reads,
                        [(CCC_HANDLE, api.BEFORE)] * 5
                        + [(CCC_HANDLE, api.REQUESTED)] * 2,
                    )
                    self.assertEqual(h.wire.request.before, b"\x01\x00")
                    self.assertEqual(h.wire.request.requested, b"\x00\x00")
                    rows = {r["boundary"]: r for r in report["boundaries"]}
                    for phase in (*api.PHASES, "closed"):
                        api.validate_boundary(report, h.control, phase)
                    for phase in (*api.PHASES[3:], "closed"):
                        self.assertEqual(
                            rows[phase]["status"], rows["rejected"]["status"]
                        )
                    status = status_from_document(rows["closed"]["status"])
                    self.assertEqual(int(status.flags), 0x0299)
                    self.assertEqual(status.before, b"\x01\x00")
                    self.assertEqual(status.after, b"\x01\x00")
                    self.assertEqual(status.observed, b"\x00\x00")
                    self.assertEqual(set(vars(status.refs).values()), {0})
                    commands = [c.opcode for c in h.wire.commands if c.service == 255]
                    self.assertEqual(commands.count(Opcode.ARM_CCC), 1)
                    self.assertEqual(commands.count(Opcode.RESOLVE), 1)
                    self.assertFalse(
                        set(commands)
                        & {Opcode.ABORT, Opcode.ARM_READ, Opcode.ARM_WRITE}
                    )
                    facts = api.runtime_facts(
                        h.text,
                        run_id=h.plan.session_id,
                        platform=platform,
                        complete=True,
                    )
                    self.assertEqual(tuple(facts["steps"]), api.STEPS)
                    self.assertEqual(facts["warm_resources"]["subscriptions"], 1)
                    self.assertEqual(
                        facts["resources"], dict.fromkeys(api.RESOURCE_FIELDS, 0)
                    )
                    self.assertEqual(
                        [
                            e["value"]
                            for e in facts["events"]
                            if e["kind"] == "notificationReceived"
                        ],
                        [[239], [225]],
                    )

    def test_target_route_and_enabled_getter_errors_never_arm(self):
        for fault in (
            "duplicate-ccc",
            "sibling-ccc-only",
            "wrong-permissions",
            "wrong-lookup",
            "lookup-error",
            "wrong-order",
            "split-service",
            "duplicate-value-outside",
            "local-att-error",
            "lost-enabled",
        ):
            with self.subTest(fault=fault):
                h = self.make(fault=fault)
                with self.assertRaises((ValueError, RuntimeError)):
                    h.run()
                self.assertFalse(
                    any(
                        c.opcode == Opcode.ARM_CCC
                        for c in h.wire.commands
                        if c.service == 255
                    )
                )
                self.assertFalse((h.root / "signal-armed.json").exists())
                report = read_document(h.root / api.PHYSICAL_NAME)
                self.assertEqual(report["status"], "failed")
                self.assertTrue(report["primary_error"])
        h = self.make()
        h.wire.enabled_readback = (
            False  # Old getter error 12 is not an enabled witness.
        )
        with self.assertRaisesRegex(ValueError, "att_response=0x0c value=0100"):
            h.run()
        self.assertFalse((h.root / "signal-baseline-value-sent.json").exists())

    def test_fault_retirement_and_resource_failures_do_not_produce_a_pass(self):
        for fault in (
            "wrong-request",
            "wrong-ticket",
            "callback",
            "commit",
            "normal-path",
            "expired",
            "late-reject",
            "lease-loss",
            "pinned-evidence",
            "retained-drift",
            "submission-failure",
            "remote-disabled",
            "short-host-silence",
            "native-close",
        ):
            with self.subTest(fault=fault):
                h = self.make(fault=fault)
                with self.assertRaises((ValueError, RuntimeError)):
                    h.run()
                report = read_document(h.root / api.PHYSICAL_NAME)
                self.assertEqual(report["status"], "failed")
                self.assertTrue(report["primary_error"])
                if fault in ("wrong-ticket", "lease-loss"):
                    self.assertTrue(report["cleanup_errors"])
                    self.assertFalse(
                        any(
                            c.opcode == Opcode.ABORT
                            for c in h.wire.commands
                            if c.service == 255
                        )
                    )
                if fault == "remote-disabled":
                    self.assertFalse(
                        (h.root / "signal-continued-value-sent.json").exists()
                    )

    def test_relay_requires_original_prefix_physical_proof_and_single_forwarding(self):
        h = self.make()
        h.run()
        for text in (h.text, h.text.replace(h.control.token, h.control.session)):
            with self.assertRaises(ValueError):
                api.CccDisableRejectRelay(h.root).observe(text)
        with self.assertRaises(ValueError):
            h.relay.observe(h.text[:-10])
        with self.assertRaises(ValueError):
            h.relay.forwarded("close-allowed")
        # A fresh relay with a valid first readiness cannot authorize a fabricated prefix.
        first = h.text[
            : h.text.index(
                api.PREFIX + h.control.marker("observed", "baseline-value-sent")
            )
        ]
        relay = api.CccDisableRejectRelay(h.root)
        relay.observe(first)
        report = read_document(h.root / api.PHYSICAL_NAME)
        report["status"] = "running"
        report["boundaries"] = report["boundaries"][:3]
        report["boundaries"][-1]["runtime_sha256"] = "00" * 32
        api.write_document(h.root / api.PHYSICAL_NAME, report)
        with self.assertRaisesRegex(ValueError, "runtime witness drift"):
            relay.pending()
        api.write_document(
            h.root / api.CONTROL_NAME, vars(replace(h.control, token=h.control.session))
        )
        with self.assertRaises(ValueError):
            relay.pending()

    def test_publication_failure_does_not_signal_or_replace_primary(self):
        for phase in ("baseline-value-sent", "held", "continued-value-sent", "passed"):
            with self.subTest(phase=phase):
                h = self.make()
                original = api.write_document
                failed = False

                def publish(path, document, phase=phase, original=original):
                    nonlocal failed
                    match = (
                        document.get("status") == "passed"
                        if phase == "passed"
                        else any(
                            r["boundary"] == phase
                            for r in document.get("boundaries", [])
                        )
                    )
                    if path.name == api.PHYSICAL_NAME and match and not failed:
                        failed = True
                        raise OSError("physical publish failure")
                    original(path, document)

                with (
                    patch.object(api, "write_document", side_effect=publish),
                    self.assertRaises(OSError),
                ):
                    h.run()
                report = read_document(h.root / api.PHYSICAL_NAME)
                self.assertIn("physical publish failure", report["primary_error"])
                self.assertEqual(report["status"], "failed")
                if phase != "passed":
                    self.assertFalse((h.root / f"signal-{phase}.json").exists())

    def test_flow12_named_provider_dispatch_and_cleanup_are_independent(self):
        h = self.make()
        self.assertEqual(h.control.requests, api.REQUESTS)
        self.assertEqual(h.control.phases, api.PHASES)
        self.assertEqual(h.control.filename, api.CONTROL_NAME)
        self.assertIn(
            "/12/rf-ccc-disable-reject/", h.control.uri("continued-value-sent")
        )
        for flow, mode in (
            (11, api.RUNTIME_MODE),
            (12, "rf-ccc-enable-reject"),
            (12, api.MODE),
        ):
            with self.assertRaises(ValueError):
                Control(h.plan.session_id, flow, mode, h.control.token)
        with (
            patch.object(provider, "_process_identity") as identity,
            self.assertRaises(provider.Nrf52840Error),
        ):
            provider.direct_start(str(h.root), api.MODE, fixture_request=None)
        identity.assert_not_called()
        for command in ("direct-start", "direct-serve"):
            with patch(
                "sys.argv", ["provider", command, "--coordination-mode", api.MODE]
            ):
                self.assertEqual(provider.parse_args().coordination_mode, api.MODE)
        fixture = Mock()
        fixture.start.return_value = h.handles
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
        run.assert_called_once_with(fixture, h.root, h.handles)
        self.assertTrue(fixture._strict_recovery_cleanup)
        self.assertTrue(fixture._preserve_gatt)
        fixture.stop.assert_called_once()
        h.run()
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
