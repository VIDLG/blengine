"""Rehashed native/physical mutations must not manufacture a CCC RF pass."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.peripherals.nrf52840 import ccc_enable_reject as protocol
from tests.peripherals.nrf52840.cancellation_control import (
    read_document,
    write_document,
)
from tests.peripherals.nrf52840.fault_control import (
    ArmRequest,
    Opcode,
    RefCounters,
    StatusFlags,
    status_document,
    status_from_document,
)
from tests.peripherals.nrf52840.fixture_session import FixtureSessionError
from tools.react_native import ccc_enable_reject_ledger as ledger
from tools.react_native import write_reject_ledger
from tools.tests.react_native.support.ccc_enable_reject_ledger import (
    Evidence,
    edit_event,
    event_line,
    row,
)


class CccEnableRejectLedgerTests(unittest.TestCase):
    def make(self, platform="android", **options):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Evidence(Path(temporary.name), platform, **options)

    def verify(self, evidence, text=None):
        return ledger.verify(
            evidence.text if text is None else text,
            platform=evidence.platform,
            run_id=evidence.plan.session_id,
            service_uuid=evidence.plan.service_uuid,
            physical_path=evidence.root / protocol.PHYSICAL_NAME,
            fixture_request=evidence.plan.request_path,
            fixture_identity=(os.getpid(), os.getpgid(0)),
        )

    def test_original_production_writer_replays_both_platforms_and_value_races(self):
        self.assertEqual(
            inspect.signature(ledger.verify),
            inspect.signature(write_reject_ledger.verify),
        )
        self.assertIs(ledger.runtime_facts, protocol.runtime_facts)
        for platform in ("android", "ios"):
            for inline in (False, True):
                with self.subTest(platform=platform, inline=inline):
                    evidence = self.make(platform, inline=inline)
                    result = self.verify(evidence)
                    self.assertEqual(result["schema_version"], 2)
                    self.assertEqual(result["scenario"], "ccc-enable-reject")
                    self.assertEqual(result["outcome"], "INJECTED_REJECT")
                    self.assertEqual(result["native_terminal_count"], 13)
                    self.assertEqual(result["known_peers_remaining"], 0)
                    self.assertEqual(result["rejected_command_id"], "33010")
                    self.assertEqual(result["warm_resources"]["connections"], 1)
                    self.assertEqual(result["warm_resources"]["subscriptions"], 0)
                    self.assertEqual(
                        result["closed_resources"],
                        dict.fromkeys(protocol.RESOURCE_FIELDS, 0),
                    )
                    self.assertEqual(
                        [p["boundary"] for p in result["runtime_prefixes"]],
                        [*protocol.PHASES, "closed"],
                    )
                    self.assertEqual(
                        result["notification"],
                        {
                            "subscription_id": "33012",
                            "value": "ef",
                            "delivery": "app-observed",
                        },
                    )
                    self.assertEqual(json.loads(json.dumps(result)), result)

    def test_rehashed_native_errors_domains_subscriptions_values_and_nonzero_fail(self):
        evidence = self.make()
        text = evidence.text
        rejected = json.loads(
            event_line(text, "commandCompleted", "33010").split(":event:", 1)[1]
        )["error"]
        mutations = [
            edit_event(
                text,
                "commandCompleted",
                "33010",
                error={**rejected, "category": "timeout"},
            ),
            edit_event(
                text,
                "commandCompleted",
                "33010",
                error={
                    **rejected,
                    "platformDetails": {**rejected["platformDetails"], "code": "133"},
                },
            ),
            edit_event(
                text,
                "commandCompleted",
                "33010",
                error={
                    **rejected,
                    "platformDetails": {
                        **rejected["platformDetails"],
                        "platform": "platform_apple",
                        "name": "CBATTErrorDomain",
                    },
                },
            ),
            edit_event(
                text,
                "commandCompleted",
                "33010",
                kind="notificationStateChanged",
                error=None,
                subscriptionId="33009",
                notificationState="Enabled",
            ),
            edit_event(text, "notificationReceived", subscriptionId="33009"),
            edit_event(text, "notificationReceived", value=[240]),
            edit_event(text, "notificationReceived", characteristicId="72"),
            edit_event(text, "notificationReceived", connectionId="33099"),
            edit_event(text, "notificationReceived", value=[239], valueTruncated=True),
            edit_event(text, "characteristicRead", "33011", value=[17]),
            edit_event(text, "characteristicRead", "33015", value=[False]),
            edit_event(text, "characteristicRead", "33016", characteristicId="71"),
            edit_event(
                text,
                "notificationStateChanged",
                "33013",
                actualNotificationMode="Indication",
            ),
            text.replace('"pendingIo": 0', '"pendingIo": 1'),
            text.replace('"handoffRoots": 0', '"handoffRoots": 1'),
        ]
        for candidate in mutations:
            with self.subTest(candidate=candidate[-80:]):
                evidence.publish(candidate)
                with self.assertRaises(ValueError):
                    self.verify(evidence, candidate)

    def test_missing_or_reordered_read_fences_silence_and_delivery_never_pass(self):
        evidence = self.make()
        text = evidence.text
        notification = event_line(text, "notificationReceived")
        candidates = [
            text.replace(notification + "\n", ""),
            text.replace(":elapsedMs=1000", ":elapsedMs=999"),
        ]
        for step in (
            "baseline-read",
            "queued-read-dispatched",
            "queued-read",
            "post-disable-read",
            "silence",
            "end-silence-read",
        ):
            line = next(line for line in text.splitlines() if f":step:{step}:" in line)
            candidates.append(text.replace(line + "\n", ""))
        for command in ("33011", "33015", "33016", "33017"):
            line = event_line(text, "characteristicRead", command)
            candidates.append(text.replace(line + "\n", ""))
        for kind, command in (
            ("notificationReceived", None),
            ("notificationStateChanged", "33013"),
            ("commandCompleted", "33010"),
        ):
            line = event_line(text, kind, command)
            candidates.append(text.replace(line, line + "\n" + line))
        without = text.replace(notification + "\n", "")
        for anchor in (
            protocol.PREFIX + evidence.control.marker("ready", "recovery-enabled"),
            protocol.PREFIX + protocol.RUNTIME_MODE + ":step:post-disable-read:",
        ):
            position = without.index(anchor)
            candidates.append(
                without[:position] + notification + "\n" + without[position:]
            )
        for candidate in candidates:
            with self.subTest(candidate=candidate[-80:]):
                evidence.publish(candidate)
                with self.assertRaises(ValueError):
                    self.verify(evidence, candidate)
        document = evidence.document()
        row(document, "value-sent")["submission"]["delivery"] = "transmitted"
        evidence.publish(document=document)
        with self.assertRaises(ValueError):
            self.verify(evidence)

    def test_wrong_descriptor_declarations_app_projection_and_local_peer_fail(self):
        evidence = self.make()
        for mutation in (
            "descriptor",
            "sibling",
            "declaration",
            "permission",
            "missing-peer",
            "foreign-peer",
            "missing-live",
            "foreign-epoch",
            "bool-epoch",
        ):
            with self.subTest(mutation=mutation):
                document = evidence.document()
                if mutation == "descriptor":
                    document["target_handle"] = document["value_handle"] + 1
                elif mutation == "sibling":
                    document["target_handle"] = 54
                elif mutation == "declaration":
                    document["declaration_associations"]["36"] = 34
                elif mutation == "permission":
                    next(a for a in document["topology"] if a["handle"] == 45)[
                        "permissions"
                    ] = 0x43
                else:
                    local = row(document, "rejected")["local"]
                    if mutation == "missing-peer":
                        del local["peer"]
                    elif mutation == "foreign-peer":
                        local["peer"] = "01665544332212"
                    elif mutation == "missing-live":
                        del local["limits_before"]
                    else:
                        for name in ("limits_before", "limits_after"):
                            local[name]["connection_epoch"] = (
                                True if mutation == "bool-epoch" else 2
                            )
                evidence.publish(document=document)
                with self.assertRaises(ValueError):
                    self.verify(evidence)
        discovery = json.loads(
            event_line(evidence.text, "gattDiscovered", "33008").split(":event:", 1)[1]
        )
        for mutation in ("uuid", "missing-ccc", "foreign-id", "not-indicatable"):
            event = json.loads(json.dumps(discovery))
            target = event["gattServices"][0]["characteristics"][0]
            if mutation == "uuid":
                target["uuid"] = protocol.READ_UUID
            elif mutation == "missing-ccc":
                target["descriptors"] = []
            elif mutation == "foreign-id":
                target["id"] = "999"
            else:
                target["indicatable"] = False
            candidate = edit_event(
                evidence.text,
                "gattDiscovered",
                "33008",
                gattServices=event["gattServices"],
            )
            evidence.publish(candidate)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.verify(evidence, candidate)

    def test_metadata_topology_requires_global_uniqueness_and_native_service_properties(
        self,
    ):
        evidence = self.make()
        for mutation in (
            "outside-duplicate",
            "lookup",
            "service-boundary",
            "unknown-method",
            "missing-method",
            "mixed-raw",
            "extra-association",
        ):
            document = evidence.document()
            if mutation == "outside-duplicate":
                document["attribute_inventory"].extend(
                    [
                        {"handle": 60, "permissions": 1, "uuid": "2800"},
                        {"handle": 61, "permissions": 1, "uuid": "2803"},
                        {
                            "handle": 64,
                            "permissions": 0x41,
                            "uuid": protocol.UPDATES_UUID,
                        },
                    ]
                )
            elif mutation == "lookup":
                document["uuid_handles"][protocol.UPDATES_UUID] = 52
            elif mutation == "service-boundary":
                document["service_binding"]["handle"] = 35
            elif mutation == "unknown-method":
                document["topology_method"] = "guess-from-profile"
            elif mutation == "missing-method":
                del document["topology_method"]
            elif mutation == "mixed-raw":
                document["declarations"] = {
                    "30": protocol.uuid_to_le_bytes(evidence.plan.service_uuid).hex()
                }
            else:
                document["declaration_associations"]["99"] = 40
            evidence.publish(document=document)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.verify(evidence)
        original = json.loads(
            event_line(evidence.text, "gattDiscovered", "33008").split(":event:", 1)[1]
        )
        for mutation in ("service", "properties", "not-primary"):
            services = json.loads(json.dumps(original["gattServices"]))
            if mutation == "service":
                services[0]["uuid"] = "fd700000-0000-4000-8000-000000000001"
            elif mutation == "properties":
                services[0]["characteristics"][1]["writableWithoutResponse"] = False
            else:
                services[0]["primary"] = False
            text = edit_event(
                evidence.text, "gattDiscovered", "33008", gattServices=services
            )
            evidence.publish(text)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.verify(evidence, text)

    def test_legacy_raw_proof_replay_is_not_a_live_declaration_read_fallback(self):
        evidence = self.make()
        document = evidence.document()
        # Test-only archived schema sample from the emulator's stored declarations.
        # The live resolver never reads or synthesizes these declaration bytes.
        legacy = protocol._resolve_topology(
            tuple(protocol.GattAttribute(**entry) for entry in document["topology"]),
            {
                str(handle): value
                for handle, value in evidence.phone.wire.declarations.items()
            },
            evidence.phone.handles,
            evidence.plan.service_uuid,
        )
        for key in (
            "topology_method",
            "attribute_inventory",
            "uuid_handles",
            "service_binding",
            "declaration_associations",
        ):
            del document[key]
        document.update(legacy)
        evidence.publish(document=document)
        self.verify(evidence)
        self.assertEqual(evidence.phone.wire.declaration_reads, [])
        document["declarations"]["36"] = (
            "323400" + protocol.uuid_to_le_bytes(protocol.UPDATES_UUID).hex()
        )
        evidence.publish(document=document)
        with self.assertRaises(ValueError):
            self.verify(evidence)

    def test_arm_request_retained_refs_callback_commit_and_peer_epoch_binding(self):
        evidence = self.make()
        for mutation in (
            "requested",
            "before",
            "peer",
            "epoch",
            "arm-budget",
            "hold-budget",
            "refs",
            "callback",
            "commit",
            "held-renewed",
            "retained",
        ):
            with self.subTest(mutation=mutation):
                document = evidence.document()
                armed = row(document, "armed")
                if mutation in (
                    "requested",
                    "before",
                    "peer",
                    "epoch",
                    "arm-budget",
                    "hold-budget",
                ):
                    request = ArmRequest.decode(
                        Opcode.ARM_CCC, bytes.fromhex(armed["arm_request_wire"])
                    )
                    if mutation == "requested":
                        request = replace(request, requested=b"\x02\x00")
                    elif mutation == "before":
                        request = replace(request, before=b"\x01\x00")
                    else:
                        common = request.common
                        fields = {
                            "peer": {"peer": bytes.fromhex("01665544332212")},
                            "epoch": {"connection_epoch": common.connection_epoch + 1},
                            "arm-budget": {"arm_budget_ms": 10000},
                            "hold-budget": {"hold_budget_ms": 20000},
                        }[mutation]
                        request = replace(request, common=replace(common, **fields))
                    armed["arm_request_wire"] = request.encode().hex()
                else:
                    name = (
                        "still-held"
                        if mutation == "held-renewed"
                        else "close-allowed"
                        if mutation == "retained"
                        else "rejected"
                    )
                    target = row(document, name)
                    status = status_from_document(target["status"])
                    if mutation == "refs":
                        status = replace(status, refs=RefCounters(1, 0, 0, 0, 0, 0))
                    elif mutation in ("callback", "commit"):
                        status = replace(
                            status,
                            flags=status.flags
                            | (
                                StatusFlags.CALLBACK_INVOKED
                                if mutation == "callback"
                                else StatusFlags.COMMIT_OBSERVED
                            ),
                        )
                    elif mutation == "held-renewed":
                        status = replace(
                            status, remaining=replace(status.remaining, hold_ms=10000)
                        )
                    else:
                        status = replace(status, after=b"\x01\x00")
                    target["status"] = status_document(status)
                evidence.publish(document=document)
                with self.assertRaises(ValueError):
                    self.verify(evidence)

    def test_physical_time_limits_gap_submissions_and_resources_cannot_be_relabelled(
        self,
    ):
        evidence = self.make()
        for mutation in (
            "whole",
            "reject",
            "submitted",
            "retirement",
            "silence",
            "nan",
            "order",
            "extra-gap",
            "gap-peer",
            "initial-busy",
            "initial-peer",
            "closed-peer",
            "counter-bool",
            "resources",
            "ef",
            "f0",
            "decision",
        ):
            with self.subTest(mutation=mutation):
                document = evidence.document()
                rows = document["boundaries"]
                if mutation == "whole":
                    document["deadline"] += 1000
                elif mutation == "reject":
                    row(document, "armed")["reject_deadline"] = row(document, "armed")[
                        "monotonic"
                    ]
                elif mutation == "submitted":
                    row(document, "resolve-dispatched")["submitted_at"] = (
                        row(document, "held")["monotonic"] - 1
                    )
                elif mutation == "retirement":
                    row(document, "rejected")["monotonic"] += 10
                elif mutation == "silence":
                    row(document, "silence-started")["silence_started"] = row(
                        document, "close-allowed"
                    )["monotonic"]
                elif mutation == "nan":
                    row(document, "held")["monotonic"] = float("nan")
                elif mutation == "order":
                    rows[2], rows[3] = rows[3], rows[2]
                elif mutation == "extra-gap":
                    rows.append(dict(rows[1]))
                elif mutation == "gap-peer":
                    rows[1]["peer"] = "01665544332212"
                elif mutation in ("initial-busy", "initial-peer"):
                    row(document, "no-peer")["limits"][
                        "engine_flags" if mutation == "initial-busy" else "peer_present"
                    ] = 4 if mutation == "initial-busy" else True
                elif mutation == "closed-peer":
                    row(document, "closed")["limits"]["peer_present"] = True
                elif mutation == "counter-bool":
                    row(document, "closed")["gap_attached"] = True
                elif mutation == "resources":
                    row(document, "closed")["resources"]["lifecycleOwners"] = 1
                elif mutation in ("ef", "f0"):
                    row(
                        document,
                        "value-sent" if mutation == "ef" else "silence-started",
                    )["submission"]["value"] = "00"
                else:
                    row(document, "resolve-dispatched")["decision"] = 1
                evidence.publish(document=document)
                with self.assertRaises(ValueError):
                    self.verify(evidence)

    def test_prefix_hashes_bind_utf8_bytes_and_cannot_slide_across_readiness(self):
        evidence = self.make()
        candidate = protocol.PREFIX + "rf-host:diagnostic:label=温度\n" + evidence.text
        evidence.publish(candidate)
        result = self.verify(evidence, candidate)
        self.assertEqual(
            result["runtime_sha256"],
            hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
        )
        for mutation in (
            "hash",
            "past-ack",
            "before-ready",
            "reversed",
            "middle-line",
            "closed-native-tail",
            "character-count",
        ):
            with self.subTest(mutation=mutation):
                document = evidence.publish(candidate)
                target = row(document, "value-sent")
                if mutation == "hash":
                    target["runtime_sha256"] = "0" * 64
                elif mutation == "past-ack":
                    marker = (
                        protocol.PREFIX
                        + evidence.control.marker("observed", "value-sent")
                        + "\n"
                    )
                    target["runtime_bytes"] = len(
                        candidate[: candidate.index(marker) + len(marker)].encode(
                            "utf-8"
                        )
                    )
                elif mutation == "before-ready":
                    marker = protocol.PREFIX + evidence.control.marker(
                        "ready", "recovery-enabled"
                    )
                    target["runtime_bytes"] = len(
                        candidate[: candidate.index(marker)].encode("utf-8")
                    )
                elif mutation == "reversed":
                    target["runtime_bytes"] = row(document, "rejected")["runtime_bytes"]
                elif mutation == "middle-line":
                    target["runtime_bytes"] -= 1
                elif mutation == "closed-native-tail":
                    row(document, "closed")["runtime_bytes"] = row(
                        document, "close-allowed"
                    )["runtime_bytes"]
                    target = row(document, "closed")
                else:
                    target["runtime_bytes"] = len(
                        candidate.encode("utf-8")[: target["runtime_bytes"]].decode(
                            "utf-8"
                        )
                    )
                if mutation != "hash":
                    target["runtime_sha256"] = hashlib.sha256(
                        candidate.encode("utf-8")[: target["runtime_bytes"]]
                    ).hexdigest()
                write_document(evidence.root / protocol.PHYSICAL_NAME, document)
                with self.assertRaises(ValueError):
                    self.verify(evidence, candidate)

    def test_control_identity_files_prefix_token_and_process_must_remain_original(self):
        evidence = self.make()
        for name in (
            "request-reject.json",
            "signal-value-sent.json",
            protocol.CONTROL_NAME,
        ):
            path = evidence.root / name
            original = read_document(path)
            write_document(path, {**original, "token": evidence.plan.session_id})
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.verify(evidence)
            write_document(path, original)
        candidate = evidence.text.replace(
            evidence.control.token, evidence.plan.session_id
        )
        evidence.publish(candidate)
        with self.assertRaises(ValueError):
            self.verify(evidence, candidate)
        document = evidence.document()
        document["fixture_pid"] += 1
        evidence.publish(document=document)
        with self.assertRaises(ValueError):
            self.verify(evidence)
        evidence.publish()
        original_read = ledger.read_document

        def drifting(path):
            value = original_read(path)
            if path.name == protocol.PHYSICAL_NAME:
                value = dict(value)
                value["fixture_pid"] += drifting.calls
                drifting.calls += 1
            return value

        drifting.calls = 0
        with (
            patch.object(ledger, "read_document", side_effect=drifting),
            self.assertRaises(ValueError),
        ):
            self.verify(evidence)

    def test_frozen_profile_receipt_and_failed_report_are_not_reauthorized(self):
        evidence = self.make()
        for field, value in (
            ("schema_version", 1),
            ("status", "running"),
            ("primary_error", "retirement failed"),
            ("cleanup_errors", ["lease lost"]),
            ("service_uuid", "fd700000-0000-4000-8000-000000000000"),
            ("profile_sha256", "00" * 32),
            ("resource_identity", "jlink:foreign"),
        ):
            document = evidence.document()
            document[field] = value
            evidence.publish(document=document)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(evidence)
        evidence.publish()
        profile = evidence.plan.profile_path
        original = profile.read_bytes()
        profile.chmod(0o600)
        try:
            edited = json.loads(original)
            edited["services"][0]["characteristics"][1]["properties"] = [
                "read",
                "indicate",
            ]
            profile.write_text(json.dumps(edited), encoding="utf-8")
            with self.assertRaises(FixtureSessionError):
                self.verify(evidence)
        finally:
            profile.write_bytes(original)
        receipt = evidence.plan.receipt_path
        original_receipt = read_document(receipt)
        write_document(receipt, {**original_receipt, "fixture_pid": os.getpid() + 1})
        with self.assertRaises(FixtureSessionError):
            self.verify(evidence)
        write_document(receipt, original_receipt)
        self.verify(evidence)

    def test_optional_disconnecting_and_trailing_host_receipts_preserve_native_proof(
        self,
    ):
        evidence = self.make()
        disconnected = event_line(evidence.text, "connectionStateChanged", "33006")
        disconnecting = disconnected.replace('"Disconnected"', '"Disconnecting"')
        candidate = evidence.text.replace(
            disconnected, disconnecting + "\n" + disconnected
        )
        evidence.publish(candidate)
        self.verify(evidence, candidate)
        candidate += protocol.PREFIX + "rf-host:diagnostic:phase=offline-tail\n"
        self.verify(evidence, candidate)
        for replacement in (
            disconnecting,
            disconnected + "\n" + disconnecting,
            disconnecting + "\n" + disconnecting + "\n" + disconnected,
        ):
            candidate = evidence.text.replace(disconnected, replacement)
            evidence.publish(candidate)
            with self.assertRaises(ValueError):
                self.verify(evidence, candidate)


if __name__ == "__main__":
    unittest.main()
