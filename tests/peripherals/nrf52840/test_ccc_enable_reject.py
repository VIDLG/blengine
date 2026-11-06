from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import ccc_enable_reject as api
from tests.peripherals.nrf52840 import direct_fixture, provider
from tests.peripherals.nrf52840.cancellation_control import Control, read_document
from tests.peripherals.nrf52840.fault_control import (
    Opcode,
    Operation,
    StatusFlags,
    status_document,
    status_from_document,
)
from tests.peripherals.nrf52840.support.ccc_enable_reject_emulator import (
    CCC_HANDLE,
    VALUE_HANDLE,
    Harness,
)


class CccEnableRejectTests(unittest.TestCase):
    def make(self, platform="android", fault="", **options):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Harness(Path(temporary.name), platform, fault, **options)

    def test_real_provider_and_relay_seven_barriers_inline_or_delayed_value(self):
        for platform in ("android", "ios"):
            for inline in (True, False):
                with self.subTest(platform=platform, inline=inline):
                    h = self.make(platform, inline=inline)
                    h.run()
                    report = read_document(h.root / api.PHYSICAL_NAME)
                    self.assertEqual(report["status"], "passed")
                    self.assertEqual(h.requests, list(api.REQUESTS))
                    self.assertEqual(h.relay.sent, list(api.PHASES))
                    self.assertEqual(report["target_handle"], CCC_HANDLE)
                    self.assertEqual(report["topology_method"], api.TOPOLOGY_METHOD)
                    self.assertNotIn("declarations", report)
                    self.assertEqual(h.wire.declaration_reads, [])
                    self.assertEqual(
                        h.wire.uuid_lookups, [api.READ_UUID, api.UPDATES_UUID]
                    )
                    self.assertNotEqual(CCC_HANDLE, VALUE_HANDLE + 1)
                    self.assertEqual(
                        h.wire.submissions,
                        [(VALUE_HANDLE, b"\xef"), (VALUE_HANDLE, b"\xf0")],
                    )
                    self.assertEqual(h.wire.local_reads, [(CCC_HANDLE, api.BEFORE)] * 4)
                    self.assertEqual(h.wire.ccc, api.BEFORE)
                    commands = [c for c in h.wire.commands if c.service == 255]
                    self.assertEqual(
                        sum(c.opcode == Opcode.ARM_CCC for c in commands), 1
                    )
                    self.assertEqual(
                        sum(c.opcode == Opcode.RESOLVE for c in commands), 1
                    )
                    self.assertFalse(
                        any(
                            c.opcode
                            in (Opcode.ARM_WRITE, Opcode.ARM_READ, Opcode.ABORT)
                            for c in commands
                        )
                    )
                    self.assertEqual(h.wire.request.before, bytes.fromhex("0000"))
                    self.assertEqual(h.wire.request.requested, bytes.fromhex("0100"))
                    rows = {r["boundary"]: r for r in report["boundaries"]}
                    for phase in (*api.PHASES, "closed"):
                        api.validate_boundary(report, h.control, phase)
                    for phase in (*api.PHASES[2:], "closed"):
                        self.assertEqual(
                            rows[phase]["status"], rows["rejected"]["status"]
                        )
                    receipt = status_from_document(rows["closed"]["status"])
                    self.assertEqual(receipt.flags, api.REJECT_FLAGS)
                    self.assertEqual(int(receipt.flags), 0x299)
                    self.assertFalse(
                        receipt.flags
                        & (StatusFlags.CALLBACK_INVOKED | StatusFlags.COMMIT_OBSERVED)
                    )
                    self.assertEqual(
                        (
                            rows["closed"]["gap_attached"],
                            rows["closed"]["gap_detached"],
                        ),
                        (1, 1),
                    )
                    facts = api.runtime_facts(
                        h.text,
                        run_id=h.plan.session_id,
                        platform=platform,
                        complete=True,
                    )
                    self.assertEqual(tuple(facts["steps"]), api.STEPS)
                    self.assertTrue(facts["passed"])
                    self.assertEqual(sum(facts["resources"].values()), 0)

    def test_declaration_att_errors_are_not_consumed_by_metadata_discovery(self):
        h = self.make()
        for handle in (30, 36):
            result = h.fixture._gatt.get_attribute_value(handle)
            self.assertEqual(result.att_response, 12)
            self.assertEqual(result.value, h.wire.declarations[handle])
        h.run()
        self.assertEqual(h.wire.declaration_reads, [30, 36])
        self.assertEqual(read_document(h.root / api.PHYSICAL_NAME)["status"], "passed")
        h = self.make(fault="local-att-error")
        with self.assertRaisesRegex(
            ValueError, r"handle=0x002d att_response=0x01 value=0000"
        ):
            h.run()
        self.assertFalse((h.root / "signal-armed.json").exists())

    def test_doctor_mapping_is_rechecked_against_actual_unique_tester_handles(self):
        h = self.make()
        h.handles["updates"] = 52
        with self.assertRaisesRegex(ValueError, "UUID/doctor/inventory mismatch"):
            h.run()
        self.assertFalse((h.root / "signal-armed.json").exists())

    def test_target_capture_preconditions_fail_without_reject_or_provision(self):
        for fault in (
            "duplicate-ccc",
            "sibling-ccc-only",
            "wrong-permissions",
            "wrong-lookup",
            "lookup-error",
            "wrong-order",
            "split-service",
            "duplicate-value-outside",
            "enabled-before",
            "local-att-error",
            "wrong-request",
            "wrong-ticket",
            "callback",
            "commit",
            "normal-path",
            "expired",
            "late-reject",
            "lease-loss",
        ):
            with self.subTest(fault=fault):
                h = self.make(fault=fault)
                with self.assertRaises((ValueError, RuntimeError)):
                    h.run()
                report = read_document(h.root / api.PHYSICAL_NAME)
                self.assertEqual(report["status"], "failed")
                self.assertTrue(report["primary_error"])
                self.assertFalse(
                    any(
                        c.service == 255 and c.opcode == Opcode.RESOLVE
                        for c in h.wire.commands
                    )
                )
                self.assertFalse((h.root / "signal-rejected.json").exists())
                self.assertFalse(h.wire.submissions)
                if fault in ("wrong-ticket", "lease-loss"):
                    self.assertTrue(report["cleanup_errors"])
                    self.assertFalse(
                        any(
                            c.service == 255 and c.opcode == Opcode.ABORT
                            for c in h.wire.commands
                        )
                    )

    def test_retirement_submission_silence_and_close_failures_remain_failed(self):
        for fault in (
            "pinned-evidence",
            "retained-drift",
            "submission-failure",
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
                self.assertEqual(
                    sum(
                        c.service == 255 and c.opcode == Opcode.ARM_CCC
                        for c in h.wire.commands
                    ),
                    1,
                )
                self.assertFalse(
                    any(
                        c.service == 255 and c.opcode == Opcode.ABORT
                        for c in h.wire.commands
                    )
                )

    def test_parser_rejects_foreign_duplicate_domains_routes_bytes_and_resources(self):
        h = self.make()
        h.run()
        replacements = (
            ('"code": "14"', '"code": "13"'),
            ('"platform": "platform_android"', '"platform": "platform_apple"'),
            ('"subscriptionId": "33012"', '"subscriptionId": "33009"'),
            ('"characteristicId": "72"', '"characteristicId": "71"'),
            ('"connectionId": "33004"', '"connectionId": "33099"'),
            ('"gattSessionId": "33007"', '"gattSessionId": "33099"'),
            ('"value": [239]', '"value": [240]'),
            (
                '"actualNotificationMode": "Notification"',
                '"actualNotificationMode": "Indication"',
            ),
            ("elapsedMs=1000", "elapsedMs=999"),
            ('"pendingIo": 0', '"pendingIo": 1'),
            ("readCharacteristicId=72", "readCharacteristicId=71"),
            ("step:queued-read:passed", "step:unexpected:passed"),
            ("resources:phase=failed-enable", "resources:phase=closed"),
        )
        for before, after in replacements:
            with self.subTest(before=before), self.assertRaises(ValueError):
                api.runtime_facts(
                    h.text.replace(before, after),
                    run_id=h.plan.session_id,
                    platform="android",
                    complete=True,
                )
        event_lines = [line for line in h.text.splitlines() if ":event:" in line]
        for line in event_lines:
            event = json.loads(line.split(":event:", 1)[1])
            if event["kind"] in (
                "notificationReceived",
                "commandCompleted",
                "notificationStateChanged",
            ):
                with self.subTest(duplicate=event), self.assertRaises(ValueError):
                    api.runtime_facts(
                        h.text.replace(line, line + "\n" + line, 1),
                        run_id=h.plan.session_id,
                        complete=True,
                    )
        with self.assertRaises(ValueError):
            api.runtime_facts(
                h.text, run_id=h.plan.session_id, platform="ios", complete=True
            )

    def test_notification_race_is_only_admitted_after_recovery_readiness(self):
        h = self.make()
        h.run()
        lines = h.text.splitlines()
        value = next(line for line in lines if '"kind": "notificationReceived"' in line)
        lines.remove(value)
        for anchor in ("step:recovery-enabled:passed", "rf-control:ready:session="):
            changed = list(lines)
            position = next(i for i, line in enumerate(changed) if anchor in line)
            changed.insert(position, value)
            with self.subTest(anchor=anchor), self.assertRaises(ValueError):
                api.runtime_facts(
                    "\n".join(changed), run_id=h.plan.session_id, complete=True
                )
        # Inline notification in the successful run is before value-sent ACK.
        self.assertLess(
            h.text.index(value),
            h.text.index(h.control.marker("observed", "value-sent")),
        )

    def test_relay_prefix_control_signal_evidence_and_single_forwarding(self):
        h = self.make()
        h.run()
        for text in (
            h.text,
            h.text.replace(h.control.token, h.control.session),
            h.text + api.PREFIX + h.control.marker("observed", "held") + "\n",
        ):
            relay = api.CccEnableRejectRelay(h.root)
            with self.assertRaises(ValueError):
                relay.observe(text)
        with self.assertRaises(ValueError):
            h.relay.observe(h.text[:-20])
        with self.assertRaises(ValueError):
            h.relay.forwarded("close-allowed")
        report = read_document(h.root / api.PHYSICAL_NAME)
        row = next(r for r in report["boundaries"] if r["boundary"] == "armed")
        status = status_from_document(row["status"])
        row["status"] = status_document(
            replace(status, ticket=replace(status.ticket, operation=Operation.READ))
        )
        with self.assertRaises(ValueError):
            api.validate_boundary(report, h.control, "armed")

    def test_publication_failures_preserve_primary_and_never_signal(self):
        for fail_at in ("held", "value-sent", "passed"):
            with self.subTest(fail_at=fail_at):
                h = self.make()
                original = api.write_document
                failed = False

                def publish(path, document, fail_at=fail_at, original=original):
                    nonlocal failed
                    matches = (
                        document.get("status") == "passed"
                        if fail_at == "passed"
                        else any(
                            r["boundary"] == fail_at
                            for r in document.get("boundaries", [])
                        )
                    )
                    if path.name == api.PHYSICAL_NAME and matches and not failed:
                        failed = True
                        raise OSError("injected physical publication failure")
                    original(path, document)

                with (
                    patch.object(api, "write_document", side_effect=publish),
                    self.assertRaises(OSError),
                ):
                    h.run()
                report = read_document(h.root / api.PHYSICAL_NAME)
                self.assertEqual(report["status"], "failed")
                self.assertIn("publication failure", report["primary_error"])
                if fail_at != "passed":
                    self.assertFalse((h.root / f"signal-{fail_at}.json").exists())

    def test_queued_read_callback_and_warm_snapshot_races_do_not_change_routes(self):
        h = self.make()
        h.run()
        lines = h.text.splitlines()
        queued = [
            line
            for line in lines
            if ":event:" in line and '"commandId": "33011"' in line
        ]
        for line in queued:
            lines.remove(line)
        position = next(
            i for i, line in enumerate(lines) if "step:enable-rejected:passed" in line
        )
        lines[position:position] = queued
        warm = next(line for line in lines if "resources:phase=failed-enable:" in line)
        lines.remove(warm)
        position = next(
            i for i, line in enumerate(lines) if "step:queued-read:passed" in line
        )
        lines.insert(position + 1, warm)
        facts = api.runtime_facts(
            "\n".join(lines), run_id=h.plan.session_id, complete=True
        )
        self.assertTrue(facts["passed"])

    def test_runtime_rejects_rejected_state_false_byte_bad_types_and_control_replay(
        self,
    ):
        h = self.make()
        h.run()
        rejected = next(
            line
            for line in h.text.splitlines()
            if ":event:" in line and '"commandId": "33010"' in line
        )
        native = json.loads(rejected.split(":event:", 1)[1])
        for mutation in (
            {
                **native,
                "kind": "notificationStateChanged",
                "error": None,
                "subscriptionId": "33009",
                "notificationState": "Enabled",
            },
            {**native, "commandId": ["33010"]},
        ):
            line = api.PREFIX + api.RUNTIME_MODE + ":event:" + json.dumps(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                api.runtime_facts(
                    h.text.replace(rejected, line),
                    run_id=h.plan.session_id,
                    complete=True,
                )
        control = api.PREFIX + h.control.marker("ready", "arm")
        for text in (
            h.text.replace(control, control + "\n" + control),
            h.text.replace(
                control, control.replace(h.control.token, h.control.session)
            ),
            h.text.replace('"value": [0]', '"value": [false]'),
            h.text.replace('"connectionState": "Connected"', '"connectionState": {}'),
            h.text + api.PREFIX + api.RUNTIME_MODE + ":event:{}\n",
        ):
            with self.subTest(text=text[-160:]), self.assertRaises(ValueError):
                api.runtime_facts(text, run_id=h.plan.session_id, complete=True)

    def test_physical_topology_arm_and_submission_cannot_be_relabelled(self):
        h = self.make()
        h.run()
        original = read_document(h.root / api.PHYSICAL_NAME)
        mutations = (
            ("descriptor", lambda d: d.update(target_handle=54)),
            (
                "interval",
                lambda d: d["topology"].remove(
                    next(a for a in d["topology"] if a["handle"] == CCC_HANDLE)
                ),
            ),
            (
                "declaration",
                lambda d: d["declaration_associations"].update({"36": 65535}),
            ),
            (
                "arm",
                lambda d: next(
                    r for r in d["boundaries"] if r["boundary"] == "armed"
                ).update(arm_request_wire="00"),
            ),
            (
                "delivery",
                lambda d: next(
                    r for r in d["boundaries"] if r["boundary"] == "value-sent"
                )["submission"].update(delivery="transmitted"),
            ),
            (
                "peer",
                lambda d: next(
                    r for r in d["boundaries"] if r["boundary"] == "rejected"
                )["local"].update(peer="00000000000000"),
            ),
            (
                "reject-count",
                lambda d: d["boundaries"].append(
                    next(
                        r
                        for r in d["boundaries"]
                        if r["boundary"] == "resolve-dispatched"
                    )
                ),
            ),
        )
        for name, mutate in mutations:
            document = json.loads(json.dumps(original))
            mutate(document)
            with self.subTest(name=name), self.assertRaises(ValueError):
                api.validate_boundary(
                    document,
                    h.control,
                    "value-sent" if name == "delivery" else "rejected",
                )

    def test_closed_validator_requires_gap_pair_idle_limits_and_native_zero(self):
        h = self.make()
        h.run()
        original = read_document(h.root / api.PHYSICAL_NAME)
        for mutation in ("gap", "limits", "resources"):
            document = json.loads(json.dumps(original))
            row = next(r for r in document["boundaries"] if r["boundary"] == "closed")
            if mutation == "gap":
                document["boundaries"].append(
                    next(r for r in document["boundaries"] if r["boundary"] == "gap")
                )
            elif mutation == "limits":
                row["limits"]["peer_present"] = True
            else:
                row["resources"]["subscriptions"] = 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                api.validate_boundary(document, h.control, "closed")

    def test_pending_rechecks_live_deadline_runtime_hash_and_peer_proof(self):
        for mutation in ("runtime", "deadline", "peer-proof", "foreign-signal"):
            with self.subTest(mutation=mutation):
                h = self.make()
                pending = h.relay.pending
                injected = False

                def corrupt(mutation=mutation, h=h, pending=pending):
                    nonlocal injected
                    path = h.root / "signal-armed.json"
                    if path.exists() and not injected:
                        injected = True
                        document = read_document(h.root / api.PHYSICAL_NAME)
                        row = next(
                            r
                            for r in document["boundaries"]
                            if r["boundary"] == "armed"
                        )
                        if mutation == "runtime":
                            row["runtime_sha256"] = "0" * 64
                        elif mutation == "deadline":
                            document["deadline"] = h.clock.now - 1
                        elif mutation == "peer-proof":
                            row["local"]["limits_after"]["connection_epoch"] += 1
                        else:
                            api.write_document(
                                path, dict(read_document(path), token=h.plan.session_id)
                            )
                        api.write_document(h.root / api.PHYSICAL_NAME, document)
                    return pending()

                h.relay.pending = corrupt
                with self.assertRaises(ValueError):
                    h.run()
                self.assertTrue(injected)
                self.assertEqual(h.relay.sent, [])
                self.assertFalse(
                    any(
                        c.service == 255 and c.opcode == Opcode.RESOLVE
                        for c in h.wire.commands
                    )
                )
                self.assertEqual(
                    read_document(h.root / api.PHYSICAL_NAME)["status"], "failed"
                )

    def test_provider_preflight_and_start_failure_do_not_touch_a_fixture(self):
        h = self.make()
        wrong = dict(read_document(h.root / api.CONTROL_NAME), flow=10)
        api.write_document(h.root / api.CONTROL_NAME, wrong)
        with (
            patch.object(direct_fixture, "DirectFixture") as constructor,
            self.assertRaises(ValueError),
        ):
            direct_fixture.serve(
                h.plan.port,
                h.plan.profile_path,
                h.root,
                api.MODE,
                fixture_session=h.plan,
            )
        constructor.assert_not_called()
        api.write_document(h.root / api.CONTROL_NAME, dict(wrong, flow=11))
        with (
            patch.object(
                direct_fixture,
                "DirectFixture",
                side_effect=OSError("serial admission failed"),
            ),
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
        self.assertEqual(report["boundaries"], [])
        self.assertIn("serial admission failed", report["primary_error"])

    def test_flow11_contract_and_named_provider_dispatch_cleanup(self):
        h = self.make()
        self.assertEqual(h.control.requests, api.REQUESTS)
        self.assertEqual(h.control.phases, api.PHASES)
        self.assertEqual(h.control.filename, api.CONTROL_NAME)
        self.assertIn("/11/rf-ccc-enable-reject/", h.control.uri("value-sent"))
        for flow, mode in (
            (10, api.RUNTIME_MODE),
            (11, "rf-write-reject"),
            (11, api.MODE),
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


if __name__ == "__main__":
    unittest.main()
