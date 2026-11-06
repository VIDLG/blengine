"""Offline tamper/order regressions against real controller-format journals."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

from tests.peripherals.nrf52840 import permission_transition as wire
from tests.peripherals.nrf52840.fixture_session import _json
from tools.react_native import permission_transition_ledger as ledger
from tools.tests.react_native.support.permission_host_samples import (
    DEVICE,
    RUN,
    Harness,
)


class PermissionLedgerTests(unittest.TestCase):
    def test_root_transport_requires_original_identity_and_exact_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = Harness(Path(directory), stack, use_root=True)
            h.capture.run()
            h.verify()
            self.assertEqual(h.permission.finish(None), [])
            control = _json(h.permission.path.read_bytes())
            ledger.verify_permission_control(control, RUN, DEVICE)
            for fault in (
                "missing-proof",
                "nonroot",
                "script",
                "shell-fallback",
                "mode",
            ):
                value = copy.deepcopy(control)
                initial = value["actions"][0]
                mutations = [
                    r
                    for a in value["actions"]
                    for r in a["receipts"]
                    if r["command"][4:6] == ["su", "-c"] and r["command"][6] != "id"
                ]
                if fault == "missing-proof":
                    initial["receipts"].pop(0)
                elif fault == "nonroot":
                    row = initial["receipts"][0]
                    row["stdout"] = "uid=2000(shell) gid=2000(shell)\n"
                    row["stdout_bytes"] = len(row["stdout"].encode())
                    row["stdout_sha256"] = ledger.digest(row["stdout"].encode())
                elif fault == "script":
                    mutations[0]["command"][-1] += "; am force-stop com.blehubnitrohost"
                elif fault == "shell-fallback":
                    mutations[0]["command"][4:] = [
                        "pm",
                        "revoke",
                        "--user",
                        "0",
                        "com.blehubnitrohost",
                        "android.permission.BLUETOOTH_SCAN",
                    ]
                else:
                    value["permission_command_mode"] = "shell"
                with (
                    self.subTest(fault=fault),
                    self.assertRaises((RuntimeError, ValueError)),
                ):
                    ledger.verify_permission_control(value, RUN, DEVICE)

    def test_evidence_boundaries_are_not_inferred_from_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = Harness(Path(directory), stack)
            h.capture.run()
            h.verify()
            boundaries = copy.deepcopy(h.capture.result["boundaries"])
            for fault in ("absent", "early-death", "early-arm", "duplicate-death"):
                with self.subTest(fault=fault):
                    h.capture.result["boundaries"] = copy.deepcopy(boundaries)
                    rows = h.capture.result["boundaries"]
                    if fault == "absent":
                        rows.clear()
                    elif fault == "early-death":
                        rows[3]["after_marker"] = 1
                    elif fault == "early-arm":
                        rows[1]["after_marker"] = rows[0]["after_marker"]
                    else:
                        rows.insert(4, copy.deepcopy(rows[3]))
                    with self.assertRaises((RuntimeError, ValueError)):
                        h.verify()

    def test_stale_reason_wrong_uid_pid_procstart_and_budget_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = Harness(Path(directory), stack)
            h.capture.run()
            path = h.log.parent / h.capture.result["os_death_proof"]
            original = path.read_bytes()
            for fault in (
                "reason10",
                "stale",
                "uid",
                "pid",
                "user",
                "start",
                "native-start",
                "late-revoke",
                "late-death",
                "raw-output",
            ):
                with self.subTest(fault=fault):
                    proof = _json(original)
                    revoke, death = proof["actions"][-2:]
                    if fault == "reason10":
                        death["exit_record"]["reason"] = 10
                    elif fault == "stale":
                        proof["actions"][0]["exit_records"].append(death["exit_record"])
                    elif fault in ("uid", "pid", "user"):
                        death["exit_record"][
                            {"uid": "real_uid", "pid": "pid", "user": "user_id"}[fault]
                        ] += 1
                    elif fault == "start":
                        revoke["old_identity"]["start_ticks"] += 1
                    elif fault == "native-start":
                        revoke["old_identity"]["native_started_unix_ms"] += 1
                    elif fault == "late-revoke":
                        h.capture.result["authorized_monotonic"] -= 4.01
                    elif fault == "late-death":
                        death["finished_monotonic"] += 7
                    else:
                        death["receipts"][1]["stdout"] += "tampered"
                    if fault != "late-revoke":
                        path.write_text(json.dumps(proof))
                    with self.assertRaises((RuntimeError, ValueError)):
                        h.verify()
                    path.write_bytes(original)
                    if fault == "late-revoke":
                        h.capture.result["authorized_monotonic"] += 4.01

    def test_original_proof_bytes_hash_cannot_be_replaced_by_equivalent_json(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = Harness(Path(directory), stack)
            h.capture.run()
            path = h.log.parent / h.capture.result["os_death_proof"]
            path.write_text(
                json.dumps(json.loads(path.read_bytes()), separators=(",", ":"))
            )
            with self.assertRaisesRegex(RuntimeError, "original OS proof"):
                h.verify()

    def test_foreign_handshake_hash_nonce_and_generation_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = Harness(Path(directory), stack)
            h.capture.run()
            originals = copy.deepcopy(h.capture.documents)
            for name, field, value in (
                (wire.ARM_READY_NAME, "hold", {"sequence": 8, "handle": 51}),
                (wire.REVOKE_AUTHORIZED_NAME, "request_sha256", "0" * 64),
                (wire.REVOKE_AUTHORIZED_NAME, "request_nonce", RUN),
                (wire.RESTORED_READY_NAME, "peer_count", 1),
                (wire.REVOKE_READY_NAME, "native_bootstrap", {}),
            ):
                with self.subTest(name=name, field=field):
                    h.capture.documents = copy.deepcopy(originals)
                    document = _json(h.capture.documents[name])
                    document[field] = value
                    h.capture.documents[name] = json.dumps(document).encode()
                    with self.assertRaises((RuntimeError, ValueError)):
                        h.verify()

    def test_dead_active_zero_late_old_pid_and_host_force_are_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = Harness(Path(directory), stack)
            text = h.capture.run()
            active = h.capture.result["phases"]["active"]
            active["closed_resources"] = dict.fromkeys(wire.RESOURCE_FIELDS, 0)
            with self.assertRaisesRegex(RuntimeError, "dead Probe"):
                h.verify()
            active["closed_resources"] = None
            h.verify()
            tail = next(
                line
                for line in text.splitlines()
                if "active:pending-resources:" in line
            )
            with self.assertRaises((RuntimeError, ValueError)):
                h.verify(text + tail + "\n")
            h.capture.result["commands"].append(
                {
                    "command": [
                        "/sdk/adb",
                        "-s",
                        DEVICE,
                        "shell",
                        "am",
                        "force-stop",
                        "com.blehubnitrohost",
                    ],
                    "phase": "active",
                    "started_monotonic": h.capture.result["authorized_monotonic"]
                    + 0.01,
                    "finished_monotonic": h.capture.result["authorized_monotonic"]
                    + 0.02,
                }
            )
            with self.assertRaisesRegex(RuntimeError, "host force"):
                h.verify()

    def test_three_real_gap_pairs_required_not_app_markers_or_wrong_hold(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = Harness(Path(directory), stack)
            h.capture.run()
            runtime = h.verify()
            request, receipt, physical = h.physical(runtime)

            def verify(value: dict) -> None:
                ledger.verify_physical(
                    value,
                    request=request,
                    receipt=receipt,
                    receipt_sha256=h.capture.receipt_sha256,
                    runtime=runtime,
                    documents=h.capture.documents,
                )

            verify(physical)
            for fault in (
                "pair-missing",
                "wrong-payload",
                "wrong-hold",
                "early-loss",
                "late-watchdog",
            ):
                with self.subTest(fault=fault):
                    value = copy.deepcopy(physical)
                    if fault == "pair-missing":
                        value["events"].pop()
                    elif fault == "wrong-payload":
                        value["events"][3]["payload_hex"] = "00" * 7
                    elif fault == "wrong-hold":
                        value["hold_status"]["ticket"]["sequence"] += 1
                    elif fault == "early-loss":
                        value["boundaries"][5]["status"] = value["boundaries"][3][
                            "status"
                        ]
                    else:
                        value["boundaries"][7]["monotonic"] += 8
                    with self.assertRaises((RuntimeError, ValueError)):
                        verify(value)

    def test_final_permission_grants_are_separate_from_death_and_index_is_partial(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = Harness(Path(directory), stack)
            text = h.capture.run()
            self.assertEqual(h.permission.finish(None), [])
            ledger.verify_permission_control(
                _json(h.permission.path.read_bytes()), RUN, DEVICE
            )
            result = ledger.index_runtime(text, h.log.name)
            self.assertEqual(
                [row["phase"] for row in result["flows"]], list(wire.PHASES)
            )
            self.assertEqual(
                [row["status"] for row in result["flows"]], ["partial"] * 3
            )
            self.assertIsNone(result["flows"][1]["closed_resources"])
            self.assertEqual(
                ledger.index_runtime("", h.log.name)["flows"][0]["status"], "missing"
            )
            control = _json(h.permission.path.read_bytes())
            control["final_grants"][next(iter(control["final_grants"]))] = False
            with self.assertRaises((RuntimeError, ValueError)):
                ledger.verify_permission_control(control, RUN, DEVICE)
