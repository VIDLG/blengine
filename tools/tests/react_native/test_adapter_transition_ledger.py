"""Synthetic protocol mutation tests, never RF/physical adapter evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest import mock

from tests.support.rf_lab.markers import SERVICE
from tools.react_native import adapter_transition_ledger as ledger
from tools.react_native.rf_evidence_bundle import RAW_PREFIX, RECOVERY_RESOURCE_FIELDS
from tools.tests.react_native.support.adapter_transition_samples import (
    BOOT,
    BOOTSTRAP,
    DEVICE,
    LOG,
    NONCE,
    OFF_EVENTS,
    RUN,
    active_bodies,
    active_gatt_event,
    active_physical_sidecars,
    bodies,
    physical_sidecars,
    readiness_sidecar,
    state_control,
    transcript,
)


class ActiveLossLedgerTests(unittest.TestCase):
    def verify(self, markers: list[str] | None = None) -> dict:
        return ledger.verify_runtime(
            transcript(active_bodies() if markers is None else markers),
            RUN,
            SERVICE,
            LOG,
        )

    def test_two_policy_transcripts_and_two_gap_pairs(self) -> None:
        for policy in ("disconnect-first", "read-error-first"):
            with self.subTest(policy=policy):
                runtime = self.verify(active_bodies(policy))
                self.assertEqual(runtime["adapter_mode"], "active-loss")
                self.assertEqual(runtime["active_loss"]["policy"], policy)
                request, receipt, physical = active_physical_sidecars(runtime)
                ledger.verify_physical(
                    physical,
                    request=request,
                    receipt=receipt,
                    receipt_sha256=physical["receipt_sha256"],
                    runtime=runtime,
                )
                index = ledger.index_runtime(transcript(active_bodies(policy)), LOG)
                self.assertEqual(index["adapter_mode"], "active-loss")

    def test_large_production_topology_uses_explicit_bounded_canonical_audit(
        self,
    ) -> None:
        original = active_gatt_event(compact=False)
        raw = json.dumps(original, separators=(",", ":"))
        self.assertGreater(len(raw.encode("utf-8")), 4096)
        compact = active_gatt_event()
        self.assertEqual(compact["auditSchema"], "active-gatt-canonical-v1")
        self.assertEqual(
            compact["gattSummary"],
            {"serviceCount": 4, "characteristicCount": 21, "descriptorCount": 3},
        )
        self.assertEqual(compact["gattServices"], original["gattServices"][:1])
        self.assertLessEqual(
            len(
                (ledger.PREFIX + "active:event:" + json.dumps(compact)).encode("utf-8")
            ),
            3500,
        )
        runtime = self.verify()
        audit = next(
            e for e in runtime["active_loss"]["events"] if e["kind"] == "gattDiscovered"
        )
        self.assertEqual(audit, compact)
        self.assertEqual(
            runtime["active_loss"]["route"]["characteristicId"],
            audit["gattServices"][0]["characteristics"][0]["id"],
        )
        markers = active_bodies()
        index = next(
            i for i, m in enumerate(markers) if '"kind": "gattDiscovered"' in m
        )
        # Retain historical, complete native-form evidence support; never repair a cut record.
        markers[index] = "active:event:" + raw
        self.verify(markers)
        cut = (ledger.PREFIX + markers[index]).encode("utf-8")[:4052].decode("utf-8")
        markers[index] = cut.removeprefix(ledger.PREFIX)
        with self.assertRaises(ledger.EvidenceError):
            self.verify(markers)

    def test_compact_schema_cannot_drop_checker_topology_or_lie_about_scope(
        self,
    ) -> None:
        source = active_bodies()
        index = next(i for i, m in enumerate(source) if '"kind": "gattDiscovered"' in m)
        original = active_gatt_event()
        for fault in (
            "schema",
            "missing-counts",
            "fractional-count",
            "missing-characteristic",
            "bad-property",
            "missing-cccd",
            "duplicate-id",
            "foreign-route",
            "full-tree-as-compact",
        ):
            with self.subTest(fault=fault):
                event = copy.deepcopy(original)
                characteristic = event["gattServices"][0]["characteristics"][0]
                if fault == "schema":
                    event["auditSchema"] = "unknown-v2"
                elif fault == "missing-counts":
                    event.pop("gattSummary")
                elif fault == "fractional-count":
                    event["gattSummary"]["serviceCount"] = 4.0
                elif fault == "missing-characteristic":
                    event["gattServices"][0]["characteristics"].pop()
                elif fault == "bad-property":
                    characteristic["readable"] = False
                elif fault == "missing-cccd":
                    event["gattServices"][0]["characteristics"][1]["descriptors"] = []
                elif fault == "duplicate-id":
                    characteristic["id"] = event["gattServices"][0]["id"]
                elif fault == "foreign-route":
                    event["connectionId"] = "999"
                else:
                    event["gattServices"] = active_gatt_event(compact=False)[
                        "gattServices"
                    ]
                markers = list(source)
                markers[index] = "active:event:" + json.dumps(event)
                with self.assertRaises(ledger.EvidenceError):
                    self.verify(markers)
        oversized = list(source)
        oversized[index] = "active:event:" + " " * 3500 + json.dumps(original)
        with self.assertRaisesRegex(ledger.EvidenceError, "audit byte budget"):
            self.verify(oversized)

    def test_requested_scan_stop_requires_production_command_id(self) -> None:
        original = active_bodies()
        index = next(
            i for i, marker in enumerate(original) if '"kind": "scanStopped"' in marker
        )
        expected = json.loads(original[index].removeprefix("active:event:"))
        self.assertEqual(expected["commandId"], "32003")
        self.verify(original)
        for command in (None, "999", "missing"):
            with self.subTest(command=command):
                markers = list(original)
                event = dict(expected)
                if command == "missing":
                    event.pop("commandId")
                else:
                    event["commandId"] = command
                markers[index] = "active:event:" + json.dumps(event)
                with self.assertRaises(ledger.EvidenceError):
                    self.verify(markers)

    def test_missing_duplicate_and_foreign_records_cannot_pass(self) -> None:
        source = active_bodies()
        for i, marker in enumerate(source):
            if marker.startswith(("active:", "native:arm:")):
                mutations = [source[:i] + source[i + 1 :]]
                if '"advertisementObserved"' not in marker:
                    mutations.append(source[:i] + [marker] + source[i:])
                for mutation in mutations:
                    with self.subTest(index=i), self.assertRaises(ledger.EvidenceError):
                        self.verify(mutation)
        for before, after in (
            ("active-loss", "startup-off"),
            ('"category": "connection"', '"category": "timeout"'),
            ('"commandId": "32008"', '"commandId": "999"'),
            ('"pendingIo": 0', '"pendingIo": 1'),
        ):
            with self.subTest(after=after), self.assertRaises(ledger.EvidenceError):
                self.verify([m.replace(before, after) for m in source])

    def test_active_proof_cannot_be_downgraded_to_one_pair_or_idle_hold(self) -> None:
        runtime = self.verify()
        request, receipt, original = active_physical_sidecars(runtime)
        for key, value in (
            ("adapter_mode", "startup-off"),
            ("connections", 1),
            ("events", original["events"][2:]),
            ("hold_status", {"state": "IDLE", "identity": None}),
            ("active_protocol", {}),
        ):
            physical = copy.deepcopy(original)
            physical[key] = value
            with (
                self.subTest(key=key),
                self.assertRaises((ledger.EvidenceError, KeyError, ValueError)),
            ):
                ledger.verify_physical(
                    physical,
                    request=request,
                    receipt=receipt,
                    receipt_sha256=original["receipt_sha256"],
                    runtime=runtime,
                )
        physical = copy.deepcopy(original)
        next(
            b
            for b in physical["boundaries"]
            if b["boundary"] == "active-off-authorized"
        )["monotonic"] += 10
        with self.assertRaises(ledger.EvidenceError):
            ledger.verify_active_physical(physical, runtime, original["receipt_sha256"])


class AdapterStateControlTests(unittest.TestCase):
    def verify(self, value: dict) -> None:
        ledger.verify_state_control(value, RUN, DEVICE)

    def test_complete_control_accepts_turning_samples_absolute_adb_and_zero_times(
        self,
    ) -> None:
        self.verify(state_control())
        for state in (
            "ON",
            "OFF",
            "TURNING_ON",
            "TURNING_OFF",
            "BLE_ON",
            "BLE_TURNING_ON",
            "BLE_TURNING_OFF",
        ):
            value = state_control()
            for action in value["actions"][1:3]:
                action["receipts"][1]["stdout"] = f"  state: {state}\n"
            self.verify(value)
        for executable in (
            "/sdk/platform-tools/adb",
            r"C:\Android\platform-tools\adb.exe",
        ):
            value = state_control()
            for action in value["actions"]:
                action["started_monotonic"] = action["confirmed_monotonic"] = 0
                for receipt in action["receipts"]:
                    receipt["command"][0] = executable
                    receipt["started_monotonic"] = receipt["finished_monotonic"] = 0
            self.verify(value)

    def test_control_identity_status_and_exact_schema(self) -> None:
        for key, value in (
            ("schema_version", True),
            ("platform", "ios"),
            ("device", "foreign"),
            ("run_id", BOOT),
            ("status", "failed"),
            ("initial_state", "OFF"),
            ("final_state", "OFF"),
            ("primary_error", "failed"),
            ("cleanup_errors", ["restore failed"]),
        ):
            changed = state_control()
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.verify(changed)
        for target in ((), ("actions", 1), ("actions", 1, "receipts", 0)):
            original = state_control()
            node = original
            for key in target:
                node = node[key]
            for key in tuple(node):
                changed = copy.deepcopy(original)
                current = changed
                for part in target:
                    current = current[part]
                del current[key]
                with (
                    self.subTest(target=target, missing=key),
                    self.assertRaises(ValueError),
                ):
                    self.verify(changed)
            node["unknown"] = True
            with self.subTest(target=target, extra=True), self.assertRaises(ValueError):
                self.verify(original)

    def test_control_actions_exact_once_in_order_no_restore_only(self) -> None:
        for index in range(4):
            for kind in ("missing", "duplicate", "restore", "swapped"):
                value = state_control()
                actions = value["actions"]
                if kind == "missing":
                    del actions[index]
                elif kind == "duplicate":
                    actions.insert(index, copy.deepcopy(actions[index]))
                elif kind == "restore":
                    actions[index]["label"] = "restore-only"
                else:
                    actions[index], actions[(index + 1) % 4] = (
                        actions[(index + 1) % 4],
                        actions[index],
                    )
                with (
                    self.subTest(index=index, kind=kind),
                    self.assertRaises(ValueError),
                ):
                    self.verify(value)
        for field in ("requested_state", "observed_state"):
            value = state_control()
            value["actions"][1][field] = "ON"
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(value)

    def test_control_exact_commands_foreign_device_svc_counts_and_final_readback(
        self,
    ) -> None:
        bad_commands = [
            ["adb", "-s", "foreign", "shell", "dumpsys", "bluetooth_manager"],
            ["adb", "shell", "dumpsys", "bluetooth_manager"],
            ["adb", "-d", "shell", "dumpsys", "bluetooth_manager"],
            [
                "adb",
                "-s",
                DEVICE,
                "-s",
                DEVICE,
                "shell",
                "dumpsys",
                "bluetooth_manager",
            ],
            ["sh", "-s", DEVICE, "shell", "dumpsys", "bluetooth_manager"],
            ["adb", "-s", DEVICE, "shell", "dumpsys bluetooth_manager"],
            ["adb", "-s", DEVICE, "shell", "dumpsys", "bluetooth_manager", "extra"],
            ["adb", "-s", DEVICE, "shell", "svc", "bluetooth", "enable"],
            [],
            [False],
            "adb -s foreign shell dumpsys bluetooth_manager",
        ]
        for command in bad_commands:
            value = state_control()
            value["actions"][0]["receipts"][0]["command"] = command
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.verify(value)
        for action_index in (1, 2):
            for sample in (0, 1, 2):
                value = state_control()
                receipts = value["actions"][action_index]["receipts"]
                # Remove svc, repeat it, or replace the final dumpsys with svc.
                receipts[sample]["command"] = receipts[1 if sample == 0 else 0][
                    "command"
                ][:]
                with (
                    self.subTest(action=action_index, sample=sample),
                    self.assertRaises(ValueError),
                ):
                    self.verify(value)
            value = state_control()
            value["actions"][action_index]["receipts"] = value["actions"][action_index][
                "receipts"
            ][:1]
            with self.assertRaises(ValueError):
                self.verify(value)

    def test_control_final_state_unique_line_not_substring_and_all_returns_succeed(
        self,
    ) -> None:
        for action_index in range(4):
            for stdout in (
                "",
                "state: TURNING_ON\n",
                "state: BLE_ON\n",
                "state: UNKNOWN\n",
                "other state: ON\n",
                "state: ON trailing\n",
                "state: ON\nstate: OFF\n",
                "state: ON\nstate: ON\n",
            ):
                value = state_control()
                value["actions"][action_index]["receipts"][-1]["stdout"] = stdout
                with (
                    self.subTest(action=action_index, stdout=stdout),
                    self.assertRaises(ValueError),
                ):
                    self.verify(value)
        for returncode in (False, 1, -1, 0.0, "0", None):
            value = state_control()
            value["actions"][1]["receipts"][0]["returncode"] = returncode
            with self.subTest(returncode=returncode), self.assertRaises(ValueError):
                self.verify(value)
        value = state_control()
        value["actions"][1]["receipts"][1]["stdout"] = (
            "state: TURNING_OFF\nstate: OFF\n"
        )
        with self.assertRaises(ValueError):
            self.verify(value)

    def test_control_finite_typed_times_and_action_receipt_envelopes(self) -> None:
        for target, field in (
            (("actions", 1), "started_monotonic"),
            (("actions", 1), "confirmed_monotonic"),
            (("actions", 1, "receipts", 1), "started_monotonic"),
            (("actions", 1, "receipts", 1), "finished_monotonic"),
        ):
            for invalid in (
                True,
                -1,
                float("nan"),
                float("inf"),
                float("-inf"),
                "12",
                None,
                10**1000,
            ):
                value = state_control()
                node = value
                for key in target:
                    node = node[key]
                node[field] = invalid
                with (
                    self.subTest(target=target, field=field, invalid=invalid),
                    self.assertRaises(ValueError),
                ):
                    self.verify(value)
        for target, field, invalid in (
            (("actions", 1), "started_monotonic", 0),
            (("actions", 1), "confirmed_monotonic", 9),
            (("actions", 1, "receipts", 0), "started_monotonic", 9),
            (("actions", 1, "receipts", 0), "finished_monotonic", 9),
            (("actions", 1, "receipts", 1), "started_monotonic", 10),
            (("actions", 1, "receipts", 2), "finished_monotonic", 14),
        ):
            value = state_control()
            node = value
            for key in target:
                node = node[key]
            node[field] = invalid
            with (
                self.subTest(target=target, field=field, invalid=invalid),
                self.assertRaises(ValueError),
            ):
                self.verify(value)


class AdapterReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = ledger.verify_runtime(transcript(), RUN, SERVICE, LOG)
        self.receipt_hash = "a" * 64
        self.ready = readiness_sidecar(self.runtime, self.receipt_hash)

    def verify(self, value: dict) -> None:
        ledger.verify_readiness(
            value, receipt_sha256=self.receipt_hash, runtime=self.runtime
        )

    def test_complete_ready_and_go_pure_and_exact(self) -> None:
        original = copy.deepcopy(self.ready)
        self.verify(self.ready)
        self.assertEqual(self.ready, original)
        ledger.verify_go({"schema_version": 1, "run_id": RUN}, RUN)
        for key in self.ready:
            changed = copy.deepcopy(self.ready)
            del changed[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                self.verify(changed)
        with self.assertRaises(ValueError):
            self.verify({**self.ready, "unknown": None})
        for go in (
            {},
            {"schema_version": True, "run_id": RUN},
            {"schema_version": 1, "run_id": BOOT},
            {"schema_version": 1, "run_id": RUN, "unknown": None},
        ):
            with self.subTest(go=go), self.assertRaises(ValueError):
                ledger.verify_go(go, RUN)

    def test_ready_foreign_session_receipt_bootstrap_and_time(self) -> None:
        for key, value in (
            ("schema_version", True),
            ("status", "passed"),
            ("run_id", BOOT),
            ("receipt_sha256", "0" * 64),
            ("monotonic", True),
            ("monotonic", -1),
            ("monotonic", float("inf")),
            ("monotonic", float("nan")),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.verify({**self.ready, key: value})
        for key, value in (
            ("run_id", BOOT),
            ("pid", True),
            ("boot_id", RUN),
            ("bundle_id", "foreign"),
            ("executable", "file:/foreign.apk"),
            ("source", "payload-url"),
            ("unknown", None),
        ):
            changed = copy.deepcopy(self.ready)
            changed["native_bootstrap"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.verify(changed)
        for value in (0, 0.5):
            self.verify({**self.ready, "monotonic": value})


class AdapterPhysicalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = ledger.verify_runtime(transcript(), RUN, SERVICE, LOG)
        self.request, self.receipt, self.physical = physical_sidecars(self.runtime)
        self.receipt_hash = self.physical["receipt_sha256"]

    def verify(self, physical: dict) -> None:
        ledger.verify_physical(
            physical,
            request=self.request,
            receipt=self.receipt,
            receipt_sha256=self.receipt_hash,
            runtime=self.runtime,
        )

    def test_provider_schema_pair_bindings_and_purity(self) -> None:
        before = copy.deepcopy(
            (self.physical, self.request, self.receipt, self.runtime)
        )
        self.verify(self.physical)
        self.assertEqual(
            before, (self.physical, self.request, self.receipt, self.runtime)
        )
        # Provider and host monotonic clocks are deliberately not compared.
        for event in self.physical["events"]:
            event["monotonic"] += 10000
        self.physical["boundaries"] = [
            self.physical["boundaries"][0],
            *[
                {"boundary": "gap-" + e["kind"], "cycle": -1, **e}
                for e in self.physical["events"]
            ],
        ]
        self.verify(self.physical)

    def test_readiness_boundary_exact_once_and_connection_can_precede_log_observation(
        self,
    ) -> None:
        # Logcat can lag GAP; the ready ACK, not on-started, opens the window.
        self.physical["on-started"] = 103.0
        self.verify(self.physical)
        original = copy.deepcopy(self.physical)
        for index in range(3):
            for kind in ("missing", "duplicate", "swap"):
                changed = copy.deepcopy(original)
                boundaries = changed["boundaries"]
                if kind == "missing":
                    del boundaries[index]
                elif kind == "duplicate":
                    boundaries.insert(index, copy.deepcopy(boundaries[index]))
                else:
                    boundaries[index], boundaries[(index + 1) % 3] = (
                        boundaries[(index + 1) % 3],
                        boundaries[index],
                    )
                with (
                    self.subTest(index=index, kind=kind),
                    self.assertRaises(ValueError),
                ):
                    self.verify(changed)
        # Rebinding both readiness and its boundary cannot authorize a GAP that
        # was already observed before the claimed window opened.
        for time in (101.0, 101.5):
            changed = copy.deepcopy(original)
            changed["readiness"]["monotonic"] = time
            changed["boundaries"][0]["monotonic"] = time
            with self.subTest(time=time), self.assertRaises(ValueError):
                self.verify(changed)

    def test_physical_mutations_pair_time_cleanup_session_and_native_ids(self) -> None:
        mutations = [
            (("schema_version",), True),
            (("mode",), "recovery-ten"),
            (("run_id",), BOOT),
            (("session_id",), BOOT),
            (("status",), "failed"),
            (("connections",), True),
            (("disconnections",), 2),
            (("primary_error",), "failed"),
            (("cleanup_errors",), ["failed"]),
            (("rejected_events",), [{}]),
            (("cleanup", "advertising_stopped"), False),
            (("cleanup", "hold_inactive"), 1),
            (("cleanup", "peer_count"), False),
            (("fixture_pid",), True),
            (("fixture_pgid",), 99999),
            (("receipt_sha256",), "0" * 64),
            (("contract_sha256",), "0" * 64),
            (("profile_sha256",), "0" * 64),
            (("resource_identity",), "serial:foreign"),
            (("native_bootstrap", "pid"), 1),
            (("native_signal", "boot_id"), RUN),
            (("resources", "off", "listeners"), 1),
            (("resources", "on", "hubRoots"), False),
            (("hold_status", "state"), "HELD"),
            (("events",), []),
            (("events",), self.physical["events"] * 2),
            (("events", 0, "kind"), "disconnected"),
            (("events", 1, "peer"), ["123456789abd", 1]),
            (("events", 0, "peer"), ["123456789abc", True]),
            (("events", 0, "payload_hex"), "00"),
            (("events", 1, "payload_hex"), "00bc9a78563412"),
            (("boundaries",), []),
            (("on-started",), 98.0),
            (("readiness",), None),
            (("readiness", "run_id"), BOOT),
            (("readiness", "receipt_sha256"), "0" * 64),
            (("readiness", "native_bootstrap", "boot_id"), RUN),
            (("readiness", "monotonic"), 101.0),
            (("boundaries", 0, "hold_inactive"), 1),
            (("boundaries", 0, "peer_count"), False),
            (("boundaries", 0, "monotonic"), 98.0),
            (("boundaries", 0, "unknown"), None),
        ]
        for path in (
            ("on-started",),
            ("events", 0, "monotonic"),
            ("events", 1, "monotonic"),
        ):
            mutations.extend(
                (path, invalid) for invalid in (0, -1, True, float("nan"), float("inf"))
            )
        mutations.append((("events", 1, "monotonic"), 101.0))
        for target, invalid in mutations:
            changed = copy.deepcopy(self.physical)
            node = changed
            for key in target[:-1]:
                node = node[key]
            node[target[-1]] = invalid
            with (
                self.subTest(target=target, invalid=invalid),
                self.assertRaises(ValueError),
            ):
                self.verify(changed)

    def test_fixture_receipt_request_hash_and_identity_mutations(self) -> None:
        for name in ("request", "receipt"):
            for key, value in (
                ("session_id", BOOT),
                ("schema_version", True),
                ("contract_sha256", "0" * 64),
                ("profile_sha256", "0" * 64),
                ("resource_identity", "serial:foreign"),
                ("port", "foreign-port"),
            ):
                original = getattr(self, name)
                setattr(self, name, {**original, key: value})
                with self.subTest(name=name, key=key), self.assertRaises(ValueError):
                    self.verify(self.physical)
                setattr(self, name, original)
        for key, value in (
            ("fixture_pid", 99999),
            ("fixture_pgid", True),
            ("proof_method", "cached"),
        ):
            original = self.receipt
            self.receipt = {**original, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.verify(self.physical)
            self.receipt = original
        self.request["service_uuid"] = BOOT
        with self.assertRaises(ValueError):
            self.verify(self.physical)


class AdapterTransitionLedgerTests(unittest.TestCase):
    def verify(self, markers: list[str]) -> dict:
        return ledger.verify_runtime(transcript(markers), RUN, SERVICE, LOG)

    def test_complete_protocol_reuses_io_checker_and_has_native_lineage(self) -> None:
        from tools.react_native import check_runtime_log

        with mock.patch.object(
            check_runtime_log, "check_rf_scan", wraps=check_runtime_log.check_rf_scan
        ) as check:
            result = self.verify(bodies())
        check.assert_called_once()
        self.assertEqual(
            check.call_args.kwargs, {"connect": True, "gatt": True, "io": True}
        )
        self.assertIn(RAW_PREFIX + "rf-io:passed:", check.call_args.args[0])
        self.assertEqual(result["runtime_nonce"], NONCE)
        self.assertEqual(result["native_bootstrap"], BOOTSTRAP)
        self.assertEqual(result["off_events"], OFF_EVENTS)
        self.assertEqual(
            result["closed_resources"],
            dict.fromkeys(("off", "on"), dict.fromkeys(RECOVERY_RESOURCE_FIELDS, 0)),
        )
        self.assertEqual(
            result["runtime_sha256"], hashlib.sha256(transcript().encode()).hexdigest()
        )

    def test_every_record_is_required_unique_and_ordered(self) -> None:
        original = bodies()
        for index in range(len(original)):
            mutations = {
                "missing": original[:index] + original[index + 1 :],
                "duplicate": original[:index] + [original[index]] + original[index:],
                "unknown": original[:index] + ["unknown:passed"] + original[index:],
            }
            if index + 1 < len(original):
                mutations["swap"] = (
                    original[:index]
                    + [original[index + 1], original[index]]
                    + original[index + 2 :]
                )
            for kind, changed in mutations.items():
                with (
                    self.subTest(index=index, kind=kind),
                    self.assertRaises(ValueError),
                ):
                    self.verify(changed)

    def test_foreign_run_nonce_native_identity_and_urls(self) -> None:
        for record in (0, 8):
            for key, value in {
                "run_id": BOOT,
                "pid": 9999,
                "boot_id": RUN,
                "bundle_id": "foreign.app",
                "executable": "file:/foreign.apk",
                "source": "wrong",
                "extra": "unknown",
            }.items():
                # Changing both records consistently is cooperative new identity;
                # changing only one must never pass (except valid bootstrap PID).
                changed = bodies()
                prefix, payload = (
                    changed[record].split(":", 2)[:2],
                    changed[record].split(":", 2)[2],
                )
                native = json.loads(payload)
                native[key] = value
                changed[record] = ":".join(prefix) + ":" + json.dumps(native)
                with (
                    self.subTest(record=record, key=key),
                    self.assertRaises(ValueError),
                ):
                    self.verify(changed)
        for before, after in (
            (f"nonce={NONCE}:phase=close", "nonce=m5678-other-boot:phase=close"),
            (f"passed:runId={RUN}", f"passed:runId={BOOT}"),
            (f"/adapter/{RUN}/on", f"/adapter/{BOOT}/on"),
        ):
            with self.subTest(before=before), self.assertRaises(ValueError):
                ledger.verify_runtime(
                    transcript().replace(before, after), RUN, SERVICE, LOG
                )
        for pid in (True, 0, -1, 1.5, "1234", 2**31):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                ledger.verify_runtime(
                    transcript().replace('"pid": 1234', '"pid": ' + json.dumps(pid)),
                    RUN,
                    SERVICE,
                    LOG,
                )

    def test_off_domain_before_terminal_and_scan_error_no_domain(self) -> None:
        for before, after in (
            ("PoweredOff", "PoweredOn"),
            ("Authorized", "Denied"),
            ("31000", "31001"),
            ("31002", "31003"),
            ("adapter_unavailable", "backend"),
        ):
            with self.subTest(before=before), self.assertRaises(ValueError):
                ledger.verify_runtime(
                    transcript().replace(before, after), RUN, SERVICE, LOG
                )
        for kind in (
            "scanStarted",
            "advertisementObserved",
            "adapterSnapshotChanged",
            "commandCompleted",
        ):
            for index in (3, 6, 10, len(bodies())):
                changed = bodies()
                changed.insert(
                    index,
                    "off:event:"
                    + json.dumps(
                        {"kind": kind, "commandId": "31002", "scanSessionId": "31001"}
                    ),
                )
                with (
                    self.subTest(kind=kind, index=index),
                    self.assertRaises(ValueError),
                ):
                    self.verify(changed)
        changed = bodies()
        event = {**OFF_EVENTS[1], "connectionId": "9007"}
        changed[3] = "off:event:" + json.dumps(event)
        with self.assertRaises(ValueError):
            self.verify(changed)

    def test_raw_nitro_optional_nulls_are_preserved_without_relaxing_unknown_fields(
        self,
    ) -> None:
        # Assert the exact spec key set so a production allowlist omission cannot
        # make the test silently miss another known optional Nitro DTO member.
        fields = {
            "kind",
            "commandId",
            "scanSessionId",
            "peripheralId",
            "connectionId",
            "connectionState",
            "gattSessionId",
            "characteristicId",
            "subscriptionId",
            "gattServices",
            "value",
            "notificationState",
            "actualNotificationMode",
            "rssi",
            "localName",
            "advertisedServices",
            "adapterState",
            "scanAuthorization",
            "error",
        }
        self.assertEqual(ledger._RF_EVENT_FIELDS, fields)
        changed = bodies()
        raw_events = [
            {**dict.fromkeys(fields), **copy.deepcopy(event)} for event in OFF_EVENTS
        ]
        for i, event in enumerate(raw_events):
            changed[i + 2] = "off:event:" + json.dumps(event, sort_keys=True)
        raw_text = transcript(changed, newline="\r\n")
        result = ledger.verify_runtime(raw_text, RUN, SERVICE, LOG)
        self.assertEqual(result["off_events"], raw_events)
        self.assertEqual(
            result["runtime_sha256"], hashlib.sha256(raw_text.encode()).hexdigest()
        )
        absent_hash = ledger.verify_runtime(
            transcript(newline="\r\n"), RUN, SERVICE, LOG
        )["runtime_sha256"]
        self.assertNotEqual(result["runtime_sha256"], absent_hash)
        raw_lines = raw_text.encode().splitlines(keepends=True)
        for phase in ledger.index_runtime(raw_text, LOG)["flows"]:
            segment = b"".join(raw_lines[phase["start_line"] - 1 : phase["end_line"]])
            self.assertEqual(phase["sha256"], hashlib.sha256(segment).hexdigest())
        for index in range(3):
            expected = OFF_EVENTS[index]
            for field in fields - set(expected) - {"error"}:
                mutated = changed[:]
                event = {**raw_events[index], field: "foreign"}
                mutated[index + 2] = "off:event:" + json.dumps(event)
                with (
                    self.subTest(index=index, field=field),
                    self.assertRaises(ValueError),
                ):
                    self.verify(mutated)
            for field, value in (
                ("unknown", None),
                ("runId", None),
                ("nonce", None),
                ("kind", None),
                ("commandId", 31000),
            ):
                mutated = changed[:]
                mutated[index + 2] = "off:event:" + json.dumps(
                    {**raw_events[index], field: value}
                )
                with (
                    self.subTest(index=index, field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    self.verify(mutated)

    def test_nullable_platform_details_with_strict_required_types_and_android_platform(
        self,
    ) -> None:
        valid_details = [
            None,
            {"platform": "platform_android"},
            {
                "platform": "platform_android",
                "code": None,
                "name": None,
                "message": None,
            },
            {
                "platform": "platform_android",
                "code": "1",
                "name": None,
                "message": "radio off",
            },
        ]
        for details in valid_details:
            changed = bodies()
            event = copy.deepcopy(OFF_EVENTS[2])
            event["error"]["platformDetails"] = details
            changed[4] = "off:event:" + json.dumps(event)
            result = self.verify(changed)
            self.assertEqual(result["off_events"][2], event)
        invalid_details = [
            False,
            [],
            "",
            {},
            {"platform": None},
            {"platform": True},
            {"platform": "platform_apple"},
            {"platform": "platform_android", "unknown": None},
        ]
        for field in ("code", "name", "message"):
            invalid_details.extend(
                {"platform": "platform_android", field: value}
                for value in (False, 0, [], {})
            )
        for details in invalid_details:
            changed = bodies()
            event = copy.deepcopy(OFF_EVENTS[2])
            event["error"]["platformDetails"] = details
            changed[4] = "off:event:" + json.dumps(event)
            with self.subTest(details=details), self.assertRaises(ValueError):
                self.verify(changed)
        for field, invalid in (
            ("category", None),
            ("message", None),
            ("message", False),
            ("message", 0),
            ("unknown", None),
        ):
            changed = bodies()
            event = copy.deepcopy(OFF_EVENTS[2])
            event["error"][field] = invalid
            changed[4] = "off:event:" + json.dumps(event)
            with (
                self.subTest(field=field, invalid=invalid),
                self.assertRaises(ValueError),
            ):
                self.verify(changed)

    def test_off_error_projection_and_opaque_diagnostic_message(self) -> None:
        for error in ({}, False, [], "", {"category": "adapter_unavailable"}):
            changed = bodies()
            changed[3] = "off:event:" + json.dumps({**OFF_EVENTS[1], "error": error})
            with self.subTest(error=error), self.assertRaises(ValueError):
                self.verify(changed)
        changed = bodies()
        event = {
            **OFF_EVENTS[2],
            "error": {
                "category": "adapter_unavailable",
                "message": "expected:failed:radio-off",
                "platformDetails": {
                    "platform": "platform_android",
                    "message": "STATE_OFF",
                },
            },
        }
        changed[4] = "off:event:" + json.dumps(event)
        self.verify(changed)
        self.assertEqual(
            ledger.index_runtime(transcript(changed), LOG)["error_markers"], []
        )
        for invalid in (
            {**event["error"], "message": None},
            {**event["error"], "unknown": True},
            {**event["error"], "platformDetails": {"platform": "platform_apple"}},
        ):
            changed[4] = "off:event:" + json.dumps({**event, "error": invalid})
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.verify(changed)

    def test_closed_resources_exact_keys_and_integer_zero_not_bool_float_or_negative_zero(
        self,
    ) -> None:
        for phase in ("off", "on"):
            position = next(
                i
                for i, body in enumerate(bodies())
                if body.startswith(phase + ":resources:")
            )
            for value in (
                "1",
                "-1",
                "false",
                "0.0",
                "-0",
                "-0.0",
                "null",
                '"0"',
                "NaN",
            ):
                changed = bodies()
                changed[position] = changed[position].replace(
                    '"activeScans": 0', '"activeScans": ' + value
                )
                with (
                    self.subTest(phase=phase, value=value),
                    self.assertRaises(ValueError),
                ):
                    self.verify(changed)
            for snapshot in (
                {},
                {**dict.fromkeys(RECOVERY_RESOURCE_FIELDS, 0), "extra": 0},
                {key: 0 for key in RECOVERY_RESOURCE_FIELDS[1:]},
            ):
                changed = bodies()
                changed[position] = phase + ":resources:" + json.dumps(snapshot)
                with (
                    self.subTest(phase=phase, snapshot=snapshot),
                    self.assertRaises(ValueError),
                ):
                    self.verify(changed)

    def test_json_duplicates_failures_foreign_markers_and_truncation(self) -> None:
        text = transcript()
        for changed in (
            text.replace('"pid": 1234', '"pid": 9999, "pid": 1234'),
            text.replace('"activeScans": 0', '"activeScans": 1, "activeScans": 0'),
            text + ledger.PREFIX + "failed:late off callback after close began\n",
            text + RAW_PREFIX + "rf:passed:foreign\n",
            text + ledger.PREFIX + "off:passed",
            text.replace("off:passed", "off:passed " + ledger.PREFIX + "off:passed"),
        ):
            with self.subTest(changed=changed[-150:]), self.assertRaises(ValueError):
                ledger.verify_runtime(changed, RUN, SERVICE, LOG)

    def test_io_mutations_are_rejected_by_existing_checker(self) -> None:
        for before, after in (
            ("writeResponseCommandId=9011", "writeResponseCommandId=1"),
            ("terminal=local-submission", "terminal=remote-confirmed"),
            ("observationMs=2000", "observationMs=0"),
            (f"serviceUuid={SERVICE}", f"serviceUuid={BOOT}"),
        ):
            with self.subTest(before=before), self.assertRaises(ValueError):
                ledger.verify_runtime(
                    transcript().replace(before, after), RUN, SERVICE, LOG
                )

    def test_inclusive_raw_ranges_hash_diagnostics_crlf_and_partial_index(self) -> None:
        for newline in ("\n", "\r\n"):
            text = transcript(newline=newline)
            index = ledger.index_runtime(text, LOG)
            raw = text.encode().splitlines(keepends=True)
            off, on = index["flows"]
            self.assertEqual(off["end_line"], on["start_line"] - 1)
            self.assertEqual(on["end_line"], len(raw))
            self.assertEqual(index["invalid_markers"], [])
            for phase in (off, on):
                segment = b"".join(raw[phase["start_line"] - 1 : phase["end_line"]])
                self.assertEqual(phase["sha256"], hashlib.sha256(segment).hexdigest())
                self.assertEqual(phase["size"], len(segment))
                self.assertEqual(
                    (
                        phase["run_id"],
                        phase["runtime_nonce"],
                        phase["pid"],
                        phase["boot_id"],
                    ),
                    (RUN, NONCE, 1234, BOOT),
                )
            self.assertNotEqual(
                on["sha256"],
                ledger.index_runtime(text + "another diagnostic\n", LOG)["flows"][1][
                    "sha256"
                ],
            )
            ledger.verify_runtime(text, RUN, SERVICE, LOG)
        partial = ledger.index_runtime(transcript(bodies()[:5]), LOG)
        self.assertEqual(
            [row["status"] for row in partial["flows"]], ["partial", "missing"]
        )


if __name__ == "__main__":
    unittest.main()
