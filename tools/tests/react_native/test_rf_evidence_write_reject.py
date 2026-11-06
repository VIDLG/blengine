"""Write-reject bundles are independent, portable audits, not physical RF proof."""

from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.peripherals.nrf52840 import write_reject as wire
from tests.peripherals.nrf52840.fault_control import (
    status_document,
    status_from_document,
)
from tests.peripherals.nrf52840.fixture_session import FixtureSessionError
from tools.react_native import rf_evidence_bundle as evidence
from tools.react_native import write_reject_ledger
from tools.tests.react_native.support import evidence_write_reject as samples


class WriteRejectEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def ready(self, platform="android"):
        return samples.ready(self.root / platform, platform)

    def finish(self, bundle) -> None:
        bundle.finish(
            completed=True,
            primary_error=None,
            cleanup_errors=[],
            runtime_log=bundle.attempt / "write-reject-runtime.log",
        )

    def clone(self, source: Path, name: str) -> Path:
        destination = self.root / name
        shutil.copytree(source, destination)
        return destination

    def test_both_platforms_finish_replay_exact_interface_and_relocate_without_source(
        self,
    ):
        for platform in ("android", "ios"):
            with self.subTest(platform=platform):
                bundle, h = self.ready(platform)
                with mock.patch.object(
                    write_reject_ledger, "verify", wraps=write_reject_ledger.verify
                ) as verify:
                    self.finish(bundle)
                self.assertGreaterEqual(verify.call_count, 2)
                for call in verify.call_args_list:
                    self.assertEqual(
                        set(call.kwargs),
                        {
                            "platform",
                            "run_id",
                            "service_uuid",
                            "physical_path",
                            "fixture_request",
                            "fixture_identity",
                        },
                    )
                    self.assertEqual(call.args, (h.capture.text(),))
                    self.assertEqual(
                        call.kwargs["fixture_identity"], h.capture.fixture_identity
                    )
                    self.assertEqual(call.kwargs["platform"], platform)
                manifest = samples.load(bundle.attempt / evidence.MANIFEST)
                self.assertEqual(manifest["status"], "passed")
                self.assertEqual(manifest["scenario"], "write-reject")
                self.assertEqual(manifest["flow_index"]["scenario"], "write-reject")
                self.assertNotIn("flows", manifest["flow_index"])
                self.assertNotIn("cancellation_physical", manifest["references"])
                relocated = self.clone(bundle.attempt, platform + "-relocated")
                shutil.rmtree(bundle.attempt.parent)
                # Extraction timestamps are not evidence; recorded receipt dates are.
                (
                    relocated / "coordination/fixture-session/fixture-request.json"
                ).touch()
                before = (relocated / evidence.MANIFEST).read_bytes()
                with mock.patch.object(
                    evidence.subprocess,
                    "run",
                    side_effect=AssertionError("offline must not execute commands"),
                ):
                    evidence.verify_bundle(relocated / evidence.MANIFEST)
                self.assertEqual((relocated / evidence.MANIFEST).read_bytes(), before)

    def test_missing_required_references_fail_finish_and_rehashed_offline(self):
        for platform in ("android", "ios"):
            bundle, _ = self.ready(platform)
            self.finish(bundle)
            for role, name in evidence._write_reject_names(platform).items():
                with self.subTest(platform=platform, role=role):
                    attempt = self.clone(bundle.attempt, platform + "-missing-" + role)
                    (attempt / name).unlink()
                    samples.rehash(attempt)
                    with self.assertRaises((ValueError, OSError)):
                        evidence.verify_bundle(attempt / evidence.MANIFEST)
                    candidate = copy.copy(bundle)
                    candidate.attempt = attempt
                    candidate.document = copy.deepcopy(bundle.document)
                    with self.assertRaises((ValueError, OSError)):
                        self.finish(candidate)
                    self.assertEqual(
                        samples.load(attempt / evidence.MANIFEST)["status"], "failed"
                    )

    def test_rehashed_ticket_payload_readback_detach_resource_and_control_attacks(self):
        bundle, _ = self.ready()
        self.finish(bundle)
        for fault in (
            "ticket",
            "payload",
            "readback",
            "peer-detach",
            "resource-zero",
            "control",
            "request",
            "receipt",
            "ledger",
        ):
            with self.subTest(fault=fault):
                attempt = self.clone(bundle.attempt, fault)
                path = attempt / "coordination/write-reject-physical.json"
                physical = samples.load(path)
                rows = physical["boundaries"]
                by_name = {row["boundary"]: row for row in rows}
                if fault in ("ticket", "payload"):
                    row = by_name["held"]
                    status = status_from_document(row["status"])
                    status = (
                        replace(
                            status,
                            ticket=replace(
                                status.ticket, sequence=status.ticket.sequence + 1
                            ),
                        )
                        if fault == "ticket"
                        else replace(status, observed=b"\x12")
                    )
                    row["status"] = status_document(status)
                elif fault == "readback":
                    by_name["recovery"]["local"]["value"] = "11"
                elif fault == "peer-detach":
                    next(row for row in reversed(rows) if row["boundary"] == "gap")[
                        "peer"
                    ] = "01665544332212"
                elif fault == "resource-zero":
                    by_name["closed"]["resources"]["lifecycleOwners"] = 1
                elif fault == "control":
                    path = attempt / "coordination/write-reject-control.json"
                    physical = samples.load(path)
                    physical["token"] = "11111111-1111-4111-8111-111111111111"
                elif fault == "request":
                    path = attempt / "coordination/request-reject.json"
                    physical = samples.load(path)
                    physical["flow"] = 8
                elif fault == "receipt":
                    path = attempt / "coordination/fixture-session/fixture-receipt.json"
                    physical = samples.load(path)
                    physical["fixture_pid"] += 1
                else:
                    path = attempt / "write-reject-ledger.json"
                    physical = samples.load(path)
                    physical["rejected_command_id"] = "30012"
                samples.write(path, physical)
                samples.rehash(attempt)
                with self.assertRaises(ValueError):
                    evidence.verify_bundle(attempt / evidence.MANIFEST)

    def test_rehashed_runtime_errors_values_resources_and_close_order_fail(self):
        for platform in ("android", "ios"):
            bundle, _ = self.ready(platform)
            self.finish(bundle)
            for index, (before, after) in enumerate(
                (
                    ('"code": "14"', '"code": "133"'),
                    ('"value": [0]', '"value": [17]'),
                    ('"value": [34]', '"value": [17]'),
                    ('"handoffRoots": 0', '"handoffRoots": 1'),
                    (
                        "rf-write-reject:step:close:passed",
                        "rf-write-reject:step:close:failed",
                    ),
                )
            ):
                with self.subTest(platform=platform, before=before):
                    attempt = self.clone(
                        bundle.attempt, platform + "-runtime-" + str(index)
                    )
                    samples.replace_runtime(attempt, before, after)
                    samples.rehash(attempt)
                    with self.assertRaises(ValueError):
                        evidence.verify_bundle(attempt / evidence.MANIFEST)

            for index, (first, second) in enumerate(
                (
                    ("rf-write-reject:step:close:passed", ":phase=close"),
                    (":phase=close", "rf-write-reject:passed"),
                )
            ):
                with self.subTest(platform=platform, close_order=index):
                    attempt = self.clone(
                        bundle.attempt, platform + "-close-order-" + str(index)
                    )
                    samples.swap_runtime_records(attempt, first, second)
                    samples.rehash(attempt)
                    with self.assertRaises(ValueError):
                        evidence.verify_bundle(attempt / evidence.MANIFEST)

    def test_capture_native_identity_control_lineage_and_install_drift_fail(self):
        for platform in ("android", "ios"):
            bundle, _ = self.ready(platform)
            self.finish(bundle)
            for fault in ("identity", "capture", "ack", "raw", "projection", "install"):
                with self.subTest(platform=platform, fault=fault):
                    attempt = self.clone(bundle.attempt, platform + "-" + fault)
                    if fault == "raw":
                        path = attempt / "write-reject-runtime.raw.log"
                        path.write_bytes(
                            path.read_bytes().replace(
                                b"rf-write-reject:passed", b"rf-write-reject:failed"
                            )
                        )
                    elif fault == "projection":
                        (attempt / "coordination/runtime.log").write_text(
                            "foreign projection\n"
                        )
                    else:
                        name = (
                            "write-reject-runtime.identity.json"
                            if fault == "identity"
                            else "write-reject-runtime.capture.json"
                        )
                        if fault == "install":
                            name = (
                                "artifact-install-identity.json"
                                if platform == "android"
                                else "device-install.json"
                            )
                        path = attempt / name
                        value = samples.load(path)
                        if fault == "identity":
                            value["bootstrap"]["pid"] += 1
                        elif fault == "capture":
                            value["actions"][0]["url"] = "blehub-rn-spike://foreign"
                        elif fault == "ack":
                            value["actions"][0]["js_ack_at"] = (
                                value["actions"][0]["requested_at"] + 3
                            )
                        elif platform == "android":
                            value["apk"] = "candidate/foreign.apk"
                        else:
                            value["result"]["installedApplications"][0][
                                "installationURL"
                            ] = "file:///foreign/"
                        samples.write(path, value)
                    samples.rehash(attempt)
                    with self.assertRaises((ValueError, RuntimeError)):
                        evidence.verify_bundle(attempt / evidence.MANIFEST)

    def test_unrehashed_hash_drift_and_scenario_downgrade_fail(self):
        bundle, _ = self.ready()
        self.finish(bundle)
        for fault in (
            "raw",
            "artifact",
            "ledger",
            "physical",
            "missing-scenario",
            "continuous10",
        ):
            with self.subTest(fault=fault):
                attempt = self.clone(bundle.attempt, "hash-" + fault)
                if fault in ("missing-scenario", "continuous10"):
                    path = attempt / evidence.MANIFEST
                    value = samples.load(path)
                    if fault == "missing-scenario":
                        value.pop("scenario")
                    else:
                        value["scenario"] = "continuous10"
                    samples.write(path, value)
                else:
                    name = {
                        "raw": "write-reject-runtime.raw.log",
                        "artifact": bundle.document["artifact"]["path"],
                        "ledger": "write-reject-ledger.json",
                        "physical": "coordination/write-reject-physical.json",
                    }[fault]
                    with (attempt / name).open("ab") as stream:
                        stream.write(b"drift")
                with self.assertRaises(ValueError):
                    evidence.verify_bundle(attempt / evidence.MANIFEST)

    def truncated_bundle(self, name: str, platform: str = "android"):
        root = self.root / name
        root.mkdir()
        bundle = evidence.EvidenceBundle(
            root / "attempt",
            platform=platform,
            device="synthetic",
            project_root=root,
            scenario="write-reject",
        )
        bundle.prepare_artifact(samples.artifact(root, platform))
        run_id = "12345678-1234-4234-8234-123456789abc"
        log = bundle.attempt / "write-reject-runtime.log"
        log.write_text(
            wire.PREFIX
            + f"rf-write-reject:runtime:session={run_id}:nonce=synthetic:phase=start\n"
            + wire.PREFIX
            + 'rf-write-reject:event:{"runId":"'
            + run_id
            + '","nonce":"synthetic","kind":"gattDiscovered","services":[{"uuid":"truncated\n',
            encoding="utf-8",
        )
        return bundle, log

    def test_truncated_json_retains_failure_diagnostic_and_rejects_pass(self):
        for platform in ("android", "ios"):
            for completed in (False, True):
                with self.subTest(platform=platform, completed=completed):
                    bundle, log = self.truncated_bundle(
                        platform + str(completed), platform
                    )
                    original = log.read_bytes()
                    options = {
                        "completed": completed,
                        "primary_error": None
                        if completed
                        else RuntimeError("pre-ARM truncation"),
                        "cleanup_errors": [],
                        "runtime_log": log,
                    }
                    if completed:
                        with self.assertRaisesRegex(
                            evidence.EvidenceError,
                            "incomplete/invalid write-reject runtime",
                        ):
                            bundle.finish(**options)
                    else:
                        bundle.finish(**options)
                    manifest = bundle.attempt / evidence.MANIFEST
                    value = samples.load(manifest)
                    self.assertEqual(value["status"], "failed")
                    index = value["flow_index"]
                    self.assertEqual(index["scenario"], "write-reject")
                    self.assertIsNone(index["facts"])
                    self.assertEqual(len(index["error_markers"]), 1)
                    self.assertIn(
                        "malformed session JSON: Unterminated string",
                        index["error_markers"][0],
                    )
                    self.assertIsNotNone(index["sha256"])
                    self.assertEqual(index["size"], len(original))
                    if not completed:
                        self.assertEqual(
                            value["primary_error"]["message"], "pre-ARM truncation"
                        )
                    before = manifest.read_bytes()
                    evidence.verify_bundle(manifest)
                    self.assertEqual(manifest.read_bytes(), before)
                    self.assertEqual(log.read_bytes(), original)
                    # Relabelling a diagnosed truncated capture cannot pass offline.
                    value.update(
                        status="passed",
                        completed=True,
                        primary_error=None,
                        cleanup_errors=[],
                        evidence_errors=[],
                    )
                    samples.write(manifest, value)
                    with self.assertRaisesRegex(
                        evidence.EvidenceError,
                        "incomplete/invalid write-reject runtime",
                    ):
                        evidence.verify_bundle(manifest)

    def test_historical_null_index_requires_exact_replayed_malformed_json_failure(self):
        bundle, log = self.truncated_bundle("legacy")
        index = evidence._write_reject_index(log, log.name)
        diagnostic = index["error_markers"][0]
        # Reproduce the old finish failure: RHS raises before flow_index assignment.
        with (
            mock.patch.object(
                evidence,
                "_write_reject_index",
                side_effect=FixtureSessionError(diagnostic),
            ),
            self.assertRaises(FixtureSessionError),
        ):
            bundle.finish(
                completed=False,
                primary_error=FixtureSessionError(diagnostic),
                cleanup_errors=[],
                runtime_log=log,
            )
        manifest = bundle.attempt / evidence.MANIFEST
        original = manifest.read_bytes()
        value = samples.load(manifest)
        self.assertIsNone(value["flow_index"])
        self.assertEqual(
            value["evidence_errors"], ["finish: FixtureSessionError: " + diagnostic]
        )
        evidence.verify_bundle(manifest)
        self.assertEqual(manifest.read_bytes(), original)
        for fault in ("diagnostic", "index", "completed", "passed", "runtime", "hash"):
            with self.subTest(fault=fault):
                attempt = self.clone(bundle.attempt, "legacy-" + fault)
                path = attempt / evidence.MANIFEST
                changed = samples.load(path)
                if fault == "diagnostic":
                    changed["evidence_errors"] = [
                        "finish: FixtureSessionError: malformed session JSON: foreign"
                    ]
                elif fault == "index":
                    changed["flow_index"] = evidence._empty_index(
                        log.name, "write-reject"
                    )
                elif fault == "completed":
                    changed["completed"] = True
                elif fault == "passed":
                    changed.update(
                        status="passed",
                        completed=True,
                        primary_error=None,
                        evidence_errors=[],
                    )
                elif fault == "runtime":
                    runtime = attempt / log.name
                    runtime.write_text(runtime.read_text().splitlines(keepends=True)[0])
                    samples.rehash(attempt)
                    changed = samples.load(path)
                    changed["flow_index"] = None
                else:
                    with (attempt / log.name).open("ab") as stream:
                        stream.write(b"drift")
                samples.write(path, changed)
                with self.assertRaises(evidence.EvidenceError):
                    evidence.verify_bundle(path)

    def test_runtime_index_does_not_swallow_unexpected_runtime_errors(self):
        _, log = self.truncated_bundle("unexpected")
        for error in (RuntimeError("unexpected parser defect"), OSError("IO failed")):
            with (
                self.subTest(error=error),
                mock.patch.object(
                    write_reject_ledger, "runtime_facts", side_effect=error
                ),
                self.assertRaises(type(error)),
            ):
                evidence._write_reject_index(log, log.name)

    def test_failed_partial_attempt_never_indexes_continuous10_or_hides_primary(self):
        bundle = evidence.EvidenceBundle(
            self.root / "partial",
            platform="android",
            device="synthetic",
            project_root=self.root,
            scenario="write-reject",
        )
        for has_log in (False, True):
            with self.subTest(has_log=has_log):
                log = bundle.attempt / "write-reject-runtime.log"
                if has_log:
                    log.write_text(wire.PREFIX + "rf-write-reject:failed:synthetic\n")
                bundle.finish(
                    completed=False,
                    primary_error=RuntimeError("primary"),
                    cleanup_errors=["cleanup"],
                    runtime_log=log,
                )
                value = samples.load(bundle.attempt / evidence.MANIFEST)
                self.assertEqual(value["status"], "failed")
                self.assertEqual(value["primary_error"]["message"], "primary")
                self.assertEqual(value["cleanup_errors"], ["cleanup"])
                self.assertEqual(value["flow_index"]["scenario"], "write-reject")
                self.assertNotIn("flows", value["flow_index"])
                evidence.verify_bundle(bundle.attempt / evidence.MANIFEST)
