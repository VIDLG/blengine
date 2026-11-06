"""Synthetic verifier/coordinator tests only: no App, USB, native backend or RF."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.peripherals.nrf52840 import recovery_ten as physical
from tools.react_native import recovery_ten_ledger as api
from tools.tests.react_native.support.recovery_ledger import (
    SyntheticHarness,
    synthetic_events,
)


class RecoveryTenLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.reset("ios")

    def reset(
        self, platform: str, fault: str = "", *, require_local_name: bool = False
    ) -> None:
        suffix = "-required-name" if require_local_name else ""
        directory = self.root / (platform + "-" + (fault or "baseline") + suffix)
        directory.mkdir()
        self.harness = SyntheticHarness(directory, platform, fault, require_local_name)
        self.document = self.harness.run()
        self.lines = synthetic_events(self.harness)
        self.platform = platform
        self.plan = self.harness.plan
        self.path = self.plan.root / physical.PHYSICAL_NAME
        self.path.chmod(0o600)

    def verify(
        self,
        *,
        lines: list[str] | None = None,
        document: dict | None = None,
        **kwargs: object,
    ) -> dict:
        self.path.write_text(
            json.dumps(self.document if document is None else document),
            encoding="utf-8",
        )
        arguments = {
            "platform": self.platform,
            "run_id": self.plan.session_id,
            "service_uuid": self.plan.service_uuid,
            "physical_path": self.path,
            "fixture_request": self.plan.request_path,
            "fixture_identity": (os.getpid(), os.getpgid(0)),
            "host_cycles": tuple(range(10)),
            **kwargs,
        }
        return api.verify(
            "\n".join(self.lines if lines is None else lines) + "\n", **arguments
        )

    def rejected(self, **kwargs: object) -> None:
        with self.assertRaises((RuntimeError, ValueError, OSError)):
            self.verify(**kwargs)

    def mutate_event(
        self, predicate, mutate, *, lines: list[str] | None = None
    ) -> list[str]:
        result = list(self.lines if lines is None else lines)
        for index, line in enumerate(result):
            if not line.startswith(physical.PREFIX + "event:"):
                continue
            event = json.loads(line.removeprefix(physical.PREFIX + "event:"))
            if predicate(event):
                mutate(event)
                result[index] = physical.PREFIX + "event:" + json.dumps(event)
                return result
        self.fail("synthetic event mutation target not found")

    def test_synthetic_both_platforms_counts_read_only_and_warm_barriers(self) -> None:
        for platform in ("ios", "android"):
            if platform != self.platform:
                self.reset(platform)
            with self.subTest(platform=platform):
                self.verify()
                before = {
                    path: path.read_bytes()
                    for path in (
                        self.plan.request_path,
                        self.plan.profile_path,
                        self.plan.root / api.session.CONTRACT_NAME,
                        self.plan.receipt_path,
                        self.path,
                    )
                }
                result = api.verify(
                    "\n".join(self.lines) + "\n",
                    platform=platform,
                    run_id=self.plan.session_id,
                    service_uuid=self.plan.service_uuid,
                    physical_path=self.path,
                    fixture_request=self.plan.request_path,
                    fixture_identity=(os.getpid(), os.getpgid(0)),
                    host_cycles=tuple(range(10)),
                )
                self.assertEqual(before, {p: p.read_bytes() for p in before})
                self.assertEqual(json.loads(json.dumps(result)), result)
                self.assertEqual(result["counts"]["terminals"], 100)
                self.assertEqual(result["counts"]["resource_snapshots"], 50)
                self.assertEqual(
                    result["final_groups"],
                    {
                        "cycles": 10,
                        "connections": 20,
                        "disconnections": 20,
                        "closed_snapshots": 10,
                    },
                )
                self.assertTrue(
                    all(
                        value == 0
                        for value in result["closed_resource_totals"].values()
                    )
                )
                self.assertEqual(
                    result["cycles"][0]["resources"]["baseline"]["gattAttributes"],
                    12 if platform == "ios" else 0,
                )
                self.assertIn(
                    "not independently timed", result["explicit_limits"]["timing"]
                )
                self.assertIn("not OS memory", result["explicit_limits"]["resources"])

    def test_real_apple_passive_error_and_marker_agreement(self) -> None:
        self.reset("ios", "apple-passive-error")
        result = self.verify()
        self.assertEqual(result["final_groups"]["disconnections"], 20)
        passive = lambda event: (
            event["kind"] == "connectionStateChanged" and event.get("commandId") is None
        )
        event = next(
            json.loads(line.removeprefix(physical.PREFIX + "event:"))
            for line in self.lines
            if line.startswith(physical.PREFIX + "event:")
            and passive(json.loads(line.removeprefix(physical.PREFIX + "event:")))
        )
        self.assertNotIn("commandId", event)
        self.assertNotIn("connectionState", event)
        self.assertEqual(event["connectionId"], "10004")
        self.assertEqual(
            event["error"],
            {
                "category": "connection",
                "message": "CoreBluetooth connection ended unexpectedly",
                "platformDetails": {
                    "platform": "platform_apple",
                    "code": "7",
                    "name": "CBErrorDomain",
                    "message": "device disconnected",
                },
            },
        )
        for fields in (
            {"connectionState": "Disconnected"},
            {"commandId": "10005"},
            {"commandId": "19999"},
            {"connectionId": "10010"},
            {"gattSessionId": "10006"},
            {"subscriptionId": "10008"},
            {"error": {**event["error"], "category": "backend"}},
            {
                "error": {
                    **event["error"],
                    "platformDetails": {"platform": "platform_android"},
                }
            },
            {
                "error": {
                    **event["error"],
                    "platformDetails": {"platform": "platform_apple", "code": 7},
                }
            },
            {"error": {**event["error"], "message": "x" * 513}},
            {"error": {**event["error"], "extra": True}},
        ):
            with self.subTest(fields=fields):
                self.rejected(
                    lines=self.mutate_event(
                        passive, lambda e, fields=fields: e.update(fields)
                    )
                )
        self.rejected(
            lines=[
                line.replace("error=connection", "error=none") for line in self.lines
            ]
        )
        for state in ("Disconnected", None):
            lines = self.mutate_event(
                passive,
                lambda e, state=state: e.update(error=None, connectionState=state),
            )
            self.rejected(
                lines=lines
            )  # Success needs its own matching error=none marker.
        lines = list(self.lines)
        index = next(
            i
            for i, line in enumerate(lines)
            if line.startswith(physical.PREFIX + "event:")
            and passive(json.loads(line.removeprefix(physical.PREFIX + "event:")))
        )
        for operation in ("missing", "duplicate", "early", "late"):
            changed = list(lines)
            if operation == "missing":
                changed.pop(index)
            elif operation == "duplicate":
                changed.insert(index, changed[index])
            elif operation == "early":
                changed[index - 1], changed[index] = changed[index], changed[index - 1]
            else:
                changed[index], changed[index + 1] = changed[index + 1], changed[index]
            with self.subTest(operation=operation):
                self.rejected(lines=changed)

    def test_advertisement_rssi_is_optional_i16_not_a_foreign_route(self) -> None:
        for value in (None, -32768, 32767):
            with self.subTest(valid=value):
                self.verify(
                    lines=self.mutate_event(
                        lambda event: event["kind"] == "advertisementObserved",
                        lambda event, value=value: event.update(rssi=value),
                    )
                )
        for value in (True, -32769, 32768, -62.5, "-62"):
            with self.subTest(invalid=value):
                self.rejected(
                    lines=self.mutate_event(
                        lambda event: event["kind"] == "advertisementObserved",
                        lambda event, value=value: event.update(rssi=value),
                    )
                )

    def test_fd50_advertisement_without_name_rssi_and_zero_peripheral(self) -> None:
        self.assertEqual(self.plan.service_uuid, "fd500000-0000-4000-8000-000000000000")
        self.assertFalse(self.harness.fixture._profile.require_local_name)
        lines = []
        for line in self.lines:
            line = line.replace("peripheralId=88", "peripheralId=0")
            if line.startswith(physical.PREFIX + "event:"):
                event = json.loads(line.removeprefix(physical.PREFIX + "event:"))
                if event["kind"] == "advertisementObserved":
                    event.pop("localName")
                    event.pop("rssi")
                    event["peripheralId"] = "0"
                    line = physical.PREFIX + "event:" + json.dumps(event)
            lines.append(line)
        result = self.verify(lines=lines)
        self.assertEqual(result["final_groups"]["cycles"], 10)
        self.assertEqual(result["counts"]["resource_snapshots"], 50)
        self.rejected(
            lines=self.mutate_event(
                lambda event: event["kind"] == "advertisementObserved",
                lambda event: event.update(localName="OtherFixture"),
                lines=lines,
            )
        )

    def test_frozen_profile_requires_name_only_when_declared(self) -> None:
        advertisement = lambda event: event["kind"] == "advertisementObserved"
        for require in (False, True):
            if require:
                self.reset("ios", require_local_name=True)
            with self.subTest(require_local_name=require):
                self.verify()
                frozen = json.loads(self.plan.profile_path.read_text(encoding="utf-8"))
                self.assertIs(frozen["advertising"]["require_local_name"], require)
                self.assertIs(self.harness.fixture._profile.require_local_name, require)
                for missing in (True, False):
                    lines = self.mutate_event(
                        advertisement,
                        lambda event, missing=missing: (
                            event.pop("localName")
                            if missing
                            else event.update(localName=None)
                        ),
                    )
                    if require:
                        with self.assertRaisesRegex(
                            RuntimeError, "configured fixture local name"
                        ):
                            self.verify(lines=lines)
                    else:
                        self.verify(lines=lines)
                for wrong in ("OtherFixture", "", False, 0):
                    with (
                        self.subTest(wrong=wrong),
                        self.assertRaisesRegex(
                            RuntimeError, "configured fixture local name"
                        ),
                    ):
                        self.verify(
                            lines=self.mutate_event(
                                advertisement,
                                lambda event, wrong=wrong: event.update(
                                    localName=wrong
                                ),
                            )
                        )

    def test_android_budget_is_mandatory_exact_and_brackets_native_scan(self) -> None:
        self.reset("android")
        self.verify()
        for token in (
            "scan-budget-policy:",
            "cycle:0:scan-budget-wait:",
            "cycle:0:scan-budget-dispatch:",
            "cycle:0:scan-budget-started:",
        ):
            index = next(i for i, line in enumerate(self.lines) if token in line)
            for operation in ("missing", "duplicate", "extra-field"):
                with self.subTest(token=token, operation=operation):
                    lines = list(self.lines)
                    if operation == "missing":
                        lines.pop(index)
                    elif operation == "duplicate":
                        lines.insert(index, lines[index])
                    else:
                        lines[index] += ":extra=true"
                    self.rejected(lines=lines)
        index = next(
            i
            for i, line in enumerate(self.lines)
            if "cycle:0:scan-budget-started:" in line
        )
        lines = list(self.lines)
        # Move the complete JS acknowledgement ahead of the native start+terminal.
        acknowledgement = lines.pop(index)
        lines.insert(index - 2, acknowledgement)
        self.rejected(lines=lines)
        self.rejected(
            lines=[
                line.replace("monotonicMs=30000", "monotonicMs=29999", 1)
                for line in self.lines
            ]
        )

    def test_same_rpa_and_fast_logical_disconnect_are_valid_exact_pairs(self) -> None:
        for fault in ("same-address", "fast-logical-disconnect"):
            self.reset("ios", fault)
            result = self.verify()
            self.assertEqual(result["final_groups"]["disconnections"], 20)
            if fault == "same-address":
                self.assertEqual(
                    result["physical"]["cycles"][0]["old_peer"],
                    result["physical"]["cycles"][0]["recovery_peer"],
                )

    def test_adapter_commandless_optional_disconnecting_and_zero_peripheral(
        self,
    ) -> None:
        lines = self.mutate_event(
            lambda e: e["kind"] == "adapterSnapshotChanged",
            lambda e: e.update(commandId=None),
        )
        line = next(
            line
            for line in lines
            if '"commandId": "10012"' in line
            and '"connectionState": "Disconnected"' in line
        )
        value = json.loads(line.removeprefix(physical.PREFIX + "event:"))
        value["connectionState"] = "Disconnecting"
        index = lines.index(line)
        lines.insert(index, physical.PREFIX + "event:" + json.dumps(value))
        self.verify(lines=lines)
        self.rejected(lines=lines[:index] + [lines[index]] + lines[index:])
        zero = [
            line.replace("peripheralId=88", "peripheralId=0").replace(
                '"peripheralId": "88"', '"peripheralId": "0"'
            )
            for line in self.lines
        ]
        self.verify(lines=zero)
        for allocated in ("01", "-1", "18446744073709551616"):
            self.rejected(
                lines=[
                    line.replace(
                        "peripheralId=88", "peripheralId=" + allocated
                    ).replace(
                        '"peripheralId": "88"', '"peripheralId": "' + allocated + '"'
                    )
                    for line in self.lines
                ]
            )

    def test_resource_leak_phase_count_type_and_positive_control(self) -> None:
        for phase, field, value in (
            ("closed", "callbackRoutes", 1),
            ("closed", "hubRoots", 1),
            ("active", "gattAttributes", 0),
            ("baseline", "hubRoots", True),
            ("active", "subscriptions", 0),
            ("passive", "connections", 1),
        ):
            lines = list(self.lines)
            index = next(
                i
                for i, line in enumerate(lines)
                if f"resources:cycle=0:phase={phase}:" in line
            )
            prefix, body = lines[index].split(":snapshot=", 1)
            snapshot = json.loads(body)
            snapshot[field] = value
            lines[index] = prefix + ":snapshot=" + json.dumps(snapshot)
            with self.subTest(phase=phase, field=field):
                self.rejected(lines=lines)
        self.rejected(
            lines=[
                line.replace("phase=active:", "phase=baseline:") for line in self.lines
            ]
        )
        self.rejected(
            lines=[
                line
                for line in self.lines
                if "resources:cycle=4:phase=closed:" not in line
            ]
        )
        self.reset("android")
        self.rejected(
            lines=[
                line.replace('"gattAttributes": 0', '"gattAttributes": 12')
                if "phase=passive:" in line
                else line
                for line in self.lines
            ]
        )

    def test_cycles_runtime_unknown_duplicates_and_final_close(self) -> None:
        for lines in (
            [
                line
                for line in self.lines
                if "cycle:4:" not in line
                and "cycle=4:" not in line
                and '"cycle": 4' not in line
            ],
            self.lines + [self.lines[-1]],
            self.lines[:2] + [self.lines[1]] + self.lines[2:],
            [
                line
                for line in self.lines
                if "runtime:" not in line or "phase=close" not in line
            ],
            [
                line
                for line in self.lines
                if "close:passed:listener-removed" not in line
            ],
            [line.replace("cycle:9:passed", "cycle:10:passed") for line in self.lines],
            self.lines[:-1] + [physical.PREFIX + "native:unknown:{}", self.lines[-1]],
            self.lines[:-1]
            + [physical.PREFIX + "cycle:9:failed:synthetic", self.lines[-1]],
        ):
            self.rejected(lines=lines)
        passed = next(line for line in self.lines if "cycle:0:passed" in line)
        index = self.lines.index(passed)
        self.rejected(lines=self.lines[:index] + [passed] + self.lines[index:])
        event = next(
            line for line in self.lines if line.startswith(physical.PREFIX + "event:")
        )
        self.rejected(lines=self.lines[:index] + [event] + self.lines[index:])
        self.rejected(lines=["threadtime " + line for line in self.lines])

    def test_native_bootstrap_signals_identity_source_and_host_actions(self) -> None:
        for actions in (
            list(range(10)),
            tuple(range(9)),
            tuple(reversed(range(10))),
            (False, *range(1, 10)),
            (*range(9), 10),
        ):
            self.rejected(host_cycles=actions)
        for label, mutate in (
            ("native:bootstrap:", lambda r: r.update(source="synthetic-callback")),
            ("native:bootstrap:", lambda r: r.update(pid=True)),
            ("native:bootstrap:", lambda r: r.update(extra="field")),
            ("native:bootstrap:", lambda r: r.update(bundle_id="foreign.bundle")),
            ("native:signal:", lambda r: r.update(source="launch")),
            ("native:signal:", lambda r: r.update(pid=43)),
            ("native:signal:", lambda r: r.update(boot_id=self.plan.session_id)),
            ("native:signal:", lambda r: r.update(cycle=True)),
            ("native:signal:", lambda r: r.update(url="blehub-rn-spike://foreign")),
        ):
            lines = list(self.lines)
            index = next(
                i
                for i, line in enumerate(lines)
                if line.startswith(physical.PREFIX + label)
            )
            record = json.loads(lines[index].removeprefix(physical.PREFIX + label))
            mutate(record)
            lines[index] = physical.PREFIX + label + json.dumps(record)
            with self.subTest(label=label, record=record):
                self.rejected(lines=lines)
        lines = list(self.lines)
        signal = next(
            i
            for i, line in enumerate(lines)
            if line.startswith(physical.PREFIX + "native:signal:")
        )
        lines[signal], lines[signal + 1] = lines[signal + 1], lines[signal]
        self.rejected(lines=lines)
        document = copy.deepcopy(self.document)
        document["native_signals"]["0"]["executable"] = "/foreign"
        self.rejected(document=document)
        self.rejected(platform="android")

    def test_event_unknown_terminal_error_duplicate_domain_order_and_routes(
        self,
    ) -> None:
        target = lambda e: e["kind"] == "commandCompleted" and e["commandId"] == "10007"
        for fields in (
            {"kind": "nativeUnknown"},
            {"commandId": "9999"},
            {"commandId": "10006"},
            {"error": {"category": "gatt", "message": "synthetic"}},
            {"connectionId": "10104"},
            {"extra": 1},
        ):
            self.rejected(
                lines=self.mutate_event(
                    target, lambda e, fields=fields: e.update(fields)
                )
            )
        terminal = next(
            line
            for line in self.lines
            if '"kind": "commandCompleted"' in line and '"commandId": "10007"' in line
        )
        index = self.lines.index(terminal)
        self.rejected(lines=self.lines[:index] + [terminal] + self.lines[index:])
        lines = list(self.lines)
        lines[index - 1], lines[index] = lines[index], lines[index - 1]
        self.rejected(lines=lines)
        for kind, fields in (
            ("adapterSnapshotChanged", {"commandId": "10002"}),
            ("characteristicRead", {"value": [1]}),
            ("characteristicRead", {"value": [False]}),
            ("notificationReceived", {"value": [0]}),
            ("notificationStateChanged", {"actualNotificationMode": "Indication"}),
            ("notificationReceived", {"subscriptionId": "10108"}),
            ("gattDiscovered", {"gattSessionId": "10014"}),
        ):
            self.rejected(
                lines=self.mutate_event(
                    lambda e, kind=kind: e["kind"] == kind,
                    lambda e, fields=fields: e.update(fields),
                )
            )
        self.rejected(
            lines=self.mutate_event(
                lambda e: e["kind"] == "adapterSnapshotChanged",
                lambda e: e.update(nonce="foreign"),
            )
        )
        self.rejected(
            lines=self.mutate_event(
                lambda e: e["kind"] == "characteristicRead",
                lambda e: e.update(cycle=True),
            )
        )

    def test_full_gatt_projection_and_opaque_ids(self) -> None:
        def gatt(mutate):
            return self.mutate_event(
                lambda e: e["kind"] == "gattDiscovered",
                lambda e: mutate(e["gattServices"][0]),
            )

        for mutate in (
            lambda s: s.update(primary=1),
            lambda s: s.update(uuid="fd600000-0000-4000-8000-000000000000"),
            lambda s: s["characteristics"].pop(),
            lambda s: s["characteristics"][0].update(readable=False),
            lambda s: s["characteristics"][0].update(readable=1),
            lambda s: s["characteristics"][1]["descriptors"].clear(),
            lambda s: s["characteristics"][1]["descriptors"][0].update(
                uuid="00002901-0000-1000-8000-00805f9b34fb"
            ),
            lambda s: s["characteristics"][1]["descriptors"][0].update(id="0"),
            lambda s: s["characteristics"][0].update(id="00"),
        ):
            self.rejected(lines=gatt(mutate))
        self.verify(lines=gatt(lambda s: s.update(id="18446744073709551615")))
        self.rejected(
            lines=[
                line.replace("characteristicCount=5", "characteristicCount=4")
                for line in self.lines
            ]
        )
        self.rejected(
            lines=[
                line.replace(
                    "characteristicId=98765:actualMode",
                    "characteristicId=98764:actualMode",
                )
                for line in self.lines
            ]
        )

    def test_passive_exception_exact_platform_and_stale_route_silence(self) -> None:
        passive = lambda e: (
            e["kind"] == "connectionStateChanged" and e.get("commandId") is None
        )
        for fields in (
            {"connectionId": "10010"},
            {"connectionState": "Disconnecting"},
            {"commandId": "10005"},
            {"error": {"category": "connection", "message": "wrong iOS semantics"}},
        ):
            self.rejected(
                lines=self.mutate_event(
                    passive, lambda e, fields=fields: e.update(fields)
                )
            )
        notification = next(
            line for line in self.lines if '"kind": "notificationReceived"' in line
        )
        index = next(
            i
            for i, line in enumerate(self.lines)
            if "cycle:0:old-route-silence:" in line
        )
        self.rejected(lines=self.lines[:index] + [notification] + self.lines[index:])
        self.reset("android")
        for fields in (
            {"error": None},
            {"connectionState": "Disconnected"},
            {"error": {"category": "gatt", "message": "wrong category"}},
        ):
            self.rejected(
                lines=self.mutate_event(
                    passive, lambda e, fields=fields: e.update(fields)
                )
            )

    def test_physical_pair_wire_peer_fault_readiness_and_final_clear(self) -> None:
        for mutate in (
            lambda d: d["cycles"][0]["gap_events"][1]["peer"].__setitem__(
                0, "112233445566"
            ),
            lambda d: d["cycles"][0]["gap_events"][0].update(payload_hex="00"),
            lambda d: d["cycles"].pop(),
            lambda d: d["cycles"][0]["resources"]["closed"].update(listeners=1),
            lambda d: d["cycles"][0].update(readiness_ack_seconds=2.0),
            lambda d: d["cycles"][0].update(readiness_ack_seconds=float("nan")),
            lambda d: d["cycles"][0].update(remote_read_value_hex="01"),
            lambda d: d["cycles"][0]["cleanup"].update(peer_count=True),
            lambda d: d["cleanup"].update(advertising_stop_confirmed=False),
            lambda d: d["hold_status"].update(state="WAITING"),
            lambda d: d["hold_status"].update(state=[]),
            lambda d: d["boundaries"].pop(),
            lambda d: d["errors"].append("synthetic failure"),
            lambda d: d.update(primary_error=False),
            lambda d: d["final_groups"].update(closed_snapshots=True),
            lambda d: d["handles"].update(updates=True),
            lambda d: d["budgets_seconds"].update(whole=901.0),
        ):
            document = copy.deepcopy(self.document)
            mutate(document)
            self.rejected(document=document)
        for name in (
            "passive-fault-requested",
            "passive-observed",
            "physical-cycle-closed",
        ):
            document = copy.deepcopy(self.document)
            document["boundaries"] = [
                r
                for r in document["boundaries"]
                if not (r["cycle"] == 0 and r["boundary"] == name)
            ]
            document["cycles"][0]["boundaries"] = [
                r for r in document["cycles"][0]["boundaries"] if r["boundary"] != name
            ]
            self.rejected(document=document)

    def test_physical_absolute_budgets_and_raw_record_equality(self) -> None:
        for invalid in (True, float("inf"), -1):
            document = copy.deepcopy(self.document)
            document["boundaries"][0]["monotonic"] = invalid
            self.rejected(document=document)
        document = copy.deepcopy(self.document)
        document["boundaries"][-1]["monotonic"] += 900.0
        self.rejected(document=document)
        document = copy.deepcopy(self.document)
        anchor = next(
            r["monotonic"]
            for r in document["boundaries"]
            if r["cycle"] == 0 and r["boundary"] == "subscription-enabled"
        )
        for records in (
            document["boundaries"],
            *[row["boundaries"] for row in document["cycles"]],
            *[row["gap_events"] for row in document["cycles"]],
        ):
            for record in records:
                if record["monotonic"] >= anchor:
                    record["monotonic"] += 90.0
        with self.assertRaisesRegex(RuntimeError, "absolute physical phase budget"):
            self.verify(document=document)
        # Keep both raw copies identical so failure is the actual phase order,
        # not simply a mismatch of the redundant global/per-cycle records.
        document = copy.deepcopy(self.document)
        records = document["cycles"][0]["boundaries"]
        fault = next(r for r in records if r["boundary"] == "passive-fault-requested")
        gap = next(r for r in records if r["boundary"] == "gap-disconnected")
        instants = [r["monotonic"] for r in records]
        records.remove(fault)
        records.insert(records.index(gap) + 1, fault)
        for instant, record in zip(instants, records, strict=True):
            record["monotonic"] = instant
        document["cycles"][0]["gap_events"] = [
            {k: v for k, v in r.items() if k not in ("boundary", "cycle")}
            for r in records
            if r["boundary"].startswith("gap-")
        ]
        document["boundaries"] = [
            document["boundaries"][0],
            *records,
            *[r for r in document["boundaries"] if r["cycle"] > 0],
        ]
        self.rejected(document=document)
        document = copy.deepcopy(self.document)
        document["boundaries"].insert(
            -1,
            {
                "boundary": "gatt-reset",
                "cycle": 9,
                "monotonic": document["boundaries"][-1]["monotonic"],
            },
        )
        self.rejected(document=document)

    def test_current_receipt_hash_resource_uid_and_canonical_run(self) -> None:
        self.rejected(run_id="{" + self.plan.session_id + "}")
        self.rejected(fixture_identity=(True, os.getpgid(0)))
        self.rejected(fixture_identity=(os.getpid() + 1, os.getpgid(0)))
        document = copy.deepcopy(self.document)
        document["resource_identity"] = "jlink:654321"
        self.rejected(document=document)
        receipt = json.loads(self.plan.receipt_path.read_bytes())
        for fields in (
            {"fixture_pid": os.getpid() + 1},
            {"captured_at_utc": "2000-01-01T00:00:00+00:00"},
            {"resource_identity": "jlink:654321"},
            {
                "installed_identity": {
                    **receipt["installed_identity"],
                    "image_sha256": "00" * 32,
                }
            },
        ):
            self.plan.receipt_path.write_text(
                json.dumps({**receipt, **fields}), encoding="utf-8"
            )
            self.rejected()
        self.plan.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.verify()
        self.plan.profile_path.chmod(0o600)
        self.plan.profile_path.write_bytes(self.plan.profile_path.read_bytes() + b" ")
        self.rejected()
        self.plan.receipt_path.unlink()
        self.rejected()


if __name__ == "__main__":
    unittest.main()
