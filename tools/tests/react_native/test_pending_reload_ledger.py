"""Synthetic checker tests only: no Android, USB, RF or real callback claims.

Immutable session snapshots and the synthetic receipt use the existing factory;
load_session/verify_receipt run unmocked against their actual files and hashes.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.peripherals.nrf52840 import fixture_session, pending_reload
from tests.peripherals.nrf52840.support.session_receipts import fake_attempt
from tools.react_native import pending_reload_ledger as api
from tools.tests.react_native.support.pending_reload_ledger import (
    ACTIONS,
    FOREIGN,
    PROBE,
    SERVICE,
    route,
    synthetic_physical,
    synthetic_runtime,
)


class PendingReloadLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.attempt = fake_attempt(self.root)
        self.plan = self.attempt.plan
        self.identity = (os.getpid(), os.getpgid(0))
        self.lines = synthetic_runtime(self.plan.session_id)
        self.document = synthetic_physical(self.plan, self.lines, self.identity)
        self.path = self.attempt.root / pending_reload.PHYSICAL_NAME

    def verify(self, *, lines=None, document=None, **kwargs) -> dict:
        self.path.write_text(
            json.dumps(self.document if document is None else document),
            encoding="utf-8",
        )
        arguments = {
            "run_id": self.plan.session_id,
            "service_uuid": SERVICE,
            "physical_path": self.path,
            "fixture_request": self.plan.request_path,
            "fixture_identity": self.identity,
            "host_actions": ACTIONS,
        }
        arguments.update(kwargs)
        return api.verify(
            "\n".join(self.lines if lines is None else lines) + "\n", **arguments
        )

    def rejected(self, **kwargs) -> None:
        with self.assertRaises((RuntimeError, ValueError)):
            self.verify(**kwargs)

    def boundary(self, name: str, generation: int = 1, document=None) -> dict:
        source = self.document if document is None else document
        return next(
            r
            for r in source["boundaries"]
            if r["boundary"] == name and r["generation"] == generation
        )

    def replaced(self, old: str, new: str) -> list[str]:
        return [line.replace(old, new) for line in self.lines]

    def test_synthetic_baseline_structured_limits_and_read_only(self) -> None:
        before = {
            p: p.read_bytes()
            for p in (
                self.plan.request_path,
                self.plan.profile_path,
                self.plan.receipt_path,
            )
        }
        with patch.object(
            api.session, "verify_receipt", wraps=fixture_session.verify_receipt
        ) as receipt:
            result = self.verify()
        receipt.assert_called_once_with(self.plan, fixture_identity=self.identity)
        self.assertEqual(result["old_characteristic_id"], "98765")
        self.assertEqual(result["new_characteristic_id"], "87654")
        self.assertEqual(result["probe_id"], PROBE)
        self.assertEqual(
            [r["event"] for r in result["native_factual_order"]],
            [
                "read-accepted",
                "engine-invalidation-close-started",
                "dispose-returned",
                "late-submit-rejected",
                "engine-close-complete",
            ],
        )
        self.assertIn("BluetoothGattCallback", result["explicit_limits"]["late_submit"])
        self.assertIn("Parent", result["explicit_limits"]["pid_provenance"])
        self.assertEqual(json.loads(json.dumps(result)), result)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_generation_2_runtime_boot_may_overlap_old_close(self) -> None:
        result = self.verify()
        new_boot = result["generations"][1]["marker_indices"][0]
        complete = result["native_factual_order"][-1]["marker_index"]
        self.assertLess(new_boot, complete)
        self.assertGreater(result["generations"][1]["marker_indices"][3], complete)
        self.verify(
            lines=[line for line in self.lines if "cleanup-started" not in line]
        )

    def test_gap_can_be_drained_in_released_sample(self) -> None:
        for generation in (1, 2):
            records = self.document["boundaries"]
            gap = self.boundary("gap-disconnected", generation)
            released = self.boundary("retired", generation)
            records.remove(gap)
            gap["monotonic"] = released["monotonic"] - 0.1
            records.insert(records.index(released), gap)
        self.verify()

    def test_host_actions_are_exact_confirmed_tuple_not_js_fake(self) -> None:
        for actions in (
            (),
            ("reload", "arm-2"),
            ("arm-1", "arm-2", "reload"),
            ACTIONS + ("reload",),
            list(ACTIONS),
        ):
            with self.subTest(actions=actions):
                self.rejected(host_actions=actions)
        self.rejected(
            lines=self.lines[:-1]
            + [
                pending_reload.PREFIX + "arm-linking-dispatched:accepted=true",
                self.lines[-1],
            ]
        )

    def test_foreign_run_or_noncanonical_run(self) -> None:
        self.rejected(run_id=FOREIGN)
        self.rejected(run_id="{" + self.plan.session_id + "}")
        self.rejected(lines=self.replaced(self.plan.session_id, FOREIGN))
        for key in ("run_id", "session_id"):
            document = copy.deepcopy(self.document)
            document[key] = FOREIGN
            self.rejected(document=document)

    def test_duplicate_rerun_and_unknown_generation(self) -> None:
        self.rejected(lines=self.lines[:1] + self.lines)
        self.rejected(lines=self.lines + self.lines)
        self.rejected(
            lines=self.lines
            + [
                pending_reload.PREFIX
                + "generation=3:runtime-start:runId="
                + self.plan.session_id
                + ":nonce=third"
            ]
        )

    def test_nonce_reuse_missing_and_physical_mismatch(self) -> None:
        self.rejected(lines=self.replaced("synthetic-engine-2", "synthetic-engine-1"))
        self.rejected(
            lines=[
                line for line in self.lines if "generation=2:runtime-start:" not in line
            ]
        )
        self.document["runtime_nonces"]["2"] = "foreign-engine"
        self.rejected()

    def test_normalized_prefix_and_foreign_lines_are_not_silently_filtered(
        self,
    ) -> None:
        for line in (
            "",
            "unrelated log",
            "10-14 12:00 123 123 I " + self.lines[0],
            "BLEHUB_NITRO_RUNTIME rf:passed",
        ):
            with self.subTest(line=line):
                self.rejected(lines=[line, *self.lines])

    def test_any_js_failure_unknown_or_old_route_is_rejected(self) -> None:
        for marker in (
            "generation=1:failed:cancelled",
            "generation=2:failed:gatt error",
            "generation=2:close:failed:error",
            "generation=2:event:commandId=12008",
            "generation=1:read:passed:" + route(1) + ":value=00",
        ):
            with self.subTest(marker=marker):
                self.rejected(
                    lines=self.lines[:-1]
                    + [pending_reload.PREFIX + marker, self.lines[-1]]
                )

    def test_js_handshake_and_initialization_order(self) -> None:
        for generation in (1, 2):
            for first, second in (
                ("native-ready", "initialize"),
                ("gatt", "awaiting-arm"),
                ("awaiting-arm", "arm-js-observed"),
                ("scan", "connected"),
            ):
                lines = self.lines.copy()
                a = next(
                    i
                    for i, line in enumerate(lines)
                    if f"generation={generation}:{first}:" in line
                )
                b = next(
                    i
                    for i, line in enumerate(lines)
                    if f"generation={generation}:{second}:" in line
                )
                lines[a], lines[b] = lines[b], lines[a]
                with self.subTest(generation=generation, first=first):
                    self.rejected(lines=lines)

    def test_each_arm_ack_is_required_once(self) -> None:
        for generation in (1, 2):
            line = next(
                line
                for line in self.lines
                if f"generation={generation}:arm-js-observed:" in line
            )
            self.rejected(lines=[other for other in self.lines if other != line])
            index = self.lines.index(line)
            self.rejected(lines=self.lines[:index] + [line] + self.lines[index:])

    def test_canonical_read_ids_and_scanned_peripheral_correlation(self) -> None:
        for old, new in (
            ("characteristicId=98765", "characteristicId=098765"),
            ("characteristicId=87654", "characteristicId=18446744073709551616"),
            ("connectionId=13004", "connectionId=12004"),
            ("gattSessionId=12006", "gattSessionId=12007"),
            ("commandId=13008", "commandId=13009"),
        ):
            self.rejected(lines=self.replaced(old, new))
        self.rejected(
            lines=self.replaced(
                "connectionId=13004:peripheralId=88",
                "connectionId=13004:peripheralId=89",
            )
        )

    def test_native_failure_duplicate_unknown_or_fake_suffix(self) -> None:
        accepted = next(line for line in self.lines if ":native:read-accepted:" in line)
        for marker in (
            accepted,
            accepted.replace("read-accepted", "unknown-event"),
            accepted.replace("read-accepted", "native-boundary-failed"),
        ):
            self.rejected(lines=self.lines[:-1] + [marker, self.lines[-1]])
        self.rejected(
            lines=self.replaced(
                "source=synthetic:result=closed",
                "source=BluetoothGattCallback:result=closed",
            )
        )
        self.rejected(
            lines=self.replaced(
                "source=synthetic:result=closed", "source=synthetic:result=accepted"
            )
        )

    def test_native_probe_route_and_raw_physical_facts_match_exactly(self) -> None:
        self.rejected(lines=self.replaced(PROBE, FOREIGN))
        self.document["native_close_facts"]["native:dispose-returned"] += ":extra=true"
        self.rejected()
        self.document = synthetic_physical(self.plan, self.lines, self.identity)
        self.boundary("native-dispose-observed")["probe_id"] = FOREIGN
        self.rejected()

    def test_native_factual_order_and_generation_2_readiness_barrier(self) -> None:
        for first, second in (
            (":native:dispose-returned:", ":native:late-submit-rejected:"),
            (":native:late-submit-rejected:", ":native:engine-close-complete:"),
            (":native:engine-close-complete:", "generation=2:native-ready:"),
        ):
            lines = self.lines.copy()
            a = next(i for i, line in enumerate(lines) if first in line)
            b = next(i for i, line in enumerate(lines) if second in line)
            lines[a], lines[b] = lines[b], lines[a]
            self.rejected(lines=lines)

    def test_new_read_value_close_and_final_pass_are_mandatory(self) -> None:
        for marker in (
            "generation=2:read:passed:",
            "generation=2:close:passed",
            ":passed:runId=",
        ):
            self.rejected(lines=[line for line in self.lines if marker not in line])
        self.rejected(lines=self.replaced(":value=00", ":value=01"))
        lines = self.lines.copy()
        lines[-1], lines[-2] = lines[-2], lines[-1]
        self.rejected(lines=lines)

    def test_physical_schema_failure_and_error_fields(self) -> None:
        for field, value in (
            ("status", "failed"),
            ("cleanup_errors", ["timeout"]),
            ("schema_version", True),
            ("primary_error", "failure"),
            ("unexpected", False),
        ):
            document = copy.deepcopy(self.document)
            document[field] = value
            self.rejected(document=document)

    def test_fixture_process_resource_profile_and_target_binding(self) -> None:
        for field, value in (
            ("fixture_pid", self.identity[0] + 1),
            ("fixture_pgid", True),
            ("resource_identity", "jlink:654321"),
            ("contract_sha256", "00" * 32),
            ("profile_sha256", "00" * 32),
            ("profile_id", "foreign"),
            ("service_uuid", FOREIGN),
            ("target_uuid", FOREIGN),
            ("target_handle", 0),
            ("target_handle", 65536),
            ("live_receipt_path", "historical.json"),
            ("port", "/dev/foreign"),
        ):
            document = copy.deepcopy(self.document)
            document[field] = value
            self.rejected(document=document)
        self.rejected(fixture_identity=(True, self.identity[1]))
        self.rejected(fixture_identity=(self.identity[0] + 1, self.identity[1]))

    def test_target_permissions_require_ordinary_rw_native_43(self) -> None:
        for field, value in (
            ("profile", True),
            ("profile", 0x13),
            ("native", 0x03),
            ("native", 0x41),
            ("native", 0xC3),
            ("expected_native", 0x03),
        ):
            document = copy.deepcopy(self.document)
            document["target_permissions"][field] = value
            self.rejected(document=document)

    def test_physical_sequence_handle_and_peer_drift(self) -> None:
        for name, field, value in (
            ("waiting", "sequence", 9),
            ("retired", "handle", 52),
            ("waiting", "peer", ["665544332211", 1]),
            ("gap-disconnected", "observed_peer", ["665544332211", 1]),
            ("armed", "sequence", True),
            ("waiting", "peer", ["112233445566", True]),
        ):
            document = copy.deepcopy(self.document)
            self.boundary(name, document=document)[field] = value
            self.rejected(document=document)
        document = copy.deepcopy(self.document)
        for record in document["boundaries"]:
            if record["generation"] == 2 and record["boundary"] != "gap-connected":
                record["sequence"] = 7
        self.rejected(document=document)

    def test_physical_states_missing_duplicate_and_dispose_position(self) -> None:
        for state in ("ABORTED", "EXPIRED", "RELEASE_REQUESTED", False):
            document = copy.deepcopy(self.document)
            self.boundary("retired", document=document)["state"] = state
            self.rejected(document=document)
        for name in ("waiting", "native-dispose-observed", "gap-disconnected"):
            document = copy.deepcopy(self.document)
            record = self.boundary(name, document=document)
            document["boundaries"].remove(record)
            self.rejected(document=document)
            document["boundaries"].append(record)
            document["boundaries"].append(record)
            self.rejected(document=document)
        document = copy.deepcopy(self.document)
        records = document["boundaries"]
        disposed = self.boundary("native-dispose-observed", document=document)
        records.remove(disposed)
        disposed["monotonic"] = 105.1
        records.insert(
            records.index(self.boundary("retired", document=document)), disposed
        )
        self.rejected(document=document)

    def test_physical_config_timing_finite_bool_and_rewinds(self) -> None:
        for value in (True, float("nan"), float("inf"), -1, "102"):
            document = copy.deepcopy(self.document)
            self.boundary("waiting", document=document)["monotonic"] = value
            self.rejected(document=document)
        document = copy.deepcopy(self.document)
        self.boundary("waiting", document=document)["monotonic"] = 100.5
        self.rejected(document=document)
        for key in self.document["budgets_seconds"]:
            document = copy.deepcopy(self.document)
            document["budgets_seconds"][key] += 1
            self.rejected(document=document)
        self.document["budgets_seconds"]["resolve_to_retired"] = True
        self.rejected()

    def test_exact_deadline_boundaries_are_not_accepted(self) -> None:
        for budget in ("arm", "release", "native", "whole", "cleanup"):
            document = copy.deepcopy(self.document)
            if budget in ("arm", "native"):
                threshold = 102 if budget == "arm" else 104
                for record in document["boundaries"]:
                    if record["monotonic"] >= threshold:
                        record["monotonic"] += 6.9
                        for anchor in ("armed_at", "first_waiting_at", "resolve_at"):
                            if anchor in record:
                                record[anchor] += 6.9
            elif budget == "release":
                self.boundary("retired", 2, document)["monotonic"] = 117.9
                self.boundary("gap-disconnected", 2, document)["monotonic"] = 117.9
            elif budget == "whole":
                self.boundary("closed", 2, document)["monotonic"] = 280.0
            else:
                self.boundary("physical-cleanup", 2, document)["monotonic"] = 147.0
                self.boundary("closed", 2, document)["monotonic"] = 147.0
            messages = {
                "arm": "arm to held budget exceeded",
                "release": "resolve retirement budget exceeded",
                "native": "native close budget exceeded",
                "whole": "physical whole budget exceeded",
                "cleanup": "cleanup phase budget exceeded",
            }
            with (
                self.subTest(budget=budget),
                self.assertRaisesRegex(RuntimeError, messages[budget]),
            ):
                self.verify(document=document)

    def test_v2_retained_receipts_bind_full_ticket_outcome_and_all_six_owners(
        self,
    ) -> None:
        from dataclasses import replace

        from tests.peripherals.nrf52840 import fault_control as wire

        for generation in (1, 2):
            original = wire.decode_status(
                bytes.fromhex(self.boundary("retired", generation)["status_wire"])
            )
            for field in wire.RefCounters.__dataclass_fields__:
                document = copy.deepcopy(self.document)
                status = replace(original, refs=replace(original.refs, **{field: 1}))
                self.boundary("retired", generation, document)["status_wire"] = (
                    wire.encode_status(status).hex()
                )
                self.rejected(document=document)
            for status in (
                replace(
                    original,
                    ticket=replace(original.ticket, owner_nonce=bytes(range(1, 17))),
                ),
                replace(original, ticket=replace(original.ticket, connection_epoch=99)),
                replace(original, flags=original.flags & ~wire.StatusFlags.REARMABLE),
                replace(original, outcome=wire.Outcome.ARM_EXPIRED),
            ):
                document = copy.deepcopy(self.document)
                self.boundary("retired", generation, document)["status_wire"] = (
                    wire.encode_status(status).hex()
                )
                self.rejected(document=document)

    def test_cleanup_inactive_no_peers_stopped_and_closed_peer_zero(self) -> None:
        for field in ("hold_inactive", "no_known_peers", "advertising_stopped"):
            document = copy.deepcopy(self.document)
            document["physical_cleanup"][field] = False
            self.rejected(document=document)
        for field, value in (
            ("peer_count", 1),
            ("peer_count", False),
            ("hold_inactive", False),
            ("generation", True),
        ):
            document = copy.deepcopy(self.document)
            self.boundary("closed", 2, document)[field] = value
            self.rejected(document=document)
        for state in ("ABORTED", "EXPIRED"):
            document = copy.deepcopy(self.document)
            document["physical_cleanup"]["hold_state"] = state
            self.boundary("physical-cleanup", 2, document)["hold_state"] = state
            self.rejected(document=document)

    def test_current_receipt_files_and_actual_snapshot_hashes_required(self) -> None:
        self.plan.receipt_path.unlink()
        self.rejected()
        # Restore synthetic proof through the existing factory in a separate dir.
        root = self.root / "second"
        root.mkdir()
        attempt = fake_attempt(root)
        self.rejected(fixture_request=attempt.plan.request_path)
        self.plan = attempt.plan
        self.identity = (os.getpid(), os.getpgid(0))
        self.lines = synthetic_runtime(self.plan.session_id)
        self.document = synthetic_physical(self.plan, self.lines, self.identity)
        self.verify()
        receipt = json.loads(self.plan.receipt_path.read_bytes())
        receipt["fixture_pid"] += 1
        self.plan.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.rejected()
        receipt["fixture_pid"] -= 1
        self.plan.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.verify()
        stale = dict(receipt, captured_at_utc="2000-01-01T00:00:00+00:00")
        self.plan.receipt_path.write_text(json.dumps(stale), encoding="utf-8")
        self.rejected()
        self.plan.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.plan.profile_path.chmod(0o600)
        self.plan.profile_path.write_bytes(self.plan.profile_path.read_bytes() + b" ")
        self.rejected()


if __name__ == "__main__":
    unittest.main()
