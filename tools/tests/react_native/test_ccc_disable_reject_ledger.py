"""Rehashed CCC-disable evidence cannot substitute for remote state or App delivery."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from tests.peripherals.nrf52840 import ccc_disable_reject as protocol
from tests.peripherals.nrf52840.cancellation_control import (
    read_document,
    write_document,
)
from tests.peripherals.nrf52840.fault_control import (
    ArmRequest,
    Opcode,
    Operation,
    RefCounters,
    StatusFlags,
    status_document,
    status_from_document,
)
from tests.peripherals.nrf52840.fixture_attestation import FixtureAttestationError
from tests.peripherals.nrf52840.fixture_session import FixtureSessionError
from tools.react_native import ccc_disable_reject_ledger as ledger
from tools.tests.react_native.support.ccc_disable_reject_ledger import (
    Evidence,
    edit_event,
    event_line,
    row,
)


class CccDisableRejectLedgerTests(unittest.TestCase):
    def make(self, platform="android", **options):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Evidence(Path(temporary.name), platform, **options)

    def verify(self, e, text=None):
        return ledger.verify(
            e.text if text is None else text,
            platform=e.platform,
            run_id=e.plan.session_id,
            service_uuid=e.plan.service_uuid,
            physical_path=e.root / protocol.PHYSICAL_NAME,
            fixture_request=e.plan.request_path,
            fixture_identity=(os.getpid(), os.getpgid(0)),
        )

    def reject(self, e, *, text=None, document=None):
        e.publish(text=text, document=document)
        with self.assertRaises((ValueError, FixtureSessionError)):
            self.verify(e, text)

    def test_portable_schema2_exact_api_both_platforms_and_delivery_races(self):
        self.assertIs(ledger.runtime_facts, protocol.runtime_facts)
        for platform in ("android", "ios"):
            for inline in (False, True):
                with self.subTest(platform=platform, inline=inline):
                    e = self.make(platform, inline=inline)
                    result = self.verify(e)
                    self.assertEqual(result["schema_version"], 2)
                    self.assertEqual(result["scenario"], "ccc-disable-reject")
                    self.assertEqual(result["rejected_command_id"], "34011")
                    self.assertEqual(result["subscription_id"], "34009")
                    self.assertEqual(result["native_terminal_count"], 13)
                    self.assertEqual(result["known_peers_remaining"], 0)
                    self.assertEqual(result["outcome"], "INJECTED_REJECT")
                    self.assertEqual(result["warm_resources"]["subscriptions"], 1)
                    self.assertEqual(
                        result["closed_resources"],
                        dict.fromkeys(protocol.RESOURCE_FIELDS, 0),
                    )
                    self.assertEqual(
                        [p["boundary"] for p in result["runtime_prefixes"]],
                        [*protocol.PHASES, "closed"],
                    )
                    self.assertEqual(
                        result["baseline_notification"],
                        {
                            "subscription_id": "34009",
                            "value": "ef",
                            "delivery": "app-observed",
                        },
                    )
                    self.assertEqual(
                        result["notification"],
                        {
                            "subscription_id": "34009",
                            "value": "e1",
                            "delivery": "app-observed",
                        },
                    )
                    self.assertEqual(json.loads(json.dumps(result)), result)

    def test_reject_requires_gatt14_no_disabled_and_same_original_subscription(self):
        e = self.make()
        error = json.loads(
            event_line(e.text, "commandCompleted", "34011").split(":event:", 1)[1]
        )["error"]
        candidates = [
            edit_event(e.text, "commandCompleted", "34011", error=None),
            edit_event(
                e.text,
                "commandCompleted",
                "34011",
                error={**error, "category": "timeout"},
            ),
            edit_event(
                e.text,
                "commandCompleted",
                "34011",
                error={
                    **error,
                    "platformDetails": {**error["platformDetails"], "code": "133"},
                },
            ),
            edit_event(
                e.text,
                "commandCompleted",
                "34011",
                error={
                    **error,
                    "platformDetails": {
                        "platform": "platform_apple",
                        "name": "CBATTErrorDomain",
                        "code": "14",
                    },
                },
            ),
            edit_event(
                e.text,
                "commandCompleted",
                "34011",
                kind="notificationStateChanged",
                error=None,
                notificationState="Disabled",
                subscriptionId="34009",
            ),
            edit_event(
                e.text,
                "notificationStateChanged",
                "34010",
                actualNotificationMode="Indication",
            ),
            edit_event(
                e.text, "notificationStateChanged", "34013", notificationState="Enabled"
            ),
            e.text.replace('"subscriptionId": "34009"', '"subscriptionId": "34099"'),
            e.text.replace('"pendingIo": 0', '"pendingIo": 1'),
            e.text.replace('"watchdogs": 0', '"watchdogs": 1'),
            e.text.replace('"subscriptions": 1', '"subscriptions": 0'),
            e.text.replace('"connections": 1', '"connections": 0'),
            e.text.replace('"handoffRoots": 0', '"handoffRoots": 1'),
        ]
        failed = event_line(e.text, "commandCompleted", "34011")
        fabricated = (
            protocol.PREFIX
            + protocol.RUNTIME_MODE
            + ":event:"
            + json.dumps(
                {
                    "runId": e.plan.session_id,
                    "nonce": "synthetic-ccc-boot",
                    "kind": "notificationStateChanged",
                    "commandId": "34011",
                    "connectionId": "34004",
                    "gattSessionId": "34007",
                    "characteristicId": "71",
                    "subscriptionId": "34009",
                    "notificationState": "Disabled",
                }
            )
        )
        candidates.append(e.text.replace(failed, fabricated + "\n" + failed))
        for candidate in candidates:
            with self.subTest(candidate=candidate[-100:]):
                self.reject(e, text=candidate)

    def test_actual_continued_e1_not_set_value_or_reenable_is_required(self):
        e = self.make()
        notifications = [
            line
            for line in e.text.splitlines()
            if '"kind": "notificationReceived"' in line
        ]
        self.assertEqual(len(notifications), 2)
        baseline, continued = notifications
        candidates = []
        for line in notifications:
            candidates.extend(
                [
                    e.text.replace(line + "\n", ""),
                    e.text.replace(line, line + "\n" + line),
                ]
            )
            for fields in (
                {"value": [240]},
                {"value": [True]},
                {"valueTruncated": True},
                {"valueByteLength": 2},
                {"subscriptionId": "34099"},
                {"connectionId": "34099"},
                {"gattSessionId": "34099"},
                {"characteristicId": "72"},
            ):
                event = json.loads(line.split(":event:", 1)[1]) | fields
                candidates.append(
                    e.text.replace(
                        line,
                        protocol.PREFIX
                        + protocol.RUNTIME_MODE
                        + ":event:"
                        + json.dumps(event),
                    )
                )
        candidates.append(
            e.text.replace(continued, continued.replace("[225]", "[239]"))
        )
        for line, phase in (
            (baseline, "baseline-enabled"),
            (continued, "reject-observed"),
        ):
            without = e.text.replace(line + "\n", "")
            marker = protocol.PREFIX + e.control.marker("ready", phase)
            candidates.append(without.replace(marker, line + "\n" + marker))
        without = e.text.replace(continued + "\n", "")
        anchor = protocol.PREFIX + protocol.RUNTIME_MODE + ":step:post-disable-read:"
        candidates.append(without.replace(anchor, continued + "\n" + anchor))
        enabled = event_line(e.text, "notificationStateChanged", "34010")
        candidates.append(e.text.replace(continued, enabled + "\n" + continued))
        for candidate in candidates:
            self.reject(e, text=candidate)
        for phase in ("baseline-value-sent", "continued-value-sent", "silence-started"):
            d = e.document()
            row(d, phase)["submission"]["delivery"] = "transmitted"
            self.reject(e, document=d)

    def test_every_enabled_read_is_successful_peer_bound_and_epoch_stable(self):
        e = self.make()
        for phase in (
            "baseline-value-sent",
            "armed",
            "rejected",
            "continued-value-sent",
            "retry-allowed",
            "silence-started",
            "close-allowed",
        ):
            for mutation in (
                "att-error",
                "wrong-value",
                "foreign-peer",
                "missing-live",
                "changed-epoch",
                "bool-epoch",
                "engine-busy",
                "stale-after",
            ):
                with self.subTest(phase=phase, mutation=mutation):
                    d = e.document()
                    local = row(d, phase)["local"]
                    if mutation == "att-error":
                        local["att_response"] = 12
                    elif mutation == "wrong-value":
                        local["value"] = "0000" if local["value"] == "0100" else "0100"
                    elif mutation == "foreign-peer":
                        local["peer"] = "01665544332212"
                    elif mutation == "missing-live":
                        del local["limits_before"]
                    elif mutation == "stale-after":
                        local["limits_after"]["database_epoch"] += 1
                    else:
                        for key in ("limits_before", "limits_after"):
                            field = (
                                "engine_flags"
                                if mutation == "engine-busy"
                                else "connection_epoch"
                            )
                            local[key][field] = True if mutation == "bool-epoch" else 7
                    self.reject(e, document=d)

    def test_arm_ticket_receipt_flags_six_counters_and_retention_are_semantic(self):
        e = self.make()
        for phase in (
            "armed",
            "held",
            "still-held",
            "rejected",
            "continued-value-sent",
            "retry-allowed",
            "silence-started",
            "close-allowed",
            "closed",
        ):
            for field in (
                "ticket",
                "peer",
                "before",
                "after",
                "observed",
                "callback_result",
                "offset",
                "normal_att_result",
                "derived_write_flags",
                "flags",
                "refs",
            ):
                d = e.document()
                status = status_from_document(row(d, phase)["status"])
                changes = {
                    "ticket": replace(
                        status.ticket, operation=Operation.RESPONSE_WRITE
                    ),
                    "peer": bytes.fromhex("01665544332212"),
                    "before": bytes(2),
                    "after": bytes(2),
                    "observed": b"\x02\x00",
                    "callback_result": 1,
                    "offset": 1,
                    "normal_att_result": 1,
                    "derived_write_flags": 1,
                    "flags": status.flags | StatusFlags.CALLBACK_INVOKED,
                    "refs": RefCounters(1, 0, 0, 0, 0, 0),
                }
                if (field == "refs" and phase in ("armed", "held", "still-held")) or (
                    field == "observed" and phase == "armed"
                ):
                    continue
                if field in ("offset", "derived_write_flags"):
                    wire = bytearray.fromhex(row(d, phase)["status"]["status_wire"])
                    wire[
                        2
                        + len(status.ticket.encode())
                        + (6 if field == "offset" else 5)
                    ] = 1
                    row(d, phase)["status"]["status_wire"] = wire.hex()
                else:
                    row(d, phase)["status"] = status_document(
                        replace(status, **{field: changes[field]})
                    )
                with self.subTest(phase=phase, field=field):
                    self.reject(e, document=d)
        for field in (
            "before",
            "requested",
            "peer",
            "database_epoch",
            "owner_nonce",
            "handle",
            "arm_budget_ms",
            "hold_budget_ms",
        ):
            d = e.document()
            request = ArmRequest.decode(
                Opcode.ARM_CCC, bytes.fromhex(row(d, "armed")["arm_request_wire"])
            )
            if field in ("before", "requested"):
                request = replace(request, **{field: b"\x02\x00"})
            else:
                value = (
                    bytes.fromhex("01665544332212")
                    if field == "peer"
                    else bytes([99]) * 16
                    if field == "owner_nonce"
                    else getattr(request.common, field) + 1
                )
                request = replace(
                    request, common=replace(request.common, **{field: value})
                )
            row(d, "armed")["arm_request_wire"] = request.encode().hex()
            with self.subTest(arm_field=field):
                self.reject(e, document=d)
        for field in vars(RefCounters(0, 0, 0, 0, 0, 0)):
            d = e.document()
            status = status_from_document(row(d, "rejected")["status"])
            row(d, "rejected")["status"] = status_document(
                replace(status, refs=replace(status.refs, **{field: 1}))
            )
            self.reject(e, document=d)

    def test_read_fences_app_host_time_and_original_deadlines_cannot_be_removed(self):
        e = self.make()
        for step in protocol.STEPS:
            line = next(
                line for line in e.text.splitlines() if f":step:{step}:" in line
            )
            self.reject(e, text=e.text.replace(line + "\n", ""))
        for command in protocol.READ_COMMANDS:
            line = event_line(e.text, "characteristicRead", command)
            self.reject(e, text=e.text.replace(line + "\n", ""))
            self.reject(
                e, text=edit_event(e.text, "characteristicRead", command, value=[1])
            )
        self.reject(e, text=e.text.replace("elapsedMs=1000", "elapsedMs=999"))
        for mutation in (
            "whole",
            "reject",
            "retirement",
            "hold-renewed",
            "short-host",
            "future-start",
            "time-reversed",
            "nan",
        ):
            d = e.document()
            if mutation == "whole":
                d["deadline"] = row(d, "closed")["monotonic"]
            elif mutation == "reject":
                row(d, "armed")["reject_deadline"] = row(d, "resolve-dispatched")[
                    "monotonic"
                ]
            elif mutation == "retirement":
                row(d, "rejected")["monotonic"] += 10
            elif mutation == "hold-renewed":
                held = status_from_document(row(d, "still-held")["status"])
                row(d, "still-held")["status"] = status_document(
                    replace(
                        held,
                        remaining=replace(held.remaining, hold_ms=protocol.HOLD_MS + 1),
                    )
                )
            elif mutation == "short-host":
                row(d, "silence-started")["silence_started"] = (
                    row(d, "close-allowed")["monotonic"] - 0.999
                )
            elif mutation == "future-start":
                row(d, "silence-started")["silence_started"] = (
                    row(d, "silence-started")["monotonic"] + 1
                )
            elif mutation == "time-reversed":
                row(d, "held")["monotonic"] = 0
            else:
                d["deadline"] = float("nan")
            with self.subTest(mutation=mutation):
                self.reject(e, document=d)

    def test_opaque_runtime_characteristic_zero_is_valid_but_att_handle_zero_is_not(
        self,
    ):
        e = self.make()
        for opaque, field in (
            ("71", "characteristicId"),
            ("72", "readCharacteristicId"),
        ):
            candidate = e.text.replace(f'"id": "{opaque}"', '"id": "0"')
            candidate = candidate.replace(
                f'"characteristicId": "{opaque}"', '"characteristicId": "0"'
            )
            candidate = candidate.replace(f":{field}={opaque}", f":{field}=0")
            if opaque == "72":
                candidate = candidate.replace(
                    ":characteristicId=72", ":characteristicId=0"
                )
            e.publish(candidate)
            with self.subTest(opaque=opaque):
                result = self.verify(e, candidate)
                self.assertIn(
                    "0",
                    (
                        result["target"]["characteristic_id"],
                        result["target"]["read_characteristic_id"],
                    ),
                )
        for name in ("target_handle", "value_handle", "read_handle"):
            d = e.document()
            d[name] = 0
            self.reject(e, document=d)
        for value in ("0", "1", "18446744073709551615"):
            self.assertTrue(protocol._id(value))
        for value in ("", "00", "01", "-1", "+0", "18446744073709551616", 0, False):
            self.assertFalse(protocol._id(value))

    def test_inventory_unique_uuid_interval_native_properties_and_routes(self):
        e = self.make()
        for mutation in (
            "descriptor",
            "outside-duplicate",
            "lookup",
            "service",
            "association",
            "mixed-proof",
            "permissions",
        ):
            d = e.document()
            if mutation == "descriptor":
                d["target_handle"] = d["value_handle"] + 1
            elif mutation == "outside-duplicate":
                d["attribute_inventory"].extend(
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
                d["uuid_handles"][protocol.UPDATES_UUID] = 52
            elif mutation == "service":
                d["service_binding"]["uuid"] = protocol.READ_UUID
            elif mutation == "association":
                d["declaration_associations"]["36"] = 34
            elif mutation == "mixed-proof":
                d["declarations"] = {}
            else:
                next(a for a in d["attribute_inventory"] if a["handle"] == 45)[
                    "permissions"
                ] = 0x43
            self.reject(e, document=d)
        discovery = json.loads(
            event_line(e.text, "gattDiscovered", "34008").split(":event:", 1)[1]
        )
        for mutation in (
            "service",
            "primary",
            "id",
            "ccc",
            "notifiable",
            "indicatable",
            "write-property",
        ):
            services = json.loads(json.dumps(discovery["gattServices"]))
            target = services[0]["characteristics"][0]
            if mutation == "service":
                services[0]["uuid"] = protocol.READ_UUID
            elif mutation == "primary":
                services[0]["primary"] = False
            elif mutation == "id":
                target["id"] = "999"
            elif mutation == "ccc":
                target["descriptors"] = []
            elif mutation == "write-property":
                services[0]["characteristics"][1]["writableWithoutResponse"] = False
            else:
                target[mutation] = False
            self.reject(
                e,
                text=edit_event(
                    e.text, "gattDiscovered", "34008", gattServices=services
                ),
            )

    def test_prefix_control_close_peer_and_frozen_identity_are_not_sidecar_verdicts(
        self,
    ):
        e = self.make()
        for phase in (*protocol.PHASES, "closed"):
            d = e.document()
            r = row(d, phase)
            if phase == "closed":
                size = len(e.text.encode()) - 1
            else:
                ack = protocol.PREFIX + e.control.marker("observed", phase) + "\n"
                size = len(e.text[: e.text.index(ack) + len(ack)].encode())
            r["runtime_bytes"] = size
            r["runtime_sha256"] = hashlib.sha256(e.text.encode()[:size]).hexdigest()
            write_document(e.root / protocol.PHYSICAL_NAME, d)
            with self.subTest(prefix=phase), self.assertRaises(ValueError):
                self.verify(e)
        for field, value in (
            ("schema_version", True),
            ("fixture_pid", True),
            ("contract_sha256", "00" * 32),
            ("profile_sha256", "00" * 32),
            ("resource_identity", "jlink:other"),
            ("status", "failed"),
            ("cleanup_errors", ["failed"]),
        ):
            d = e.document()
            d[field] = value
            self.reject(e, document=d)
        for mutation in (
            "missing-gap",
            "wrong-peer",
            "peer-present",
            "bool-count",
            "busy",
            "resource",
        ):
            d = e.document()
            if mutation == "missing-gap":
                d["boundaries"].pop(-2)
            elif mutation == "wrong-peer":
                d["boundaries"][-2]["peer"] = "01665544332212"
            elif mutation == "peer-present":
                row(d, "closed")["limits"]["peer_present"] = True
            elif mutation == "bool-count":
                row(d, "closed")["peer_count"] = False
            elif mutation == "busy":
                row(d, "closed")["limits"]["engine_flags"] = 1
            else:
                row(d, "closed")["resources"]["callbackRoutes"] = 1
            self.reject(e, document=d)
        e.publish()
        for name in (
            protocol.CONTROL_NAME,
            "request-reject-observed.json",
            "signal-continued-value-sent.json",
        ):
            path = e.root / name
            original = read_document(path)
            write_document(path, asdict(replace(e.control, token=e.control.session)))
            with self.subTest(control=name), self.assertRaises(ValueError):
                self.verify(e)
            write_document(path, original)
        receipt = read_document(e.plan.receipt_path)
        receipt["installed_identity"]["image_sha256"] = "00" * 32
        write_document(e.plan.receipt_path, receipt)
        with self.assertRaises(FixtureAttestationError):
            self.verify(e)
