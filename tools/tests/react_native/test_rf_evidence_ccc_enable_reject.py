"""CCC bundle replay attacks over real provider output, not RF authorization."""

from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.peripherals.nrf52840 import ccc_enable_reject as wire
from tests.peripherals.nrf52840.fault_control import (
    status_document,
    status_from_document,
)
from tools.react_native import ccc_enable_reject_ledger
from tools.react_native import rf_evidence_bundle as evidence
from tools.tests.react_native.support import evidence_ccc_enable_reject as samples
from tools.tests.react_native.support.ccc_reject_evidence_checks import (
    assert_native_signals_reparsed,
)


class CccEnableRejectEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.seeds = {}
        for platform in ("android", "ios"):
            bundle, h = samples.ready(Path(temporary.name) / platform, platform)
            cls.finish(bundle)
            cls.seeds[platform] = bundle, h

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.count = 0

    @staticmethod
    def finish(bundle):
        bundle.finish(
            completed=True,
            primary_error=None,
            cleanup_errors=[],
            runtime_log=bundle.attempt / samples.LOG,
        )

    def clone(self, platform="android"):
        self.count += 1
        seed, _ = self.seeds[platform]
        bundle = copy.copy(seed)
        bundle.document = copy.deepcopy(seed.document)
        bundle.attempt = self.root / str(self.count)
        shutil.copytree(seed.attempt, bundle.attempt)
        return bundle

    def rejected(self, bundle, *, rehash=True, finish=False):
        if rehash:
            samples.rehash(bundle.attempt)
        with self.assertRaises((ValueError, OSError)):
            evidence.verify_bundle(bundle.attempt / evidence.MANIFEST)
        if finish:
            with self.assertRaises((ValueError, OSError)):
                self.finish(bundle)
            self.assertEqual(
                samples.load(bundle.attempt / evidence.MANIFEST)["status"], "failed"
            )

    def test_exact_ledger_interface_explicit_dispatch_and_portable_readonly_replay(
        self,
    ):
        for platform in self.seeds:
            with self.subTest(platform=platform):
                bundle = self.clone(platform)
                _, h = self.seeds[platform]
                with mock.patch.object(
                    ccc_enable_reject_ledger,
                    "verify",
                    wraps=ccc_enable_reject_ledger.verify,
                ) as verify:
                    self.finish(bundle)
                self.assertGreaterEqual(verify.call_count, 2)
                for call in verify.call_args_list:
                    self.assertEqual(call.args, (h.capture.text(),))
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
                    self.assertEqual(
                        call.kwargs["fixture_identity"], h.capture.fixture_identity
                    )
                    self.assertEqual(call.kwargs["platform"], platform)
                doc = samples.load(bundle.attempt / evidence.MANIFEST)
                self.assertEqual(doc["scenario"], wire.SCENARIO)
                self.assertEqual(doc["flow_index"]["scenario"], wire.SCENARIO)
                self.assertNotIn("flows", doc["flow_index"])
                self.assertNotIn("cancellation_physical", doc["references"])
                for name in ("write-reject", "continuous10"):
                    self.assertNotIn(name, doc["references"])
                destination = self.root / (platform + "-relocated")
                shutil.move(bundle.attempt, destination)
                manifest = destination / evidence.MANIFEST
                before = manifest.read_bytes()
                # Hide the original attempt AND build source during relocated replay.
                seed, _ = self.seeds[platform]
                original = seed.attempt.parent
                hidden = original.with_name(platform + "-hidden")
                original.rename(hidden)
                self.addCleanup(hidden.rename, original)
                # Original absolute paths must be labels only, never replay inputs.
                with mock.patch.object(
                    evidence.subprocess,
                    "run",
                    side_effect=AssertionError("offline subprocess"),
                ):
                    evidence.verify_bundle(manifest)
                self.assertEqual(manifest.read_bytes(), before)

    def test_every_required_reference_missing_fails_finish_and_rehashed_offline(self):
        for platform in self.seeds:
            for role, name in evidence._ccc_enable_reject_names(platform).items():
                with self.subTest(platform=platform, role=role):
                    bundle = self.clone(platform)
                    (bundle.attempt / name).unlink()
                    self.rejected(bundle, finish=True)

    def test_all_seven_native_identities_are_reparsed_not_trusted_from_sidecars(self):
        assert_native_signals_reparsed(self, samples, wire.PHASES)

    def test_each_request_signal_control_and_sdk_receipt_binding(self):
        for platform in self.seeds:
            for index, (request, phase) in enumerate(
                zip(wire.REQUESTS, wire.PHASES, strict=True)
            ):
                for fault in (
                    "request",
                    "signal",
                    "action",
                    "sdk-exit",
                    "deadline",
                    "sdk-output",
                    "receipt",
                ):
                    if fault == "receipt" and platform != "ios":
                        continue
                    with self.subTest(platform=platform, phase=phase, fault=fault):
                        bundle = self.clone(platform)
                        root = bundle.attempt
                        if fault in ("request", "signal"):
                            name = (
                                f"request-{request}"
                                if fault == "request"
                                else f"signal-{phase}"
                            )
                            path = root / "coordination" / (name + ".json")
                            doc = samples.load(path)
                            doc["token"] = doc["session"]
                        elif fault == "sdk-output":
                            (
                                root
                                / f"ccc-enable-reject-runtime.control-{index}-sdk-output.log"
                            ).write_text("Error: SDK rejected\n")
                            self.rejected(bundle)
                            continue
                        elif fault == "receipt":
                            path = (
                                root
                                / f"ccc-enable-reject-runtime.control-{index}-notification.json"
                            )
                            doc = samples.load(path)
                            args = doc["info"]["arguments"]
                            args[args.index("--name") + 1] = "foreign.notification"
                        else:
                            path = root / "ccc-enable-reject-runtime.capture.json"
                            doc = samples.load(path)
                            action = doc["actions"][index]
                            if fault == "action":
                                action["control"]["flow"] = 10
                            elif fault == "sdk-exit":
                                action["sdk_exit"] = 1
                            else:
                                action["js_ack_at"] = action["requested_at"] + 2
                        samples.write(path, doc)
                        self.rejected(bundle)

    def test_runtime_subscription_error_payload_delivery_resources_and_silence_fences(
        self,
    ):
        replacements = (
            ('"code": "14"', '"code": "133"'),
            ('"subscriptionId": "33012"', '"subscriptionId": "33009"'),
            ('"value": [239]', '"value": [240]'),
            (
                '"actualNotificationMode": "Notification"',
                '"actualNotificationMode": "Indication"',
            ),
            ('"subscriptions": 0', '"subscriptions": 1'),
            ('"handoffRoots": 0', '"handoffRoots": 1'),
            ("elapsedMs=1000", "elapsedMs=999"),
            ("step:end-silence-read:passed", "step:end-silence-read:failed"),
            ("step:close:passed", "step:close:failed"),
        )
        for platform in self.seeds:
            for before, after in replacements:
                with self.subTest(platform=platform, before=before):
                    bundle = self.clone(platform)
                    samples.edit_runtime(
                        bundle.attempt,
                        lambda text, before=before, after=after: text.replace(
                            before, after
                        ),
                    )
                    self.rejected(bundle)
            for fault in (
                "missing-delivery",
                "js-first",
                "missing-native",
                "closed-resources",
            ):
                with self.subTest(platform=platform, fault=fault):
                    bundle = self.clone(platform)
                    physical = samples.load(bundle.attempt / samples.PHYSICAL)
                    submission = next(
                        r
                        for r in physical["boundaries"]
                        if r["boundary"] == "value-sent"
                    )["submission"]
                    self.assertEqual(submission["result"], "accepted")

                    def attack(text, fault=fault):
                        lines = text.splitlines(keepends=True)
                        if fault == "missing-delivery":
                            return "".join(
                                line
                                for line in lines
                                if '"kind": "notificationReceived"' not in line
                            )
                        if fault == "missing-native":
                            return "".join(
                                line for line in lines if "rf-host:signal:" not in line
                            )
                        if fault == "closed-resources":
                            return "".join(
                                line.replace('"workerThreads": 0', '"workerThreads": 1')
                                if "resources:phase=closed:" in line
                                else line
                                for line in lines
                            )
                        for i, line in enumerate(lines):
                            if "rf-host:signal:" in line and i + 1 < len(lines):
                                lines[i], lines[i + 1] = lines[i + 1], line
                                break
                        return "".join(lines)

                    samples.edit_runtime(bundle.attempt, attack)
                    self.rejected(bundle)

    def test_physical_ticket_topology_submission_silence_deadlines_and_prefix_hashes(
        self,
    ):
        for fault in (
            "ticket",
            "topology",
            "payload",
            "silence",
            "deadline",
            "prefix",
            "peer",
            "ledger",
        ):
            with self.subTest(fault=fault):
                bundle = self.clone()
                path = bundle.attempt / samples.PHYSICAL
                doc = samples.load(path)
                rows = {r["boundary"]: r for r in doc["boundaries"]}
                if fault == "ticket":
                    status = status_from_document(rows["held"]["status"])
                    rows["held"]["status"] = status_document(
                        replace(
                            status,
                            ticket=replace(
                                status.ticket, sequence=status.ticket.sequence + 1
                            ),
                        )
                    )
                elif fault == "topology":
                    doc["target_handle"] = doc["value_handle"]
                elif fault == "payload":
                    rows["value-sent"]["submission"]["value"] = "f0"
                elif fault == "silence":
                    rows["silence-started"]["silence_started"] = rows["close-allowed"][
                        "monotonic"
                    ]
                elif fault == "deadline":
                    doc["deadline"] = rows["closed"]["monotonic"]
                elif fault == "prefix":
                    rows["disable-allowed"]["runtime_sha256"] = "0" * 64
                elif fault == "peer":
                    rows["closed"]["peer_count"] = 1
                else:
                    path = bundle.attempt / samples.LEDGER
                    doc = samples.load(path)
                    doc["scenario"] = "write-reject"
                samples.write(path, doc)
                self.rejected(bundle)

    def test_install_candidate_identity_sidecars_and_unrehashed_hash_drift(self):
        for platform in self.seeds:
            for fault in (
                "install",
                "candidate",
                "identity",
                "capture",
                "control",
                "raw",
            ):
                with self.subTest(platform=platform, fault=fault):
                    bundle = self.clone(platform)
                    root = bundle.attempt
                    if fault == "candidate":
                        candidate = root / bundle.document["artifact"]["path"]
                        target = (
                            candidate
                            if platform == "android"
                            else candidate / "main.jsbundle"
                        )
                        target.write_bytes(b"wrong candidate")
                    elif fault == "raw":
                        with (root / "ccc-enable-reject-runtime.raw.log").open(
                            "ab"
                        ) as stream:
                            stream.write(b"drift")
                        self.rejected(bundle, rehash=False)
                        continue
                    else:
                        name = {
                            "identity": "ccc-enable-reject-runtime.identity.json",
                            "capture": "ccc-enable-reject-runtime.capture.json",
                            "control": "coordination/ccc-enable-reject-control.json",
                            "install": "artifact-install-identity.json"
                            if platform == "android"
                            else "device-install.json",
                        }[fault]
                        path = root / name
                        doc = samples.load(path)
                        if fault == "install":
                            if platform == "android":
                                doc["apk"] = "candidate/foreign.apk"
                            else:
                                doc["info"]["arguments"][-1] = (
                                    "/mutable/build/BleHubNitroHost.app"
                                )
                        elif fault == "control":
                            doc["flow"] = 10
                        elif fault == "identity":
                            doc["bootstrap"]["pid"] = 43
                        else:
                            doc["native_acknowledged"] = []
                        samples.write(path, doc)
                    self.rejected(bundle)
            for role, name in evidence._ccc_enable_reject_names(platform).items():
                with self.subTest(platform=platform, hash_role=role):
                    bundle = self.clone(platform)
                    (bundle.attempt / name).chmod(0o600)
                    with (bundle.attempt / name).open("ab") as stream:
                        stream.write(b"drift")
                    self.rejected(bundle, rehash=False)

    def test_empty_partial_truncated_and_scenario_downgrade_never_fall_back(self):
        for platform in self.seeds:
            for state in ("absent", "partial", "truncated"):
                with self.subTest(platform=platform, state=state):
                    bundle = self.clone(platform)
                    log = bundle.attempt / samples.LOG
                    original = log.read_text()
                    if state == "absent":
                        log.unlink()
                    elif state == "partial":
                        log.write_text(
                            original[
                                : original.index("rf-ccc-enable-reject:step:initialize")
                            ]
                        )
                    else:
                        log.write_text(
                            original.splitlines(keepends=True)[0]
                            + wire.PREFIX
                            + "rf-ccc-enable-reject:runtime:session=12345678-1234-4234-8234-123456789abc:nonce=x:phase=start\n"
                            + wire.PREFIX
                            + 'rf-ccc-enable-reject:event:{"runId":"truncated\n'
                        )
                    bundle.finish(
                        completed=False,
                        primary_error=RuntimeError("original failure"),
                        cleanup_errors=[],
                        runtime_log=log,
                    )
                    manifest = bundle.attempt / evidence.MANIFEST
                    doc = samples.load(manifest)
                    self.assertEqual(doc["status"], "failed")
                    self.assertEqual(doc["flow_index"]["scenario"], wire.SCENARIO)
                    self.assertNotIn("flows", doc["flow_index"])
                    before = manifest.read_bytes()
                    evidence.verify_bundle(manifest)
                    self.assertEqual(before, manifest.read_bytes())
                    doc.update(
                        status="passed",
                        completed=True,
                        primary_error=None,
                        evidence_errors=[],
                    )
                    samples.write(manifest, doc)
                    with self.assertRaises(ValueError):
                        evidence.verify_bundle(manifest)
            for scenario in (None, "continuous10", "write-reject", "missing"):
                bundle = self.clone(platform)
                path = bundle.attempt / evidence.MANIFEST
                doc = samples.load(path)
                if scenario == "missing":
                    doc.pop("scenario")
                else:
                    doc["scenario"] = scenario
                samples.write(path, doc)
                with self.assertRaises(ValueError):
                    evidence.verify_bundle(path)


if __name__ == "__main__":
    unittest.main()
