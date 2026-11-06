"""Synthetic integrity fixtures only: none of these tests is RF evidence."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from tests.support.rf_lab.markers import SERVICE as ADAPTER_SERVICE
from tools.react_native import rf_evidence_bundle as evidence
from tools.react_native.host_contract import BUNDLE_ID
from tools.tests.react_native.support.adapter_transition_samples import (
    BOOTSTRAP as ADAPTER_BOOTSTRAP,
)
from tools.tests.react_native.support.adapter_transition_samples import (
    LOG as ADAPTER_LOG,
)
from tools.tests.react_native.support.adapter_transition_samples import (
    RUN as ADAPTER_RUN,
)
from tools.tests.react_native.support.adapter_transition_samples import (
    active_bodies,
    active_physical_sidecars,
    physical_sidecars,
    state_control,
)
from tools.tests.react_native.support.adapter_transition_samples import (
    transcript as adapter_transcript,
)
from tools.tests.react_native.support.evidence_runtime import SESSION, recovery_runtime
from tools.tests.react_native.support.full_suite_ledger import SuiteFixture


class EvidenceBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "not-a-git-project"
        self.project.mkdir()
        self.attempt = self.root / "attempt"
        self.log = self.attempt / "runtime.log"

    def bundle(
        self, platform: str = "android", *, scenario: str = "continuous10"
    ) -> evidence.EvidenceBundle:
        return evidence.EvidenceBundle(
            self.attempt,
            platform=platform,
            device="synthetic-device-not-RF",
            project_root=self.project,
            scenario=scenario,
        )

    def apk(
        self,
        *,
        extra: list[tuple[str | zipfile.ZipInfo, bytes]] | None = None,
        omit: str | None = None,
    ) -> Path:
        path = self.root / "synthetic.apk"
        files = [
            ("assets/index.android.bundle", b"fake JS; not RF"),
            ("classes.dex", b"fake DEX; not RF"),
            ("classes2.dex", b"second fake DEX"),
            ("lib/arm64-v8a/libblehub_uniffi.so", b"fake UniFFI; not RF"),
            ("lib/arm64-v8a/libBleHubReactNative.so", b"fake Nitro; not RF"),
        ]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w") as archive:
                for name, content in [*files, *(extra or [])]:
                    if name != omit:
                        archive.writestr(name, content)
        return path

    def app(self) -> Path:
        path = self.root / "Synthetic.app"
        path.mkdir()
        (path / "Info.plist").write_bytes(
            plistlib.dumps(
                {"CFBundleExecutable": "Synthetic", "CFBundleIdentifier": BUNDLE_ID}
            )
        )
        (path / "main.jsbundle").write_bytes(b"fake JS; not RF")
        # Magic only, not an executable program or a symbol/linking assertion.
        (path / "Synthetic").write_bytes(
            bytes.fromhex("cffaedfe") + b"fake static native"
        )
        (path / "Synthetic").chmod(0o755)
        (path / "Resources").mkdir(mode=0o755)
        (path / "Resources" / "asset").write_bytes(b"fake resource")
        return path

    def runtime(self, *, count: int = 10, close: bool = True) -> bytes:
        lines = [f"runtime:session={SESSION}:nonce=synthetic-runtime:phase=start"]
        for index, mode in enumerate(evidence.MODES[:count]):
            lines.extend(
                [
                    f"flow-started:index={index}:mode={mode}",
                    "synthetic-inner-marker:not-physical-evidence",
                    f"flow-passed:index={index}:mode={mode}",
                ]
            )
        if close:
            lines.extend(
                [
                    f"runtime:session={SESSION}:nonce=synthetic-runtime:phase=close",
                    "passed:flows=10:passedCount=10:failureCount=0",
                ]
            )
        # Both platform captures may have native/timestamp prefixes. Hash raw bytes.
        return "".join(
            f"timestamp native-prefix {evidence.PREFIX}{line}\r\n" for line in lines
        ).encode()

    def required(
        self,
        platform: str = "android",
        *,
        ios_attempt: bool = False,
        scenario: str = "continuous10",
    ) -> None:
        identity_log = self.log
        if ios_attempt:
            identity_log = self.log.with_name(self.log.stem + ".attempt.log")
            identity_log.write_bytes(self.log.read_bytes())
        suffix = (
            ".identity.json"
            if platform == "android" or scenario == "recovery-ten"
            else ".rf-host-identity.json"
        )
        paths = [
            identity_log.with_suffix(suffix),
            self.attempt / "coordination/fixture-session/fixture-request.json",
            self.attempt / "coordination/fixture-session/fixture-receipt.json",
        ]
        if scenario == "pending-read-reload":
            paths.append(self.attempt / "coordination/pending-reload-physical.json")
        elif scenario == "recovery-ten":
            paths.extend(
                [
                    self.attempt / "coordination/recovery-ten-physical.json",
                    self.attempt / "recovery-ten-ledger.json",
                ]
            )
        else:
            paths.extend(
                [
                    self.attempt
                    / "coordination/flow-8-rf-cancellation/physical-evidence.json",
                    self.attempt
                    / "coordination/flow-9-rf-watchdog/physical-evidence.json",
                ]
            )
        if platform == "android" or scenario == "recovery-ten":
            paths.append(self.log.with_suffix(".capture.json"))
        if platform == "android":
            paths.append(self.attempt / "artifact-install-identity.json")
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Bundle checks required references, NOT physical RF field semantics.
            path.write_text('{"synthetic": "not RF evidence"}\n', encoding="utf-8")

    def finish(
        self,
        bundle: evidence.EvidenceBundle,
        *,
        completed: bool = True,
        error: BaseException | None = None,
        cleanup: list[str] | None = None,
    ) -> None:
        bundle.finish(
            completed=completed,
            primary_error=error,
            cleanup_errors=cleanup or [],
            runtime_log=self.log,
        )

    def manifest(self) -> dict:
        return json.loads(
            (self.attempt / evidence.MANIFEST).read_text(encoding="utf-8")
        )

    def ready(self, platform: str = "android") -> tuple[evidence.EvidenceBundle, Path]:
        bundle = self.bundle(platform)
        snapshot = bundle.prepare_artifact(
            self.apk() if platform == "android" else self.app()
        )
        self.log.write_bytes(self.runtime())
        self.required(platform)
        return bundle, snapshot

    def permission_ready(self, *, use_root: bool = False) -> evidence.EvidenceBundle:
        """Real software owners + original journal bytes, all SDK/OS calls fake."""
        from tools.android import state_control
        from tools.tests.react_native.support import permission_host_samples as samples

        bundle = evidence.EvidenceBundle(
            self.attempt,
            platform="android",
            device=samples.DEVICE,
            project_root=self.project,
            scenario="permission-transition",
        )
        snapshot = bundle.prepare_artifact(self.apk())
        with contextlib.ExitStack() as stack:
            h = samples.Harness(self.attempt, stack, use_root=use_root)
            self.log = h.log
            contract = b'{"synthetic":"contract, not attestation"}\n'
            profile = (
                json.dumps({"advertising": {"local_name": samples.NAME}}).encode()
                + b"\n"
            )
            request = {
                "schema_version": 1,
                "session_id": samples.RUN,
                "contract_sha256": hashlib.sha256(contract).hexdigest(),
                "profile_sha256": hashlib.sha256(profile).hexdigest(),
                "resource_identity": "synthetic:fixture",
                "service_uuid": ADAPTER_SERVICE,
                "port": "/dev/synthetic-not-a-device",
            }
            receipt = dict(
                request,
                proof_method="btp-vendor-live-under-device-lease",
                fixture_pid=12345,
                fixture_pgid=12345,
            )
            receipt_raw = (json.dumps(receipt, indent=3) + "\n").encode()
            h.receipt.write_bytes(receipt_raw)
            h.capture.receipt_raw = receipt_raw
            h.capture.receipt_sha256 = hashlib.sha256(receipt_raw).hexdigest()
            h.capture.run()
            runtime = h.verify()
            _, _, physical = h.physical(runtime)
            physical.update(
                {
                    key: request[key]
                    for key in (
                        "contract_sha256",
                        "profile_sha256",
                        "resource_identity",
                    )
                }
            )
            self.assertEqual(h.permission.finish(None), [])
            session = h.root / "fixture-session"
            (session / "contract.json").write_bytes(contract)
            (session / "profile.json").write_bytes(profile)
            (session / "fixture-request.json").write_text(json.dumps(request))
            (h.root / "permission-control.json").write_text(
                json.dumps({"schema_version": 1, "run_id": samples.RUN})
            )
            (h.root / "permission-transition-physical.json").write_text(
                json.dumps(physical)
            )
            (self.attempt / "permission-transition-ledger.json").write_text(
                json.dumps(runtime)
            )
            for entry in h.capture.result["phases"].values():
                identity = {
                    "schema_version": 1,
                    "device_serial": samples.DEVICE,
                    "package_name": evidence.ANDROID_PACKAGE,
                    "boot_budget_seconds": 4.0,
                    "boot_elapsed_seconds": 0.05,
                    "status": "pinned",
                    "pid": str(entry["pid"]),
                    "observations": [
                        {
                            "elapsed_seconds": 0.05,
                            "exit_code": 0,
                            "output": str(entry["pid"]),
                        }
                    ],
                }
                (self.attempt / entry["pid_receipt"]).write_text(json.dumps(identity))
            from tools.react_native.host_contract import ANDROID_ACTIVITY

            installed = {
                "schema_version": 1,
                "status": "passed",
                "apk": snapshot.relative_to(self.attempt).as_posix(),
                "method": "pinned-sdk-aapt2-dump-badging",
                "package_names": [evidence.ANDROID_PACKAGE],
                "launchable_activities": [ANDROID_ACTIVITY],
                "expected_package": evidence.ANDROID_PACKAGE,
                "expected_activity": ANDROID_ACTIVITY,
            }
            (self.attempt / "artifact-install-identity.json").write_text(
                json.dumps(installed)
            )
            (self.attempt / "permission-setup-replacement.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": samples.RUN,
                        "package": evidence.ANDROID_PACKAGE,
                        "method": "replace-old-known-host-under-phone-lease",
                        "status": "completed",
                        "stdout": "synthetic replacement, not device evidence",
                    }
                )
            )
            radio = state_control.AndroidRadioController(
                samples.DEVICE,
                samples.RUN,
                self.attempt / "permission-radio-control.json",
                lock_root=self.root / "synthetic-phone-leases",
            )
            with (
                mock.patch.object(
                    state_control, "adb_executable", return_value=Path("/sdk/adb")
                ),
                mock.patch.object(
                    state_control.subprocess,
                    "run",
                    side_effect=lambda argv, **kwargs: subprocess.CompletedProcess(
                        argv, 0, "Bluetooth Status\n    state: ON\n"
                    ),
                ),
            ):
                radio.acquire()
                radio.initial()
                self.assertEqual(radio.finish_observation(None), [])
        return bundle

    def test_root_permission_bundle_replays_original_execution_identity(self) -> None:
        bundle = self.permission_ready(use_root=True)
        self.finish(bundle)
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        self.assertEqual(self.manifest()["status"], "passed")
        control = json.loads(
            (self.attempt / "permission-state-control.json").read_text()
        )
        self.assertEqual(control["permission_command_mode"], "root")
        self.assertEqual(
            control["actions"][0]["receipts"][0]["command"][4:], ["su", "-c", "id"]
        )

    def test_permission_bundle_original_proofs_three_phases_and_relocation(
        self,
    ) -> None:
        bundle = self.permission_ready()
        self.finish(bundle)
        manifest = self.attempt / evidence.MANIFEST
        evidence.verify_bundle(manifest)
        document = self.manifest()
        self.assertEqual(document["status"], "passed")
        self.assertEqual(
            {role: ref["path"] for role, ref in document["references"].items()},
            {
                "runtime_capture": "permission-transition-runtime.result.json",
                "raw_runtime": "permission-transition-runtime.raw.log",
                "runtime_projection": "permission-transition-runtime.log",
                "permission_projection": "coordination/permission-transition-runtime.log",
                "permission_state_control": "permission-state-control.json",
                "radio_state_control": "permission-radio-control.json",
                "os_death_proof": "permission-transition-os-death-proof.json",
                "os_exit_info": "permission-transition-exit-info.raw.log",
                "ledger_permission_transition": "permission-transition-ledger.json",
                "setup_replacement": "permission-setup-replacement.json",
                "artifact_install_identity": "artifact-install-identity.json",
                "fixture_request": "coordination/fixture-session/fixture-request.json",
                "fixture_receipt": "coordination/fixture-session/fixture-receipt.json",
                "fixture_contract": "coordination/fixture-session/contract.json",
                "fixture_profile": "coordination/fixture-session/profile.json",
                "permission_control": "coordination/permission-control.json",
                "physical_permission_transition": "coordination/permission-transition-physical.json",
                **{
                    "phase_identity_"
                    + phase: f"permission-transition-{phase}.identity.json"
                    for phase in ("denied", "active", "restored")
                },
                **{
                    "permission_"
                    + name.replace("-", "_"): f"coordination/permission-{name}.json"
                    for name in (
                        "active-go",
                        "arm-ready",
                        "revoke-ready",
                        "revoke-request",
                        "revoke-authorized",
                        "loss-confirmed",
                        "restored-ready",
                    )
                },
            },
        )
        raw = (self.attempt / "permission-transition-runtime.raw.log").read_bytes()
        lines = raw.splitlines(keepends=True)
        flows = document["flow_index"]["flows"]
        self.assertEqual(
            [row["phase"] for row in flows], ["denied", "active", "restored"]
        )
        # Indexing alone never infers an OS death or a successful RF phase.
        self.assertEqual([row["status"] for row in flows], ["partial"] * 3)
        for row in flows:
            segment = b"".join(lines[row["start_line"] - 1 : row["end_line"]])
            self.assertEqual(row["sha256"], hashlib.sha256(segment).hexdigest())
            self.assertEqual(row["size"], len(segment))
            self.assertEqual(row["capture_phase"]["pid"], row["pid"])
        self.assertIsNone(flows[1]["capture_phase"]["closed_resources"])
        boundaries = document["flow_index"]["host_boundaries"]
        self.assertEqual(len(boundaries), 5)
        for row in boundaries:
            self.assertEqual(
                row["raw_after_line"],
                document["flow_index"]["events"][row["after_marker"] - 1]["line"],
            )
        relocated = self.root / "relocated"
        shutil.copytree(self.attempt, relocated)
        shutil.rmtree(self.attempt)
        from tools.react_native import permission_transition_ledger as ledger

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    evidence.subprocess,
                    "run",
                    side_effect=AssertionError(
                        "offline proof must not execute host/device commands"
                    ),
                )
            )
            replay = {
                name: stack.enter_context(
                    mock.patch.object(ledger, name, wraps=getattr(ledger, name))
                )
                for name in (
                    "verify_runtime",
                    "verify_death",
                    "verify_physical",
                    "verify_permission_control",
                    "verify_state_control",
                )
            }
            evidence.verify_bundle(relocated / evidence.MANIFEST)
        calls = replay["verify_runtime"].call_args_list
        self.assertEqual(
            [call.args[3] for call in calls],
            [
                "permission-transition-runtime.log",
                "permission-transition-runtime.raw.log",
            ],
        )
        for call in calls:
            self.assertEqual(call.args[0], (relocated / call.args[3]).read_text())
            self.assertEqual(
                call.kwargs["death_proof"],
                (relocated / "permission-transition-os-death-proof.json").read_bytes(),
            )
            self.assertEqual(
                call.kwargs["os_raw"],
                (relocated / "permission-transition-exit-info.raw.log").read_bytes(),
            )
        self.assertEqual(replay["verify_death"].call_count, 2)
        for name in (
            "verify_physical",
            "verify_permission_control",
            "verify_state_control",
        ):
            replay[name].assert_called_once()

    def _refresh_permission_manifest(self) -> None:
        """Rehash changed submissions to test semantics, not merely inventory drift."""
        document = self.manifest()
        document["files"] = evidence._inventory(self.attempt)
        document["references"] = evidence._references(
            document["files"],
            "android",
            document["runtime_log"],
            "permission-transition",
        )
        document["flow_index"] = evidence._flow_index(
            self.log, document["runtime_log"], "permission-transition"
        )
        (self.attempt / evidence.MANIFEST).write_text(json.dumps(document))

    def test_permission_rehashed_raw_os_uid_phase_and_missing_physical_reject(
        self,
    ) -> None:
        for fault in (
            "raw",
            "raw-reprojected",
            "reason10",
            "uid",
            "stale-phase",
            "native-start",
            "phase-pid",
            "missing-physical",
            "dead-zero",
            "journal-prefix",
            "proof-original-sha",
        ):
            with self.subTest(fault=fault):
                self.attempt = self.root / fault
                bundle = self.permission_ready()
                self.finish(bundle)
                result_path = self.attempt / "permission-transition-runtime.result.json"
                proof_path = self.attempt / "permission-transition-os-death-proof.json"
                if fault in ("raw", "raw-reprojected"):
                    path = self.attempt / "permission-transition-runtime.raw.log"
                    original = path.read_bytes()
                    mutated = original.replace(
                        b'"scanAuthorization": "Denied"',
                        b'"scanAuthorization": "Authorized"',
                        1,
                    )
                    self.assertNotEqual(mutated, original)
                    path.write_bytes(mutated)
                    if fault == "raw-reprojected":
                        from tools.react_native import (
                            permission_transition_ledger as ledger,
                        )

                        # Refresh every projection/hash so only semantic replay,
                        # not inventory or raw/projection inequality, can reject it.
                        projected = "".join(
                            line + "\n" for line in ledger.markers(mutated.decode())
                        )
                        self.log.write_text(projected)
                        (self.attempt / "coordination" / self.log.name).write_text(
                            projected
                        )
                        saved_path = self.attempt / "permission-transition-ledger.json"
                        saved = json.loads(saved_path.read_bytes())
                        saved["runtime_sha256"] = hashlib.sha256(
                            projected.encode()
                        ).hexdigest()
                        saved_path.write_text(json.dumps(saved))
                elif fault in ("reason10", "uid"):
                    from tools.android.permission_control import parse_exit_history

                    proof = json.loads(proof_path.read_bytes())
                    os_path = self.attempt / "permission-transition-exit-info.raw.log"
                    dump = os_path.read_text()
                    if fault == "reason10":
                        dump = dump.replace(
                            "reason=8 (PERMISSION CHANGE)", "reason=10 (USER REQUESTED)"
                        )
                    else:
                        dump = dump.replace("realUid=10234", "realUid=10235")
                    os_path.write_text(dump)
                    death = proof["actions"][-1]
                    records = parse_exit_history(dump, "+0000")
                    death["exit_records"], death["exit_record"] = records, records[0]
                    for receipt in death["receipts"]:
                        if receipt["command"][4:7] == [
                            "dumpsys",
                            "activity",
                            "exit-info",
                        ]:
                            receipt.update(
                                stdout=dump,
                                stdout_bytes=len(dump.encode()),
                                stdout_sha256=hashlib.sha256(dump.encode()).hexdigest(),
                            )
                    proof_path.write_text(json.dumps(proof))
                elif fault == "phase-pid":
                    path = self.attempt / "permission-transition-restored.identity.json"
                    value = json.loads(path.read_bytes())
                    value["pid"] = "2345"
                    path.write_text(json.dumps(value))
                elif fault in ("stale-phase", "native-start", "dead-zero"):
                    result = json.loads(result_path.read_bytes())
                    if fault == "stale-phase":
                        result["phases"]["restored"]["native_bootstrap"] = result[
                            "phases"
                        ]["active"]["native_bootstrap"]
                    elif fault == "native-start":
                        result["phases"]["active"]["process_identity"][
                            "native_started_unix_ms"
                        ] += 1
                    else:
                        result["phases"]["active"]["closed_resources"] = dict.fromkeys(
                            evidence.RECOVERY_RESOURCE_FIELDS, 0
                        )
                    result_path.write_text(json.dumps(result))
                elif fault == "missing-physical":
                    (
                        self.attempt
                        / "coordination/permission-transition-physical.json"
                    ).unlink()
                elif fault == "journal-prefix":
                    path = self.attempt / "permission-state-control.json"
                    journal = json.loads(path.read_bytes())
                    journal["actions"][-3]["exit_record"]["reason"] = 10
                    path.write_text(json.dumps(journal))
                else:
                    # Same JSON meaning but different original bytes must not pass
                    # against the provider's original exit_proof_sha256.
                    proof_path.write_text(
                        json.dumps(
                            json.loads(proof_path.read_bytes()), separators=(",", ":")
                        )
                    )
                self._refresh_permission_manifest()
                expected = (
                    "OS reason8 PID/UID/user identity"
                    if fault in ("reason10", "uid")
                    else "permission proof:"
                    if fault == "raw-reprojected"
                    else ".*"
                )
                with self.assertRaisesRegex(evidence.EvidenceError, expected):
                    evidence.verify_bundle(self.attempt / evidence.MANIFEST)
                with self.assertRaises(evidence.EvidenceError):
                    self.finish(bundle)
                self.assertEqual(self.manifest()["status"], "failed")
                evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_permission_each_fixed_reference_is_required_even_with_passed_sidecars(
        self,
    ) -> None:
        for role, name in evidence._permission_names().items():
            with self.subTest(role=role):
                self.attempt = self.root / ("missing-" + role)
                bundle = self.permission_ready()
                self.finish(bundle)
                (self.attempt / name).unlink()
                self._refresh_permission_manifest()
                self.assertIsNone(self.manifest()["references"][role])
                with self.assertRaises(evidence.EvidenceError):
                    evidence.verify_bundle(self.attempt / evidence.MANIFEST)
                with self.assertRaises(evidence.EvidenceError):
                    self.finish(bundle)
                self.assertEqual(self.manifest()["status"], "failed")
                evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_permission_failed_bundle_cannot_promote_valid_passed_sidecars(
        self,
    ) -> None:
        for fault in ("incomplete", "primary", "cleanup", "capture-failed"):
            with self.subTest(fault=fault):
                self.attempt = self.root / fault
                bundle = self.permission_ready()
                if fault == "capture-failed":
                    path = self.attempt / "permission-transition-runtime.result.json"
                    result = json.loads(path.read_bytes())
                    result.update(status="failed", primary_error="late capture failure")
                    path.write_text(json.dumps(result))
                    with self.assertRaises(evidence.EvidenceError):
                        self.finish(bundle)
                elif fault == "cleanup":
                    with self.assertRaises(evidence.EvidenceError):
                        self.finish(bundle, cleanup=["permission restoration failed"])
                else:
                    self.finish(
                        bundle,
                        completed=fault != "incomplete",
                        error=RuntimeError("original permission failure")
                        if fault == "primary"
                        else None,
                    )
                document = self.manifest()
                self.assertEqual(document["status"], "failed")
                self.assertTrue(document["evidence_errors"])
                self.assertEqual(
                    [row["status"] for row in document["flow_index"]["flows"]],
                    ["partial"] * 3,
                )
                manifest = self.attempt / evidence.MANIFEST
                evidence.verify_bundle(manifest)
                document["status"] = "passed"
                manifest.write_text(json.dumps(document))
                with self.assertRaisesRegex(
                    evidence.EvidenceError, "passed requires completion"
                ):
                    evidence.verify_bundle(manifest)

    def test_permission_no_late_foreign_partial_or_malformed_raw_record_ignored(
        self,
    ) -> None:
        from tools.tests.react_native.support.permission_host_samples import RUN

        for index, tail in enumerate(
            (
                f"BLEHUB_NITRO_RUNTIME rf-permission-transition:passed:phase=restored:runId={RUN}\n",
                "BLEHUB_NITRO_RUNTIME rf-other:late\n",
                "BLEHUB_NITRO_RUNTIME:malformed-late\n",
                "BLEHUB_NITRO_RUNTIME rf-permission-transition:partial",
                "BLEHUB_NITRO_RUNTIME rf-permission-transition:active:event:{}\n",
                'BLEHUB_NITRO_RUNTIME rf-permission-transition:native:bootstrap:{"phase":[]}\n',
            )
        ):
            with self.subTest(tail=tail):
                self.attempt = self.root / f"late-{index}"
                bundle = self.permission_ready()
                self.finish(bundle)
                path = self.attempt / "permission-transition-runtime.raw.log"
                with path.open("ab") as stream:
                    stream.write(
                        ("10-17 12:00:00.000 2345 2345 I Test: " + tail).encode()
                    )
                self._refresh_permission_manifest()
                with self.assertRaises(evidence.EvidenceError):
                    evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_permission_fixed_coordination_files_reject_extra_and_duplicate_controls(
        self,
    ) -> None:
        for name in (
            "permission-unexpected.json",
            "extra/permission-control.json",
            "permission-control-copy.json",
        ):
            with self.subTest(name=name):
                self.attempt = self.root / name.replace("/", "-")
                bundle = self.permission_ready()
                self.finish(bundle)
                path = self.attempt / "coordination" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    (self.attempt / "coordination/permission-control.json").read_text()
                )
                self._refresh_permission_manifest()
                with self.assertRaisesRegex(
                    evidence.EvidenceError, "coordination files"
                ):
                    evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_permission_duplicate_json_control_keys_are_not_silently_reauthorized(
        self,
    ) -> None:
        from tools.tests.react_native.support.permission_host_samples import RUN

        bundle = self.permission_ready()
        self.finish(bundle)
        (self.attempt / "coordination/permission-control.json").write_text(
            '{"schema_version":1,"run_id":"' + RUN + '","run_id":"' + RUN + '"}\n'
        )
        self._refresh_permission_manifest()
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_permission_reader_paths_symlinks_and_per_file_total_budgets(self) -> None:
        bundle = self.permission_ready()
        self.finish(bundle)
        original = self.manifest()
        for role, malicious in (
            ("os_death_proof", "../outside.json"),
            ("phase_identity_active", "/tmp/outside.json"),
        ):
            with self.subTest(role=role):
                document = copy.deepcopy(original)
                document["references"][role]["path"] = malicious
                with self.assertRaises(evidence.EvidenceError):
                    evidence._permission_proofs(self.attempt, document)
        role = "os_death_proof"
        path = self.attempt / original["references"][role]["path"]
        content = path.read_bytes()
        external = self.root / "external-proof.json"
        external.write_bytes(content)
        path.unlink()
        path.symlink_to(external)
        with self.assertRaises(evidence.EvidenceError):
            evidence._permission_proofs(self.attempt, original)
        path.unlink()
        path.write_bytes(content)
        with (
            mock.patch.object(evidence, "PERMISSION_FILE_BYTES", 8),
            self.assertRaises(evidence.EvidenceError),
        ):
            evidence._permission_proofs(self.attempt, original)
        one_file_budget = max(ref["size"] for ref in original["references"].values())
        with (
            mock.patch.object(evidence, "PERMISSION_PROOF_BYTES", one_file_budget),
            self.assertRaises(evidence.EvidenceError),
        ):
            evidence._permission_proofs(self.attempt, original)
        # The ordinary full-bundle inventory fingerprint also rejects raw edits.
        path.write_bytes(content + b"\n")
        with self.assertRaisesRegex(evidence.EvidenceError, "inventory/hash drift"):
            evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_permission_failed_partial_attempt_retains_primary_and_no_pass_index(
        self,
    ) -> None:
        bundle = self.permission_ready()
        raw = self.attempt / "permission-transition-runtime.raw.log"
        lines = raw.read_bytes().splitlines(keepends=True)
        stop = next(
            i
            for i, line in enumerate(lines)
            if b"native:bootstrap:" in line and b'"phase": "active"' in line
        )
        partial = b"".join(lines[:stop])
        raw.write_bytes(partial)
        self.log.write_bytes(partial)
        result_path = self.attempt / "permission-transition-runtime.result.json"
        result = json.loads(result_path.read_bytes())
        result.update(
            status="failed",
            primary_error="original permission deadline",
            phases={"denied": result["phases"]["denied"]},
            boundaries=[],
        )
        result_path.write_text(json.dumps(result))
        (self.attempt / "coordination/permission-transition-physical.json").unlink()
        self.finish(
            bundle,
            completed=False,
            error=TimeoutError("original permission deadline"),
            cleanup=["original restore failure"],
        )
        document = self.manifest()
        self.assertEqual(
            document["primary_error"]["message"], "original permission deadline"
        )
        self.assertEqual(document["cleanup_errors"], ["original restore failure"])
        self.assertEqual(
            [r["status"] for r in document["flow_index"]["flows"]],
            ["partial", "missing", "missing"],
        )
        self.assertTrue(document["evidence_errors"])
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_permission_failed_missing_projection_still_indexes_owned_raw(self) -> None:
        bundle = self.permission_ready()
        self.log.unlink()
        self.finish(
            bundle, completed=False, error=OSError("projection publication failed")
        )
        document = self.manifest()
        self.assertEqual(
            document["primary_error"]["message"], "projection publication failed"
        )
        self.assertEqual(
            [r["status"] for r in document["flow_index"]["flows"]], ["partial"] * 3
        )
        self.assertTrue(
            all(
                r["source"] == "permission-transition-runtime.raw.log"
                for r in document["flow_index"]["flows"]
            )
        )
        self.assertIn("missing runtime log", " ".join(document["evidence_errors"]))
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_permission_missing_artifact_capture_or_log_keeps_failed_bundle(
        self,
    ) -> None:
        bundle = self.bundle(scenario="permission-transition")
        self.finish(
            bundle, completed=False, error=RuntimeError("permission preflight failed")
        )
        document = self.manifest()
        self.assertEqual(
            [r["status"] for r in document["flow_index"]["flows"]], ["missing"] * 3
        )
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        with self.assertRaisesRegex(evidence.EvidenceError, "Android-only"):
            evidence.EvidenceBundle(
                self.root / "invalid-ios",
                platform="ios",
                device="synthetic",
                project_root=self.project,
                scenario="permission-transition",
            )

    def adapter_ready(
        self, *, active: bool = False, explicit_startup: bool = False
    ) -> evidence.EvidenceBundle:
        from tools.react_native.adapter_transition_ledger import PREFIX, verify_runtime

        bundle = evidence.EvidenceBundle(
            self.attempt,
            platform="android",
            device="synthetic-device-not-RF",
            project_root=self.project,
            scenario="adapter-transition",
            adapter_mode="active-loss"
            if active
            else "startup-off"
            if explicit_startup
            else None,
        )
        bundle.prepare_artifact(self.apk())
        self.log = self.attempt / ADAPTER_LOG
        markers = active_bodies() if active else None
        if explicit_startup:
            from tools.tests.react_native.support.adapter_transition_samples import (
                bodies,
            )

            markers = bodies()
            for i, marker in enumerate(markers):
                if marker.startswith(("native:bootstrap:", "native:signal:")):
                    prefix, payload = marker.split(":", 2)[:2], marker.split(":", 2)[2]
                    value = json.loads(payload)
                    value["adapter_mode"] = "startup-off"
                    markers[i] = ":".join(prefix) + ":" + json.dumps(value)
        raw = adapter_transcript(markers, newline="\r\n")
        projection = "".join(
            line[line.index(PREFIX) :].rstrip("\r\n") + "\n"
            for line in raw.splitlines(keepends=True)
            if PREFIX in line
        )
        self.log.write_bytes(projection.encode())
        (self.attempt / "adapter-transition-runtime.raw.log").write_bytes(raw.encode())
        runtime = verify_runtime(projection, ADAPTER_RUN, ADAPTER_SERVICE, self.log)
        request, receipt, physical = (
            active_physical_sidecars if active else physical_sidecars
        )(runtime)
        sidecars = {
            "adapter-transition-runtime.identity.json": {
                "schema_version": 1,
                "platform": "android",
                "scenario": "adapter-transition",
                "status": "passed",
                "run_id": ADAPTER_RUN,
                "pid": ADAPTER_BOOTSTRAP["pid"],
                "boot_id": ADAPTER_BOOTSTRAP["boot_id"],
                "native_bootstrap": runtime["native_bootstrap"],
                "process_records": ["synthetic-not-OS-proof"],
            },
            "adapter-transition-ledger.json": runtime,
            "coordination/adapter-transition-physical.json": physical,
            "coordination/fixture-session/fixture-request.json": request,
            "coordination/fixture-session/fixture-receipt.json": receipt,
            "adapter-state-control.json": state_control(),
            "coordination/adapter-transition-ready.json": physical["readiness"],
            "coordination/go": {"schema_version": 1, "run_id": ADAPTER_RUN},
            "artifact-install-identity.json": {
                "synthetic": "not installed-binary proof"
            },
        }
        if explicit_startup:
            physical["adapter_mode"] = "startup-off"
            physical["readiness"]["adapter_mode"] = "startup-off"
            sidecars["adapter-transition-runtime.identity.json"]["adapter_mode"] = (
                "startup-off"
            )
            sidecars["coordination/go"]["adapter_mode"] = "startup-off"
            sidecars["coordination/adapter-transition-control.json"] = {
                "schema_version": 1,
                "run_id": ADAPTER_RUN,
                "adapter_mode": "startup-off",
            }
            physical["go_sha256"] = hashlib.sha256(
                json.dumps(sidecars["coordination/go"]).encode()
            ).hexdigest()
        if active:
            from tests.peripherals.nrf52840.adapter_transition import CONTROL_NAME

            sidecars["coordination/go"]["adapter_mode"] = "active-loss"
            physical["go_sha256"] = hashlib.sha256(
                json.dumps(sidecars["coordination/go"]).encode()
            ).hexdigest()
            sidecars["coordination/" + CONTROL_NAME] = {
                "schema_version": 1,
                "run_id": ADAPTER_RUN,
                "adapter_mode": "active-loss",
            }
            sidecars.update(
                {
                    "coordination/" + name: value
                    for name, value in physical["active_protocol"].items()
                }
            )
            sidecars["adapter-transition-runtime.identity.json"].update(
                adapter_mode="active-loss",
                active_actions={
                    "arm_sent_monotonic": 1.0,
                    "off_requested_monotonic": 9.7,
                    "off_started_monotonic": 9.8,
                    "off_finished_monotonic": 13.6,
                    "arm_native_ack": True,
                    "arm_js_ack": True,
                },
            )
        for name, value in sidecars.items():
            path = self.attempt / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding="utf-8")
        return bundle

    def test_explicit_startup_mode_is_bound_without_rewriting_legacy_evidence(
        self,
    ) -> None:
        bundle = self.adapter_ready(explicit_startup=True)
        bundle.finish(
            completed=True, primary_error=None, cleanup_errors=[], runtime_log=self.log
        )
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        document = json.loads((self.attempt / evidence.MANIFEST).read_bytes())
        self.assertEqual(document["adapter_mode"], "startup-off")
        self.assertEqual(document["status"], "passed")

    def test_active_loss_bundle_binds_raw_handshake_mode_and_cannot_downgrade(
        self,
    ) -> None:
        bundle = self.adapter_ready(active=True)
        bundle.finish(
            completed=True, primary_error=None, cleanup_errors=[], runtime_log=self.log
        )
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        document = json.loads((self.attempt / evidence.MANIFEST).read_bytes())
        self.assertEqual(document["status"], "passed")
        self.assertEqual(document["adapter_mode"], "active-loss")
        changed = copy.deepcopy(document)
        changed.pop("adapter_mode")
        with self.assertRaisesRegex(evidence.EvidenceError, "mode"):
            evidence._adapter_proofs(self.attempt, changed)
        handshake = self.attempt / "coordination/adapter-transition-off-ready.json"
        original = handshake.read_bytes()
        handshake.unlink()
        with self.assertRaises((evidence.EvidenceError, FileNotFoundError)):
            evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        handshake.write_bytes(original)
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_adapter_transition_fixed_refs_raw_ranges_and_offline_read_only(
        self,
    ) -> None:
        bundle = self.adapter_ready()
        self.finish(bundle)
        manifest = self.manifest()
        refs = manifest["references"]
        self.assertEqual(
            {key: value["path"] for key, value in refs.items()},
            {
                "runtime_identity": "adapter-transition-runtime.identity.json",
                "fixture_request": "coordination/fixture-session/fixture-request.json",
                "fixture_receipt": "coordination/fixture-session/fixture-receipt.json",
                "physical_adapter_transition": "coordination/adapter-transition-physical.json",
                "ledger_adapter_transition": "adapter-transition-ledger.json",
                "adapter_state_control": "adapter-state-control.json",
                "adapter_transition_ready": "coordination/adapter-transition-ready.json",
                "adapter_transition_go": "coordination/go",
                "raw_runtime": "adapter-transition-runtime.raw.log",
                "artifact_install_identity": "artifact-install-identity.json",
            },
        )
        raw = (
            (self.attempt / refs["raw_runtime"]["path"])
            .read_bytes()
            .splitlines(keepends=True)
        )
        flows = manifest["flow_index"]["flows"]
        self.assertEqual([flow["mode"] for flow in flows], ["off", "on"])
        for flow in flows:
            self.assertEqual(flow["source"], refs["raw_runtime"]["path"])
            segment = b"".join(raw[flow["start_line"] - 1 : flow["end_line"]])
            self.assertEqual(flow["sha256"], hashlib.sha256(segment).hexdigest())
            self.assertEqual(flow["size"], len(segment))
            self.assertEqual(flow["pid"], ADAPTER_BOOTSTRAP["pid"])
        before = {
            path: path.read_bytes()
            for path in self.attempt.rglob("*")
            if path.is_file()
        }
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_adapter_transition_index_mutation_cannot_relabel_ranges_or_native_identity(
        self,
    ) -> None:
        bundle = self.adapter_ready()
        self.finish(bundle)
        manifest_path = self.attempt / evidence.MANIFEST
        original = manifest_path.read_bytes()
        for key, value in (
            ("start_line", 1),
            ("end_line", 2),
            ("sha256", "0" * 64),
            ("runtime_nonce", "foreign"),
            ("boot_id", ADAPTER_RUN),
            ("pid", 4321),
        ):
            document = json.loads(original)
            document["flow_index"]["flows"][0][key] = value
            manifest_path.write_text(json.dumps(document), encoding="utf-8")
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(
                    ValueError, "flow ranges/segment hashes/runtime identity drift"
                ),
            ):
                evidence.verify_bundle(manifest_path)
        manifest_path.write_bytes(original)

    def test_adapter_transition_android_only_and_failed_partial_remains_auditable(
        self,
    ) -> None:
        with self.assertRaisesRegex(evidence.EvidenceError, "Android-only"):
            self.bundle("ios", scenario="adapter-transition")
        bundle = self.bundle(scenario="adapter-transition")
        self.log.write_text(
            "BLEHUB_NITRO_RUNTIME rf-adapter-transition:failed:launch failed\n",
            encoding="utf-8",
        )
        self.finish(bundle, completed=False, error=RuntimeError("synthetic failure"))
        self.assertEqual(self.manifest()["status"], "failed")
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_adapter_transition_all_required_refs_and_sidecar_identity_are_mandatory(
        self,
    ) -> None:
        self.adapter_ready()
        paths = [
            row["path"]
            for row in evidence._references(
                evidence._inventory(self.attempt),
                "android",
                str(ADAPTER_LOG),
                "adapter-transition",
            ).values()
        ]
        # No re-freeze: recompute inventories/indexes to isolate structural checks,
        # rather than passing merely because the old manifest hash was unchanged.
        for name in paths:
            path = self.attempt / name
            original = path.read_bytes()
            path.unlink()
            bundle = self.bundle_document_for_adapter()
            with (
                self.subTest(missing=name),
                self.assertRaises((ValueError, KeyError, TypeError, FileNotFoundError)),
            ):
                evidence._passed_structure(bundle)
                evidence._adapter_proofs(self.attempt, bundle)
            path.write_bytes(original)
        for name in (
            "adapter-transition-runtime.identity.json",
            "adapter-transition-ledger.json",
            "coordination/adapter-transition-physical.json",
        ):
            path = self.attempt / name
            original = path.read_bytes()
            for field, value in (
                ("run_id", "12345678-1234-4123-8123-123456789abd"),
                ("status", "failed"),
            ):
                changed = json.loads(original)
                changed[field] = value
                path.write_text(json.dumps(changed), encoding="utf-8")
                with (
                    self.subTest(name=name, field=field),
                    self.assertRaises(ValueError),
                ):
                    evidence._adapter_proofs(
                        self.attempt, self.bundle_document_for_adapter()
                    )
            path.write_bytes(original)

    def bundle_document_for_adapter(self) -> dict:
        document = self.manifest()
        files = evidence._inventory(self.attempt)
        document.update(
            completed=True,
            primary_error=None,
            cleanup_errors=[],
            evidence_errors=[],
            runtime_log=str(ADAPTER_LOG),
            files=files,
            references=evidence._references(
                files, "android", str(ADAPTER_LOG), "adapter-transition"
            ),
            flow_index=evidence._flow_index(
                self.log, str(ADAPTER_LOG), "adapter-transition"
            ),
        )
        return document

    def test_adapter_transition_mutation_inventory_and_resubmitted_lineage(
        self,
    ) -> None:
        bundle = self.adapter_ready()
        self.finish(bundle)
        raw = self.attempt / "adapter-transition-runtime.raw.log"
        original = raw.read_bytes()
        raw.write_bytes(original + b"diagnostic mutation\n")
        with self.assertRaisesRegex(ValueError, "inventory/hash drift"):
            evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        raw.write_bytes(original)
        # A re-inventoried foreign transcript, forged ledger or nonzero resources
        # must fail structure too; integrity-only hashing is not the whole gate.
        for before, after in (
            (b"peripheralId=42", b"peripheralId=43"),
            (b'"listeners": 0', b'"listeners": 1'),
            (b'"pid": 1234', b'"pid": 4321'),
        ):
            raw.write_bytes(original.replace(before, after))
            document = self.bundle_document_for_adapter()
            with self.subTest(before=before), self.assertRaises(ValueError):
                evidence._adapter_proofs(self.attempt, document)
        raw.write_bytes(original)
        ledger_path = self.attempt / "adapter-transition-ledger.json"
        original_ledger = ledger_path.read_bytes()
        for field, value in (
            ("runtime_nonce", "foreign-boot-nonce"),
            ("runtime_sha256", "0" * 64),
            ("on_flow_sha256", "0" * 64),
            ("pid", True),
        ):
            ledger = json.loads(original_ledger)
            ledger[field] = value
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            with self.subTest(field=field), self.assertRaises(ValueError):
                evidence._adapter_proofs(
                    self.attempt, self.bundle_document_for_adapter()
                )
        ledger_path.write_bytes(original_ledger)

    def test_adapter_control_and_physical_mutations_fail_even_after_reinventory(
        self,
    ) -> None:
        bundle = self.adapter_ready()
        self.finish(bundle)
        names = {
            "control": "adapter-state-control.json",
            "physical": "coordination/adapter-transition-physical.json",
            "receipt": "coordination/fixture-session/fixture-receipt.json",
            "ready": "coordination/adapter-transition-ready.json",
            "go": "coordination/go",
        }
        mutations = [
            ("ready", ("run_id",), "foreign"),
            ("ready", ("receipt_sha256",), "0" * 64),
            ("ready", ("native_bootstrap", "boot_id"), ADAPTER_RUN),
            (
                "ready",
                ("monotonic",),
                98.0,
            ),  # Valid standalone, disagrees with physical.
            ("physical", ("readiness", "monotonic"), 98.0),
            ("go", ("run_id",), "foreign"),
            ("go", ("schema_version",), True),
            ("control", ("device",), "foreign"),
            ("control", ("actions", 0, "receipts", 0, "stdout"), "state: OFF\n"),
            ("control", ("actions", 1, "confirmed_monotonic"), 9),
            ("control", ("actions", 3, "label"), "restore-only"),
            ("physical", ("events", 1, "peer"), ["123456789abd", 1]),
            ("physical", ("cleanup", "advertising_stopped"), False),
            ("physical", ("session_id",), "foreign"),
            ("physical", ("receipt_sha256",), "0" * 64),
            ("receipt", ("fixture_pid",), 54321),
            # Even an otherwise-semantic-equivalent receipt reserialization must
            # change physical.receipt_sha256; it binds the original file bytes.
            ("receipt", ("installed_identity", "synthetic"), "changed raw receipt"),
        ]
        for role, target, value in mutations:
            path = self.attempt / names[role]
            original = path.read_bytes()
            changed = json.loads(original)
            node = changed
            for key in target[:-1]:
                node = node[key]
            node[target[-1]] = value
            path.write_text(json.dumps(changed), encoding="utf-8")
            document = self.bundle_document_for_adapter()
            with self.subTest(role=role, target=target), self.assertRaises(ValueError):
                evidence._adapter_proofs(self.attempt, document)
            # Offline entry point must make the same checks, not only finish().
            document["status"] = "passed"
            manifest_path = self.attempt / evidence.MANIFEST
            before_manifest = manifest_path.read_bytes()
            manifest_path.write_text(json.dumps(document), encoding="utf-8")
            with (
                self.subTest(role=role, target=target, offline=True),
                self.assertRaises(ValueError),
            ):
                evidence.verify_bundle(manifest_path)
            manifest_path.write_bytes(before_manifest)
            path.write_bytes(original)

    def test_adapter_ready_go_duplicate_keys_and_raw_go_hash_binding(self) -> None:
        self.adapter_ready()
        for name, before, after in (
            (
                "coordination/adapter-transition-ready.json",
                b'"status": "ready"',
                b'"status": "failed", "status": "ready"',
            ),
            (
                "coordination/go",
                b'"schema_version": 1',
                b'"schema_version": 2, "schema_version": 1',
            ),
        ):
            path = self.attempt / name
            original = path.read_bytes()
            path.write_bytes(original.replace(before, after))
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "duplicate JSON key"),
            ):
                evidence._adapter_proofs(
                    self.attempt, self.bundle_document_for_adapter()
                )
            path.write_bytes(original)
        go = self.attempt / "coordination/go"
        go.write_bytes(go.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "physical go bytes hash"):
            evidence._adapter_proofs(self.attempt, self.bundle_document_for_adapter())

    def test_adapter_nullable_raw_dto_is_audited_without_rewriting_capture(
        self,
    ) -> None:
        from tools.react_native.adapter_transition_ledger import PREFIX, verify_runtime

        bundle = self.adapter_ready()
        raw_path = self.attempt / "adapter-transition-runtime.raw.log"
        lines = []
        for line in raw_path.read_bytes().decode("utf-8").splitlines(keepends=True):
            marker = PREFIX + "off:event:"
            if marker in line:
                head, payload = line.split(marker, 1)
                event = json.loads(payload)
                event.update(
                    connectionId=None, scanSessionId=None, gattServices=None, value=None
                )
                if event.get("error"):
                    event["error"]["platformDetails"] = {
                        "platform": "platform_android",
                        "code": None,
                        "name": None,
                        "message": None,
                    }
                else:
                    event["error"] = None
                line = head + marker + json.dumps(event) + "\r\n"
            lines.append(line)
        raw = "".join(lines).encode()
        raw_path.write_bytes(raw)
        projection = "".join(
            line[line.index(PREFIX) :].rstrip("\r\n") + "\n"
            for line in lines
            if PREFIX in line
        ).encode()
        self.log.write_bytes(projection)
        runtime = verify_runtime(
            projection.decode("utf-8"), ADAPTER_RUN, ADAPTER_SERVICE, self.log
        )
        (self.attempt / "adapter-transition-ledger.json").write_text(
            json.dumps(runtime), encoding="utf-8"
        )
        self.finish(bundle)
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        self.assertEqual(raw_path.read_bytes(), raw)
        self.assertEqual(self.log.read_bytes(), projection)
        manifest = self.manifest()
        self.assertEqual(
            manifest["references"]["raw_runtime"]["sha256"],
            hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual(
            runtime["runtime_sha256"], hashlib.sha256(projection).hexdigest()
        )

    def test_adapter_state_control_duplicate_json_keys_are_not_last_key_wins(
        self,
    ) -> None:
        self.adapter_ready()
        path = self.attempt / "adapter-state-control.json"
        original = path.read_bytes()
        for before, after in (
            (b'"status": "passed"', b'"status": "failed", "status": "passed"'),
            (b'"returncode": 0', b'"returncode": 1, "returncode": 0'),
        ):
            path.write_bytes(original.replace(before, after))
            with (
                self.subTest(before=before),
                self.assertRaisesRegex(ValueError, "duplicate JSON key"),
            ):
                evidence._adapter_proofs(
                    self.attempt, self.bundle_document_for_adapter()
                )

    def full_ready(
        self, platform: str = "android"
    ) -> tuple[evidence.EvidenceBundle, SuiteFixture]:
        """Real host schemas/checkers over simulated BTP, never hardware/RF."""
        bundle = self.bundle(platform, scenario="full-suite")
        bundle.document["device"] = "physical-device"  # Synthetic host schema label.
        bundle.prepare_artifact(self.apk() if platform == "android" else self.app())
        fixture = SuiteFixture(self.attempt, platform)
        self.log = fixture.log
        ledger = fixture.verify()
        (self.attempt / "full-suite-ledger.json").write_text(
            json.dumps(ledger), encoding="utf-8"
        )
        if platform == "android":
            (self.attempt / "artifact-install-identity.json").write_text(
                '{"synthetic":"not RF or installed-binary proof"}\n', encoding="utf-8"
            )
        return bundle, fixture

    def test_full_suite_raw_crlf_and_mixed_endings_preserve_line_text_lineage(
        self,
    ) -> None:
        original_root = self.root
        for platform in ("ios", "android"):
            for mixed in (False, True):
                with self.subTest(platform=platform, mixed=mixed):
                    self.root = original_root / f"{platform}-{mixed}"
                    self.root.mkdir()
                    self.attempt = self.root / "attempt"
                    bundle, fixture = self.full_ready(platform)
                    path = self.log.with_suffix(".raw.log")
                    lines = path.read_bytes().splitlines(keepends=True)
                    raw = b"".join(
                        line.removesuffix(b"\n") + b"\r\n"
                        if not mixed or index % 2
                        else line
                        for index, line in enumerate(lines)
                    )
                    path.write_bytes(raw)
                    ledger = fixture.verify()
                    (self.attempt / "full-suite-ledger.json").write_text(
                        json.dumps(ledger), encoding="utf-8"
                    )
                    self.finish(bundle)
                    self.assertEqual(self.manifest()["status"], "passed")
                    self.assertEqual(path.read_bytes(), raw)
                    evidence.verify_bundle(self.attempt / evidence.MANIFEST)
                    # Accepting CRLF must not permit message-byte or header changes.
                    path.write_bytes(
                        raw.replace(b"BLEHUB_NITRO_RUNTIME", b"BLEHUB_OTHER_RUNTIME", 1)
                    )
                    with self.assertRaises(evidence.EvidenceError):
                        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_full_suite_both_platforms_current_host_schema_raw_ranges_and_offline(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            with self.subTest(platform=platform):
                self.attempt = self.root / platform
                bundle, fixture = self.full_ready(platform)
                self.finish(bundle)
                document = self.manifest()
                self.assertEqual(document["status"], "passed")
                self.assertEqual(document["scenario"], "full-suite")
                index = document["flow_index"]
                self.assertEqual(
                    index["sub_status"],
                    {"continuous10": "passed", "recovery_ten": "passed"},
                )
                raw = self.log.read_bytes().splitlines(keepends=True)
                for key in ("continuous10", "recovery_ten"):
                    sub = index[key]
                    for row in sub["flows"]:
                        segment = b"".join(raw[row["start_line"] - 1 : row["end_line"]])
                        self.assertEqual(row["source"], "full-suite-runtime.log")
                        self.assertEqual(
                            row["sha256"], hashlib.sha256(segment).hexdigest()
                        )
                        self.assertEqual(row["size"], len(segment))
                    self.assertEqual(
                        sub["flows"][-1]["end_line"], sub["summaries"][0]["line"]
                    )
                recovery = index["recovery_ten"]
                self.assertEqual(len(recovery["resources"]), 50)
                for row in recovery["resources"]:
                    if row["phase"] == "closed":
                        self.assertEqual(
                            row["snapshot"],
                            dict.fromkeys(evidence.RECOVERY_RESOURCE_FIELDS, 0),
                        )
                self.assertLess(
                    index["native_bootstraps"][1]["line"], index["events"][0]["line"]
                )
                refs = document["references"]
                self.assertEqual(
                    refs["runtime_identity"]["path"], "full-suite-runtime.identity.json"
                )
                self.assertEqual(
                    refs["runtime_capture"]["path"], "full-suite-runtime.capture.json"
                )
                self.assertEqual(
                    refs["physical_recovery_ten"]["path"],
                    "coordination/recovery/recovery-ten-physical.json",
                )
                if platform == "ios":
                    self.assertEqual(
                        refs["device_install"]["path"], "device-install.json"
                    )
                # Offline verification may not rerun host live receipt/physical checks.
                self.assertEqual(index["events"][0]["run_id"], fixture.plan.session_id)
                with (
                    mock.patch(
                        "tools.react_native.full_suite_ledger.verify",
                        side_effect=AssertionError("no RF replay"),
                    ),
                    mock.patch(
                        "tests.peripherals.nrf52840.fixture_session.verify_receipt",
                        side_effect=AssertionError("no live authorization"),
                    ),
                    mock.patch.object(
                        evidence.subprocess,
                        "run",
                        side_effect=AssertionError("offline only"),
                    ),
                ):
                    evidence.verify_bundle(self.attempt / evidence.MANIFEST)
                    relocated = self.root / ("relocated-" + platform)
                    shutil.copytree(self.attempt, relocated)
                    evidence.verify_bundle(relocated / evidence.MANIFEST)

    def test_full_suite_rejects_missing_duplicate_foreign_reordered_and_truncated_phases(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            self.attempt = self.root / platform
            bundle, _ = self.full_ready(platform)
            original = self.log.read_bytes().splitlines(keepends=True)
            tokens = (
                b"rf-lab-full:runtime:",
                b"rf-lab-full:recovery-awaiting:",
                b"rf-lab-full:native:signal:",
                b"rf-lab-full:recovery-ready:",
                b"rf-lab-full:passed:",
                b"rf-lab-suite:runtime:",
                b"rf-lab-suite:passed:flows=10",
                b"rf-recovery-ten:runtime:",
                b"rf-recovery-ten:passed:cycles=10",
                b"rf-recovery-ten:cycle:9:passed",
            )
            for token in tokens:
                positions = [i for i, line in enumerate(original) if token in line]
                for position in positions:
                    for operation in ("missing", "duplicate", "late", "truncated"):
                        changed = list(original)
                        if operation == "missing":
                            changed.pop(position)
                        elif operation == "duplicate":
                            changed.insert(position, changed[position])
                        elif operation == "late":
                            if position == len(changed) - 1:
                                changed.insert(position - 1, changed.pop(position))
                            else:
                                changed.append(changed.pop(position))
                        else:
                            changed[position] = changed[position][:-1]
                        with self.subTest(
                            platform=platform,
                            token=token,
                            position=position,
                            operation=operation,
                        ):
                            self.log.write_bytes(b"".join(changed))
                            with self.assertRaises(evidence.EvidenceError):
                                self.finish(bundle)
                            self.assertEqual(self.manifest()["status"], "failed")
                            evidence.verify_bundle(self.attempt / evidence.MANIFEST)
            for old, new in (
                (b"synthetic-one-runtime", b"foreign-nonce"),
                (b'"pid": 42', b'"pid": true'),
                (b'"source": "payload-url"', b'"source": "launch"'),
                (b'"boot_id": "', b'"boot_id": "foreign-'),
            ):
                changed = b"".join(original).replace(old, new, 1)
                self.assertNotEqual(changed, b"".join(original))
                with self.subTest(platform=platform, foreign=new):
                    self.log.write_bytes(changed)
                    with self.assertRaises(evidence.EvidenceError):
                        self.finish(bundle)
            self.log.write_bytes(b"".join(original))
            self.finish(bundle)

    def test_full_suite_partial_never_promotes_base_or_recovery_pass(self) -> None:
        for platform in ("android", "ios"):
            self.attempt = self.root / platform
            bundle, _ = self.full_ready(platform)
            original = self.log.read_bytes().splitlines(keepends=True)
            boundaries = [
                (
                    b"rf-lab-full:runtime:",
                    {"continuous10": "not-started", "recovery_ten": "not-started"},
                ),
                (
                    b"rf-lab-full:recovery-awaiting:",
                    {"continuous10": "passed", "recovery_ten": "not-started"},
                ),
                (
                    b"rf-recovery-ten:cycle:3:start",
                    {"continuous10": "passed", "recovery_ten": "partial"},
                ),
                (
                    b"rf-lab-full:runtime:",
                    {"continuous10": "passed", "recovery_ten": "passed"},
                ),
                (
                    b"rf-lab-full:passed:",
                    {"continuous10": "passed", "recovery_ten": "passed"},
                ),
            ]
            for i, (token, statuses) in enumerate(boundaries):
                positions = [p for p, line in enumerate(original) if token in line]
                end = positions[-1] if i == 3 else positions[0]
                with self.subTest(platform=platform, boundary=i):
                    self.log.write_bytes(b"".join(original[:end]))
                    self.finish(
                        bundle,
                        completed=False,
                        error=TimeoutError("synthetic phase interruption"),
                    )
                    document = self.manifest()
                    self.assertEqual(document["status"], "failed")
                    self.assertEqual(document["flow_index"]["sub_status"], statuses)
                    evidence.verify_bundle(self.attempt / evidence.MANIFEST)
                    if statuses["recovery_ten"] == "not-started":
                        self.assertTrue(
                            all(
                                row["status"] == "missing" and row["sha256"] is None
                                for row in document["flow_index"]["recovery_ten"][
                                    "flows"
                                ]
                            )
                        )
            self.log.unlink()
            self.finish(bundle, completed=False, error=RuntimeError("no capture"))
            evidence.verify_bundle(self.attempt / evidence.MANIFEST)
            self.assertEqual(
                self.manifest()["flow_index"]["sub_status"],
                {"continuous10": "not-started", "recovery_ten": "not-started"},
            )

    def test_full_suite_requires_every_ref_and_never_uses_legacy_ios_sidecars(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            self.attempt = self.root / platform
            bundle, _ = self.full_ready(platform)
            refs = evidence._references(
                evidence._inventory(self.attempt), platform, self.log.name, "full-suite"
            )
            for role, row in refs.items():
                path = self.attempt / row["path"]
                temporary = path.with_name(path.name + ".saved")
                path.rename(temporary)
                try:
                    with self.subTest(platform=platform, role=role):
                        if role == "runtime_identity" and platform == "ios":
                            self.log.with_suffix(".rf-host-identity.json").write_bytes(
                                temporary.read_bytes()
                            )
                        with self.assertRaises(evidence.EvidenceError):
                            self.finish(bundle)
                        self.assertIsNone(self.manifest()["references"][role])
                        evidence.verify_bundle(self.attempt / evidence.MANIFEST)
                finally:
                    temporary.rename(path)
            self.finish(bundle)

    def test_full_suite_hash_tamper_subindex_tamper_scenario_drift_and_no_backfill(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            self.attempt = self.root / platform
            bundle, _ = self.full_ready(platform)
            self.finish(bundle)
            manifest = self.attempt / evidence.MANIFEST
            saved = manifest.read_bytes()
            original = self.log.read_bytes()
            for scenario in (
                "continuous10",
                "recovery-ten",
                "pending-read-reload",
                None,
            ):
                document = json.loads(saved)
                if scenario is None:
                    document.pop("scenario")
                else:
                    document["scenario"] = scenario
                manifest.write_text(json.dumps(document), encoding="utf-8")
                with (
                    self.subTest(platform=platform, scenario=scenario),
                    self.assertRaises((ValueError, KeyError, TypeError)),
                ):
                    evidence.verify_bundle(manifest)
                self.assertEqual(json.loads(manifest.read_bytes()), document)
            manifest.write_bytes(saved)
            document = json.loads(saved)
            document["flow_index"]["continuous10"]["flows"][-1]["end_line"] = len(
                original.splitlines()
            )
            manifest.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(evidence.EvidenceError):
                evidence.verify_bundle(manifest)
            manifest.write_bytes(saved)
            for role in (
                "raw_runtime",
                "recovery_runtime",
                "ledger_full_suite",
                "ledger_recovery_ten",
                "full_suite_handoff",
            ):
                path = self.attempt / json.loads(saved)["references"][role]["path"]
                content = path.read_bytes()
                mode = stat.S_IMODE(path.stat().st_mode)
                path.chmod(0o600)
                path.write_bytes(content + b"\n")
                with (
                    self.subTest(platform=platform, tamper=role),
                    self.assertRaises(evidence.EvidenceError),
                ):
                    evidence.verify_bundle(manifest)
                path.write_bytes(content)
                path.chmod(mode)
            evidence.verify_bundle(manifest)
            # Changing the dispatch scenario cannot reinterpret a complete full log.
            for scenario in ("continuous10", "recovery-ten"):
                bundle.document["scenario"] = scenario
                with (
                    self.subTest(platform=platform, dispatch=scenario),
                    self.assertRaises(evidence.EvidenceError),
                ):
                    self.finish(bundle)
            bundle.document["scenario"] = "full-suite"
            self.finish(bundle)

    def test_full_suite_rehashed_proof_schema_and_identity_drift_is_rejected(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            self.attempt = self.root / platform
            bundle, _ = self.full_ready(platform)
            self.finish(bundle)
            refs = self.manifest()["references"]
            originals = {
                role: (self.attempt / row["path"]).read_bytes()
                for role, row in refs.items()
                if row["path"].endswith(".json")
            }
            bindings = {
                "runtime_identity": ("capture", "identity"),
                "runtime_capture": ("capture", "receipt"),
                "ledger_continuous10": ("ledgers", "continuous10"),
                "ledger_recovery_ten": ("ledgers", "recovery_ten"),
                "full_suite_handoff": ("physical", "handoff"),
            }
            cases = [
                ("runtime_identity", lambda d: d.update(scenario="recovery-ten")),
                ("runtime_identity", lambda d: d.update(pid=True)),
                ("runtime_identity", lambda d: d["bootstrap"].update(boot_id=SESSION)),
                ("runtime_capture", lambda d: d.update(runtime_nonce="other-runtime")),
                ("runtime_capture", lambda d: d.update(base_status="running")),
                (
                    "runtime_capture",
                    lambda d: d["begin"]["native_signal"].update(pid=99),
                ),
                ("ledger_continuous10", lambda d: d.update(status="failed")),
                ("ledger_recovery_ten", lambda d: d.update(platform="other")),
                (
                    "ledger_recovery_ten",
                    lambda d: d.update(runtime_nonce="other-runtime"),
                ),
                (
                    "ledger_recovery_ten",
                    lambda d: d["counts"].update(resource_snapshots=49),
                ),
                (
                    "ledger_recovery_ten",
                    lambda d: d["closed_resource_totals"].update(hubRoots=1),
                ),
                ("ledger_full_suite", lambda d: d["counts"].update(capture_children=2)),
                ("ledger_full_suite", lambda d: d["counts"].update(base_flows=True)),
                ("ledger_full_suite", lambda d: d.update(session_id=SESSION)),
                (
                    "ledger_full_suite",
                    lambda d: d["ledgers"]["recovery_ten"].update(sha256="00" * 32),
                ),
                ("ledger_full_suite", lambda d: d["capture"]["raw_line_numbers"].pop()),
                ("fixture_request", lambda d: d.update(session_id=SESSION)),
                ("fixture_receipt", lambda d: d.update(fixture_pid=99)),
                ("full_suite_handoff", lambda d: d.update(status="failed")),
            ]
            if platform == "ios":
                cases.extend(
                    [
                        (
                            "device_install",
                            lambda d: d["info"].update(outcome="failure"),
                        ),
                        (
                            "device_install",
                            lambda d: d["result"].update(
                                deviceIdentifier="other-device"
                            ),
                        ),
                        (
                            "device_install",
                            lambda d: d["result"]["installedApplications"][0].update(
                                installationURL="file:/foreign/"
                            ),
                        ),
                    ]
                )
            for role, change in cases:
                for name, content in originals.items():
                    path = self.attempt / refs[name]["path"]
                    mode = stat.S_IMODE(path.stat().st_mode)
                    path.chmod(0o600)
                    path.write_bytes(content)
                    path.chmod(mode)
                path = self.attempt / refs[role]["path"]
                value = json.loads(path.read_bytes())
                change(value)
                mode = stat.S_IMODE(path.stat().st_mode)
                path.chmod(0o600)
                path.write_text(json.dumps(value), encoding="utf-8")
                path.chmod(mode)
                # Update the host ledger binding too, so schema/identity checks,
                # not just a stale file hash, must reject the would-be pass.
                if role in bindings:
                    full = json.loads(originals["ledger_full_suite"])
                    group, key = bindings[role]
                    full[group][key]["sha256"] = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    (self.attempt / "full-suite-ledger.json").write_text(
                        json.dumps(full), encoding="utf-8"
                    )
                with self.subTest(platform=platform, role=role):
                    with self.assertRaises(evidence.EvidenceError):
                        self.finish(bundle)
                    document = self.manifest()
                    self.assertEqual(
                        document["flow_index"]["sub_status"],
                        {"continuous10": "passed", "recovery_ten": "passed"},
                    )
                    evidence.verify_bundle(self.attempt / evidence.MANIFEST)
                    # A rewritten verdict cannot bypass offline proof structure.
                    document.update(status="passed", evidence_errors=[])
                    (self.attempt / evidence.MANIFEST).write_text(
                        json.dumps(document), encoding="utf-8"
                    )
                    with self.assertRaises(evidence.EvidenceError):
                        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_full_suite_reuses_strict_snapshot_validation_and_rejects_foreign_failure_tail(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            self.attempt = self.root / platform
            bundle, _ = self.full_ready(platform)
            original = self.log.read_bytes().splitlines(keepends=True)
            position = next(
                i
                for i, line in enumerate(original)
                if b"resources:cycle=0:phase=closed:snapshot=" in line
            )
            head, _, snapshot = original[position].partition(b":snapshot=")
            changes = [
                lambda d: d.update(hubRoots=1),
                lambda d: d.update(handoffRoots=-1),
                lambda d: d.update(listeners=False),
                lambda d: d.update(connections=0.0),
                lambda d: d.pop("backendLeases"),
                lambda d: d.update(untrackedRoot=0),
            ]
            bad_logs = []
            for change in changes:
                value = json.loads(snapshot)
                change(value)
                lines = list(original)
                lines[position] = (
                    head + b":snapshot=" + json.dumps(value).encode() + b"\n"
                )
                bad_logs.append(b"".join(lines))
            bad_logs.extend(
                [
                    b"".join(original[:position] + original[position + 1 :]),
                    b"".join(
                        original[:position] + [original[position]] + original[position:]
                    ),
                    b"".join(original)
                    + evidence.FULL_PREFIX.encode()
                    + b"failed:synthetic\n",
                    b"".join(original)
                    + evidence.PENDING_PREFIX.encode()
                    + b"passed:runId="
                    + SESSION.encode()
                    + b"\n",
                ]
            )
            for number, raw in enumerate(bad_logs):
                with self.subTest(platform=platform, invalid=number):
                    self.log.write_bytes(raw)
                    with self.assertRaises(evidence.EvidenceError):
                        self.finish(bundle)
                    evidence.verify_bundle(self.attempt / evidence.MANIFEST)
            self.log.write_bytes(b"".join(original))
            projection = self.attempt / "recovery-ten-runtime.log"
            projection.write_bytes(
                projection.read_bytes().replace(b"cycle:9:passed", b"cycle:9:omitted")
            )
            with self.assertRaisesRegex(evidence.EvidenceError, "projection drift"):
                self.finish(bundle)

    def recovery_ready(self, platform: str = "android") -> evidence.EvidenceBundle:
        self.log = self.attempt / "recovery-ten-runtime.log"
        bundle = self.bundle(platform, scenario="recovery-ten")
        bundle.prepare_artifact(self.apk() if platform == "android" else self.app())
        self.log.write_bytes(recovery_runtime(platform))
        if platform == "ios":
            self.log.with_suffix(".raw.log").write_bytes(
                b"synthetic console capture\n" + self.log.read_bytes()
            )
        self.required(platform, scenario="recovery-ten")
        (self.attempt / "coordination/recovery-ten-physical.json").chmod(0o400)
        return bundle

    def recovery_snapshot_change(
        self, raw: bytes, change, *, phase: str = "closed"
    ) -> bytes:
        prefix = f"{evidence.RECOVERY_PREFIX}resources:cycle=0:phase={phase}:snapshot=".encode()
        lines = raw.splitlines(keepends=True)
        position = next(i for i, line in enumerate(lines) if line.startswith(prefix))
        snapshot = json.loads(lines[position][len(prefix) :])
        change(snapshot)
        lines[position] = (
            prefix + json.dumps(snapshot, separators=(",", ":")).encode() + b"\n"
        )
        return b"".join(lines)

    def test_recovery_ten_both_platforms_one_runtime_ten_raw_cycle_ranges(self) -> None:
        for platform in ("android", "ios"):
            with self.subTest(platform=platform):
                self.attempt = self.root / platform
                bundle = self.recovery_ready(platform)
                self.finish(bundle)
                evidence.verify_bundle(self.attempt / evidence.MANIFEST)
                document = self.manifest()
                index = document["flow_index"]
                self.assertEqual(document["scenario"], "recovery-ten")
                self.assertEqual(index["mode"], "cycle")
                self.assertEqual(len(index["resources"]), 50)
                self.assertEqual(len(index["native_markers"]), 11)
                self.assertEqual(
                    [row["phase"] for row in index["runtime_markers"]],
                    ["start", "close"],
                )
                lines = self.log.read_bytes().splitlines(keepends=True)
                for cycle, row in enumerate(index["flows"]):
                    self.assertEqual(
                        (row["cycle"], row["mode"], row["status"]),
                        (cycle, f"cycle-{cycle}", "passed"),
                    )
                    segment = b"".join(lines[row["start_line"] - 1 : row["end_line"]])
                    self.assertEqual(row["sha256"], hashlib.sha256(segment).hexdigest())
                    self.assertEqual(row["size"], len(segment))
                    boundary = (
                        index["flows"][cycle + 1]["start_line"] - 1
                        if cycle < 9
                        else len(lines)
                    )
                    self.assertEqual(row["end_line"], boundary)
                    closed = index["resources"][cycle * 5 + 4]
                    self.assertLess(closed["line"], row["terminal_line"])
                    self.assertTrue(
                        all(value == 0 for value in closed["snapshot"].values())
                    )
                refs = document["references"]
                self.assertEqual(
                    refs["runtime_identity"]["path"],
                    "recovery-ten-runtime.identity.json",
                )
                self.assertEqual(
                    refs["runtime_capture"]["path"], "recovery-ten-runtime.capture.json"
                )
                self.assertEqual(
                    refs["ledger_recovery_ten"]["path"], "recovery-ten-ledger.json"
                )
                self.assertNotIn("cancellation_physical", refs)
                self.assertNotIn("watchdog_physical", refs)
                self.assertNotIn("physical_pending_reload", refs)
                if platform == "ios":
                    self.assertFalse(
                        self.log.with_suffix(".rf-host-identity.json").exists()
                    )
                    raw = self.log.with_suffix(".raw.log")
                    row = next(
                        row for row in document["files"] if row["path"] == raw.name
                    )
                    self.assertEqual(
                        row["sha256"], hashlib.sha256(raw.read_bytes()).hexdigest()
                    )
                    self.assertNotIn("artifact_install_identity", refs)

    def test_recovery_ten_closed_root_leak_and_invalid_seventeen_field_snapshots(
        self,
    ) -> None:
        bundle = self.recovery_ready()
        raw = recovery_runtime()
        changes = [
            lambda d: d.update(hubRoots=1),
            lambda d: d.update(handoffRoots=-1),
            lambda d: d.update(listeners=False),
            lambda d: d.update(connections=0.0),
            lambda d: d.update(watchdogs=2**53),
            lambda d: d.pop("backendLeases"),
            lambda d: d.update(untrackedRoot=0),
            lambda d: d.update(HubRoots=d.pop("hubRoots")),
        ]
        bad_logs = [self.recovery_snapshot_change(raw, change) for change in changes]
        bad_logs.append(
            self.recovery_snapshot_change(
                raw, lambda d: d.update(gattAttributes=-1), phase="active"
            )
        )
        # Also reject wire negative zero and duplicate JSON keys rather than normalizing them away.
        closed = next(line for line in raw.splitlines() if b"phase=closed:" in line)
        bad_logs.extend(
            [
                raw.replace(closed, closed.replace(b'"hubRoots":0', b'"hubRoots":-0')),
                raw.replace(closed, closed[:-1] + b',"hubRoots":0}'),
                raw.replace(closed, closed[:-1]),
            ]
        )
        for number, log in enumerate(bad_logs):
            with self.subTest(case=number):
                self.log.write_bytes(log)
                with self.assertRaises(evidence.EvidenceError):
                    self.finish(bundle)
                self.assertEqual(self.manifest()["status"], "failed")
                evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_recovery_ten_duplicate_cycles_resources_nonce_and_error_markers_fail(
        self,
    ) -> None:
        bundle = self.recovery_ready()
        raw = recovery_runtime().decode()
        start = evidence.RECOVERY_PREFIX + "cycle:0:start"
        closed = next(
            line
            for line in raw.splitlines()
            if "resources:cycle=0:phase=closed:" in line
        )
        passed = evidence.RECOVERY_PREFIX + "cycle:0:passed"
        close = f"runtime:runId={SESSION}:nonce=synthetic-one-boot:phase=close"
        logs = [
            raw.replace(start, start + "\n" + start),
            raw.replace("cycle:1:start", "cycle:0:start"),
            raw.replace(passed, passed + "\n" + passed),
            raw.replace(closed, closed + "\n" + closed),
            raw.replace(closed, "SWAP").replace(passed, closed).replace("SWAP", passed),
            raw.replace(close, close.replace("synthetic-one-boot", "foreign-boot")),
            raw.replace(
                close, close.replace(SESSION, "22345678-1234-4123-8123-123456789abc")
            ),
            raw.replace("passed:cycles=10:", "failed:cycles=10:"),
            raw + f"{evidence.RECOVERY_PREFIX}cycle:9:callback:error=synthetic\n",
            raw + f"{evidence.RECOVERY_PREFIX}native:bootstrap-failed:synthetic\n",
            raw.replace(close, close.replace("phase=close", "phase=missing")),
            raw + evidence.RECOVERY_PREFIX + close + "\n",
            raw.rstrip("\n"),
        ]
        for number, log in enumerate(logs):
            with self.subTest(case=number):
                self.log.write_bytes(log.encode())
                with self.assertRaises(evidence.EvidenceError):
                    self.finish(bundle)
                self.assertEqual(self.manifest()["status"], "failed")
                evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_recovery_ten_requires_capture_identity_physical_and_ledger_references(
        self,
    ) -> None:
        bundle = self.recovery_ready()
        refs = evidence._references(
            evidence._inventory(self.attempt), "android", self.log.name, "recovery-ten"
        )
        self.assertEqual(
            set(refs),
            {
                "runtime_identity",
                "runtime_capture",
                "artifact_install_identity",
                "fixture_request",
                "fixture_receipt",
                "physical_recovery_ten",
                "ledger_recovery_ten",
            },
        )
        for row in refs.values():
            with self.subTest(missing=row["path"]):
                path = self.attempt / row["path"]
                data, mode = path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
                path.unlink()
                with self.assertRaisesRegex(
                    evidence.EvidenceError, "missing required evidence"
                ):
                    self.finish(bundle)
                self.assertEqual(self.manifest()["status"], "failed")
                evidence.verify_bundle(self.attempt / evidence.MANIFEST)
                path.write_bytes(data)
                path.chmod(mode)
        self.finish(bundle)
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_recovery_ten_raw_file_and_segment_hash_drift_fail_offline(self) -> None:
        bundle = self.recovery_ready("ios")
        self.finish(bundle)
        path = self.attempt / evidence.MANIFEST
        original = path.read_text(encoding="utf-8")
        for update in ({"sha256": "0" * 64}, {"end_line": 999}, {"start_line": 1}):
            with self.subTest(update=update):
                document = json.loads(original)
                document["flow_index"]["flows"][0].update(update)
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(evidence.EvidenceError, "flow ranges"):
                    evidence.verify_bundle(path)
        path.write_text(original, encoding="utf-8")
        raw = self.log.with_suffix(".raw.log")
        raw.write_bytes(raw.read_bytes() + b"changed synthetic console\n")
        with self.assertRaisesRegex(evidence.EvidenceError, "raw evidence"):
            evidence.verify_bundle(path)

    def test_recovery_ten_partial_failure_preserves_primary_and_missing_cycles(
        self,
    ) -> None:
        bundle = self.recovery_ready()
        lines = recovery_runtime().splitlines(keepends=True)
        stop = next(
            i
            for i, line in enumerate(lines)
            if b"resources:cycle=3:phase=passive:" in line
        )
        self.log.write_bytes(b"".join(lines[:stop]))
        self.finish(
            bundle,
            completed=False,
            error=TimeoutError("original recovery timeout"),
            cleanup=["original provider cleanup error"],
        )
        document = self.manifest()
        self.assertEqual(
            document["primary_error"]["message"], "original recovery timeout"
        )
        self.assertEqual(
            document["cleanup_errors"], ["original provider cleanup error"]
        )
        self.assertEqual(
            [row["status"] for row in document["flow_index"]["flows"]],
            ["passed"] * 3 + ["partial"] + ["missing"] * 6,
        )
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_recovery_ten_missing_log_keeps_ten_missing_cycles(self) -> None:
        bundle = self.bundle("ios", scenario="recovery-ten")
        self.finish(bundle, completed=False, error=RuntimeError("preflight failed"))
        document = self.manifest()
        self.assertEqual(len(document["flow_index"]["flows"]), 10)
        self.assertTrue(
            all(row["status"] == "missing" for row in document["flow_index"]["flows"])
        )
        self.assertTrue(all(ref is None for ref in document["references"].values()))
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_recovery_ten_cannot_be_relabelled_as_legacy_continuous10(self) -> None:
        bundle = self.recovery_ready()
        self.finish(bundle)
        path = self.attempt / evidence.MANIFEST
        document = self.manifest()
        del document["scenario"]
        path.write_text(json.dumps(document), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify_bundle(path)
        self.assertEqual(path.read_bytes(), before)

    def pending_runtime(self, *, overlap: bool = True) -> bytes:
        """Synthetic markers only; no assertion about real engine teardown/GATT."""
        route = (
            f"runId={SESSION}:probeId=abcdefab-1234-4123-8123-123456789abc:"
            "commandId=12008:connectionId=12004:gattSessionId=12006:characteristicId=5001"
        )
        complete = "native:engine-close-complete:" + route
        lines = [
            f"generation=1:runtime-start:runId={SESSION}:nonce=synthetic-boot-1",
            "generation=1:native-ready:passed",
            "native:read-accepted:" + route,
            (
                "generation=1:read-dispatched:commandId=12008:connectionId=12004:"
                "gattSessionId=12006:characteristicId=5001:peripheralId=42"
            ),
            "native:engine-invalidation-close-started:" + route,
            "native:dispose-returned:" + route,
            "native:late-submit-rejected:" + route + ":source=synthetic:result=closed",
        ]
        if not overlap:
            lines.append(complete)
        lines.append(
            f"generation=2:runtime-start:runId={SESSION}:nonce=synthetic-boot-2"
        )
        if overlap:
            lines.append(complete)
        lines.extend(
            [
                "generation=2:native-ready:passed",
                "generation=2:initialize:passed:commandId=13000",
                (
                    "generation=2:read-dispatched:commandId=13008:connectionId=13004:"
                    "gattSessionId=13006:characteristicId=6002"
                ),
                (
                    "generation=2:read:passed:commandId=13008:connectionId=13004:"
                    "gattSessionId=13006:characteristicId=6002:value=00"
                ),
                "generation=2:close:passed",
                f"passed:runId={SESSION}",
            ]
        )
        return "".join(
            f"timestamp native-prefix {evidence.PENDING_PREFIX}{line}\r\n"
            for line in lines
        ).encode()

    def pending_ready(self) -> evidence.EvidenceBundle:
        bundle = self.bundle(scenario="pending-read-reload")
        bundle.prepare_artifact(self.apk())
        self.log.write_bytes(self.pending_runtime())
        self.required(scenario="pending-read-reload")
        (self.attempt / "coordination/pending-reload-physical.json").chmod(0o400)
        return bundle

    def test_pending_generations_hash_overlapping_and_nonoverlapping_ranges(
        self,
    ) -> None:
        bundle = self.pending_ready()
        for overlap in (True, False):
            with self.subTest(overlap=overlap):
                self.log.write_bytes(self.pending_runtime(overlap=overlap))
                self.finish(bundle)
                evidence.verify_bundle(self.attempt / evidence.MANIFEST)
                document = self.manifest()
                index = document["flow_index"]
                self.assertEqual(document["scenario"], "pending-read-reload")
                flows = index["flows"]
                self.assertEqual([row["generation"] for row in flows], [1, 2])
                self.assertEqual([row["status"] for row in flows], ["passed", "passed"])
                self.assertEqual(
                    flows[0]["end_line"] >= flows[1]["start_line"], overlap
                )
                lines = self.log.read_bytes().splitlines(keepends=True)
                for row in flows:
                    segment = b"".join(lines[row["start_line"] - 1 : row["end_line"]])
                    self.assertEqual(row["sha256"], hashlib.sha256(segment).hexdigest())
                    self.assertEqual(row["size"], len(segment))
                self.assertIn(
                    b"native:engine-close-complete:", lines[flows[0]["end_line"] - 1]
                )
                self.assertEqual(
                    set(document["references"]),
                    {
                        "runtime_identity",
                        "runtime_capture",
                        "artifact_install_identity",
                        "fixture_request",
                        "fixture_receipt",
                        "physical_pending_reload",
                    },
                )
                self.assertEqual(
                    document["references"]["physical_pending_reload"]["mode"], 0o400
                )
                self.assertFalse(
                    (self.attempt / "coordination/flow-8-rf-cancellation").exists()
                )

    def test_pending_nonce_duplicate_starts_barrier_close_and_errors_fail(self) -> None:
        bundle = self.pending_ready()
        raw = self.pending_runtime().decode()
        start = f"{evidence.PENDING_PREFIX}generation=2:runtime-start:runId={SESSION}:nonce=synthetic-boot-2"
        complete = next(
            line for line in raw.splitlines() if "native:engine-close-complete:" in line
        )
        ready = next(
            line
            for line in raw.splitlines()
            if "generation=2:native-ready:passed" in line
        )
        cases = [
            raw.replace("nonce=synthetic-boot-2", "nonce=synthetic-boot-1"),
            raw.replace(start, start + "\r\n" + start),
            raw.replace(complete, complete + "\r\n" + complete),
            raw.replace(complete, "SWAP")
            .replace(ready, complete)
            .replace("SWAP", ready),
            raw.replace("generation=2:close:passed", "generation=2:close:omitted"),
            raw.replace(
                "native:engine-close-complete:", "native:engine-close-not-complete:"
            ),
            raw.replace("generation=2:read:passed:", "generation=2:read:missing:"),
            raw.replace(
                start, start.replace(SESSION, "22345678-1234-4123-8123-123456789abc")
            ),
            raw.replace(f"passed:runId={SESSION}", "passed:runId=foreign-run"),
            raw.replace(f"runId={SESSION}", f"runId={SESSION.upper()}"),
            raw + f"{evidence.PENDING_PREFIX}generation=1:close:passed\n",
            raw + f"{evidence.PENDING_PREFIX}generation=2:failed:synthetic-error\n",
            raw
            + f"{evidence.PENDING_PREFIX}native:native-boundary-failed:runId={SESSION}\n",
            raw.rstrip("\r\n"),
        ]
        for number, log in enumerate(cases):
            with self.subTest(case=number):
                self.log.write_bytes(log.encode())
                with self.assertRaises(evidence.EvidenceError):
                    self.finish(bundle)
                self.assertEqual(self.manifest()["status"], "failed")
                evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_pending_references_required_and_physical_hash_cannot_drift(self) -> None:
        bundle = self.pending_ready()
        refs = evidence._references(
            evidence._inventory(self.attempt),
            "android",
            "runtime.log",
            "pending-read-reload",
        )
        for row in refs.values():
            with self.subTest(missing=row["path"]):
                path = self.attempt / row["path"]
                original = path.read_bytes()
                mode = stat.S_IMODE(path.stat().st_mode)
                path.unlink()
                with self.assertRaisesRegex(
                    evidence.EvidenceError, "missing required evidence"
                ):
                    self.finish(bundle)
                self.assertEqual(self.manifest()["status"], "failed")
                evidence.verify_bundle(self.attempt / evidence.MANIFEST)
                path.write_bytes(original)
                path.chmod(mode)
        self.finish(bundle)
        physical = self.attempt / "coordination/pending-reload-physical.json"
        physical.chmod(0o600)
        physical.write_bytes(b"changed synthetic physical proof, not RF")
        with self.assertRaisesRegex(evidence.EvidenceError, "raw evidence"):
            evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_pending_corrupt_segment_ranges_and_hashes_rejected_offline(self) -> None:
        bundle = self.pending_ready()
        self.finish(bundle)
        path = self.attempt / evidence.MANIFEST
        original = path.read_text(encoding="utf-8")
        for update in ({"sha256": "0" * 64}, {"end_line": 999}, {"start_line": 1}):
            with self.subTest(update=update):
                document = json.loads(original)
                document["flow_index"]["flows"][1].update(update)
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(evidence.EvidenceError, "flow ranges"):
                    evidence.verify_bundle(path)
        path.write_text(original, encoding="utf-8")
        evidence.verify_bundle(path)

    def test_pending_partial_failure_preserves_primary_and_cleanup_errors(self) -> None:
        bundle = self.pending_ready()
        lines = self.pending_runtime().splitlines(keepends=True)
        partial = b"".join(
            line for line in lines if b"native:engine-close-complete:" not in line
        )
        partial = partial[: partial.index(b"generation=2:native-ready")]
        self.log.write_bytes(partial)
        self.finish(
            bundle,
            completed=False,
            error=TimeoutError("original pending timeout"),
            cleanup=["original cleanup failure"],
        )
        document = self.manifest()
        self.assertEqual(
            document["primary_error"]["message"], "original pending timeout"
        )
        self.assertEqual(document["cleanup_errors"], ["original cleanup failure"])
        self.assertEqual(
            [row["status"] for row in document["flow_index"]["flows"]],
            ["partial", "partial"],
        )
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_pending_failure_before_log_keeps_two_missing_segments(self) -> None:
        bundle = self.bundle(scenario="pending-read-reload")
        self.finish(bundle, completed=False, error=RuntimeError("preflight failed"))
        document = self.manifest()
        self.assertEqual(
            [row["status"] for row in document["flow_index"]["flows"]],
            ["missing", "missing"],
        )
        self.assertTrue(all(row is None for row in document["references"].values()))
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_scenario_scope_legacy_v1_and_continuous10_relabel(
        self,
    ) -> None:
        for platform, scenario in (
            ("ios", "unknown"),
            ("android", "unknown"),
        ):
            with (
                self.subTest(platform=platform, scenario=scenario),
                self.assertRaises(evidence.EvidenceError),
            ):
                self.bundle(platform, scenario=scenario)
            self.assertFalse(self.attempt.exists())
        bundle, _ = self.ready()
        self.finish(bundle)
        path = self.attempt / evidence.MANIFEST
        original = path.read_text(encoding="utf-8")
        self.assertEqual(self.manifest()["scenario"], "continuous10")
        legacy = json.loads(original)
        del legacy["scenario"]
        path.write_text(json.dumps(legacy), encoding="utf-8")
        before = path.read_bytes()
        evidence.verify_bundle(path)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("scenario", json.loads(path.read_bytes()))
        for scenario in ("unknown", None, "pending-read-reload"):
            document = json.loads(original)
            document["scenario"] = scenario
            path.write_text(json.dumps(document), encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaises(evidence.EvidenceError):
                evidence.verify_bundle(path)
            self.assertEqual(
                path.read_bytes(), before
            )  # No historical backfill/rewrite.
        path.write_text(original, encoding="utf-8")
        evidence.verify_bundle(path)

    def test_pending_without_scenario_is_not_accepted_as_legacy_continuous10(
        self,
    ) -> None:
        bundle = self.pending_ready()
        self.finish(bundle)
        path = self.attempt / evidence.MANIFEST
        document = self.manifest()
        del document["scenario"]
        path.write_text(json.dumps(document), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify_bundle(path)
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_v1_preserves_strict_artifact_reference_and_ten_flow_checks(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            self.attempt = self.root / platform
            self.log = self.attempt / "runtime.log"
            bundle, _ = self.ready(platform)
            self.finish(bundle)
            path = self.attempt / evidence.MANIFEST
            legacy = self.manifest()
            del legacy["scenario"]
            original = json.dumps(legacy)
            path.write_text(original, encoding="utf-8")
            evidence.verify_bundle(path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            for change in (
                lambda d: d["artifact"]["fingerprint"]["items"][0].update(
                    sha256="0" * 64
                ),
                lambda d: d["references"]["fixture_receipt"].update(sha256="0" * 64),
                lambda d: d["flow_index"]["flows"][0].update(sha256="0" * 64),
                lambda d: d["flow_index"].update(events=[]),
            ):
                with self.subTest(platform=platform, change=change):
                    document = json.loads(original)
                    change(document)
                    path.write_text(json.dumps(document), encoding="utf-8")
                    before = path.read_bytes()
                    with self.assertRaises(evidence.EvidenceError):
                        evidence.verify_bundle(path)
                    self.assertEqual(path.read_bytes(), before)
            path.write_text(original, encoding="utf-8")
            evidence.verify_bundle(path)

    def test_android_freezes_full_archive_and_items_not_mutable_source(self) -> None:
        bundle, snapshot = self.ready()
        source = self.root / "synthetic.apk"
        original = source.read_bytes()
        self.assertEqual(snapshot.read_bytes(), original)
        source.write_bytes(b"later unrelated build")
        bundle.verify_artifact()
        self.finish(bundle)
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        document = self.manifest()
        artifact = document["artifact"]
        self.assertEqual(document["status"], "passed")
        self.assertEqual(
            artifact["fingerprint"]["archive"]["sha256"],
            hashlib.sha256(original).hexdigest(),
        )
        with zipfile.ZipFile(snapshot) as archive:
            for row in artifact["fingerprint"]["items"]:
                self.assertEqual(
                    row["sha256"], hashlib.sha256(archive.read(row["path"])).hexdigest()
                )
        self.assertEqual(
            artifact["fingerprint"]["native_images"],
            [
                "lib/arm64-v8a/libBleHubReactNative.so",
                "lib/arm64-v8a/libblehub_uniffi.so",
            ],
        )
        self.assertNotIn(evidence.MANIFEST, [row["path"] for row in document["files"]])
        self.assertFalse(
            any(row["path"].startswith("candidate/") for row in document["files"])
        )

    def test_android_mixed_case_zip_entries_freeze_and_verify_separately(self) -> None:
        resources = [
            ("res/9N.9.png", b"synthetic uppercase resource"),
            ("res/9n.9.png", b"synthetic lowercase resource"),
        ]
        source = self.apk(extra=resources)
        bundle = self.bundle()
        snapshot = bundle.prepare_artifact(source)
        self.assertEqual(snapshot.read_bytes(), source.read_bytes())
        bundle.verify_artifact()
        self.log.write_bytes(self.runtime())
        self.required()
        self.finish(bundle)
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        items = {
            row["path"]: row
            for row in self.manifest()["artifact"]["fingerprint"]["items"]
        }
        for name, content in resources:
            self.assertEqual(items[name]["sha256"], hashlib.sha256(content).hexdigest())
        self.assertNotEqual(
            items[resources[0][0]]["sha256"], items[resources[1][0]]["sha256"]
        )

    def test_android_exact_duplicate_and_file_directory_conflicts_still_fail(
        self,
    ) -> None:
        for extra in (
            [("res/9n.9.png", b"first"), ("res/9n.9.png", b"duplicate")],
            [("res/resource", b"file"), ("res/resource/", b"")],
            [("res/resource/", b""), ("res/resource", b"file")],
        ):
            with (
                self.subTest(entries=extra),
                self.assertRaisesRegex(evidence.EvidenceError, "duplicate APK entry"),
            ):
                evidence._apk(self.apk(extra=extra))

    def test_android_rejects_bad_duplicate_unsafe_and_linked_zip_entries(self) -> None:
        link = zipfile.ZipInfo("lib/evil.so")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        fifo = zipfile.ZipInfo("fifo")
        fifo.create_system = 3
        fifo.external_attr = (stat.S_IFIFO | 0o644) << 16
        for name in (
            "assets/index.android.bundle",
            "../foreign",
            "/absolute",
            "C:/foreign",
            "a\\b",
            "a/./b",
            "a//b",
            link,
            fifo,
        ):
            with (
                self.subTest(entry=name),
                self.assertRaises((evidence.EvidenceError, zipfile.BadZipFile)),
            ):
                evidence._apk(self.apk(extra=[(name, b"unsafe")]))
        path = self.apk(extra=[("nulXtail", b"unsafe")])
        path.write_bytes(path.read_bytes().replace(b"nulXtail", b"nul\x00tail"))
        with self.assertRaisesRegex(evidence.EvidenceError, "NUL"):
            evidence._apk(path)
        path = self.apk()
        path.write_bytes(b"not a ZIP")
        with self.assertRaises(evidence.EvidenceError):
            evidence._apk(path)
        path = self.apk()
        path.write_bytes(
            path.read_bytes().replace(b"fake JS; not RF", b"corrupt;not RF!", 1)
        )
        with self.assertRaises(zipfile.BadZipFile):
            evidence._apk(path)

    def test_android_rejects_required_payload_and_abi_coverage_gaps(self) -> None:
        for missing in (
            "assets/index.android.bundle",
            "classes.dex",
            "lib/arm64-v8a/libBleHubReactNative.so",
            "lib/arm64-v8a/libblehub_uniffi.so",
        ):
            with self.subTest(missing=missing):
                path = self.apk(omit=missing)
                if missing == "classes.dex":
                    # Remove the second DEX too, to test the actual requirement.
                    with zipfile.ZipFile(path) as archive:
                        remaining = [
                            (row.filename, archive.read(row))
                            for row in archive.infolist()
                            if row.filename != "classes2.dex"
                        ]
                    with zipfile.ZipFile(path, "w") as archive:
                        for name, content in remaining:
                            archive.writestr(name, content)
                with self.assertRaises(evidence.EvidenceError):
                    evidence._apk(path)
        with self.assertRaisesRegex(evidence.EvidenceError, "missing.*for x86_64"):
            evidence._apk(self.apk(extra=[("lib/x86_64/libreactnative.so", b"fake")]))
        with self.assertRaisesRegex(evidence.EvidenceError, "unexpected BleHub"):
            evidence._apk(
                self.apk(extra=[("lib/arm64-v8a/libblehub_test.so", b"fake")])
            )

    def test_bounded_archive_directory_expansion_and_streaming_copy(self) -> None:
        path = self.apk()
        with (
            mock.patch.object(evidence, "MAX_ENTRIES", 2),
            self.assertRaisesRegex(evidence.EvidenceError, "directory"),
        ):
            evidence._apk(path)
        with (
            mock.patch.object(evidence, "MAX_TOTAL_BYTES", 8),
            self.assertRaisesRegex(evidence.EvidenceError, "expanded"),
        ):
            evidence._apk(path)
        with (
            mock.patch.object(evidence, "MAX_FILE_BYTES", 8),
            self.assertRaisesRegex(evidence.EvidenceError, "oversized"),
        ):
            evidence._apk(path)
        bundle = self.bundle()
        with mock.patch.object(evidence, "CHUNK_BYTES", 7):
            snapshot = bundle.prepare_artifact(path)
            bundle.verify_artifact()
        self.assertEqual(snapshot.read_bytes(), path.read_bytes())

    def test_regular_source_and_source_copy_signatures_are_enforced(self) -> None:
        bundle = self.bundle()
        path = self.apk()
        symlink = self.root / "linked.apk"
        symlink.symlink_to(path)
        with self.assertRaisesRegex(evidence.EvidenceError, "symlink"):
            bundle.prepare_artifact(symlink)
        original = evidence._copy_file

        def change_source(source: Path, destination: Path) -> None:
            original(source, destination)
            with source.open("ab") as stream:
                stream.write(b"source changed after copy")

        with (
            mock.patch.object(evidence, "_copy_file", side_effect=change_source),
            self.assertRaises((evidence.EvidenceError, zipfile.BadZipFile)),
        ):
            bundle.prepare_artifact(path)
        self.assertIsNone(bundle.document["artifact"])
        self.finish(bundle, completed=False, error=RuntimeError("freeze failed"))
        self.assertEqual(self.manifest()["status"], "failed")

    def test_candidate_drift_fails_finish_and_offline_verification(self) -> None:
        bundle, snapshot = self.ready()
        with zipfile.ZipFile(snapshot, "a") as archive:
            archive.writestr("new-resource", b"drift")
        with self.assertRaisesRegex(evidence.EvidenceError, "drift"):
            bundle.verify_artifact()
        with self.assertRaises(evidence.EvidenceError):
            self.finish(bundle)
        self.assertEqual(self.manifest()["status"], "failed")
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_candidate_extra_child_or_symlink_is_not_ignored(self) -> None:
        bundle, snapshot = self.ready()
        extra = snapshot.parent / "unbound"
        extra.write_bytes(b"not inventoried")
        with self.assertRaisesRegex(evidence.EvidenceError, "inventory"):
            bundle.verify_artifact()
        extra.unlink()
        original = self.root / "outside.apk"
        snapshot.rename(original)
        snapshot.symlink_to(original)
        with self.assertRaisesRegex(evidence.EvidenceError, "symlink"):
            bundle.verify_artifact()

    def test_ios_static_app_tree_modes_native_and_attempt_identity(self) -> None:
        bundle, snapshot = self.ready("ios")
        self.log.with_suffix(".rf-host-identity.json").unlink()
        self.required("ios", ios_attempt=True)
        # Optional native images are recognized by bytes, not names/framework requirements.
        dylib = self.root / "Synthetic.app" / "Optional.debug.dylib"
        self.assertFalse(dylib.exists())
        self.assertEqual(stat.S_IMODE((snapshot / "Synthetic").stat().st_mode), 0o755)
        self.finish(bundle)
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        document = self.manifest()
        fingerprint = document["artifact"]["fingerprint"]
        self.assertEqual(fingerprint["native_images"], ["Synthetic"])
        self.assertEqual(fingerprint["executable"], "Synthetic")
        self.assertEqual(fingerprint["bundle_id"], BUNDLE_ID)
        self.assertEqual(
            fingerprint["tree_sha256"], evidence._digest(fingerprint["items"])
        )
        self.assertEqual(
            document["references"]["runtime_identity"]["path"],
            "runtime.attempt.rf-host-identity.json",
        )
        (snapshot / "Synthetic").chmod(0o644)
        with self.assertRaises(evidence.EvidenceError):
            evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_ios_complete_tree_hash_includes_resources_and_additional_macho(
        self,
    ) -> None:
        app = self.app()
        (app / "Optional.debug.dylib").write_bytes(
            bytes.fromhex("feedfacf") + b"synthetic"
        )
        bundle = self.bundle("ios")
        snapshot = bundle.prepare_artifact(app)
        self.assertEqual(
            bundle.document["artifact"]["fingerprint"]["native_images"],
            [
                "Optional.debug.dylib",
                "Synthetic",
            ],
        )
        (snapshot / "Resources/asset").write_bytes(b"resource mutation")
        with self.assertRaisesRegex(evidence.EvidenceError, "drift"):
            bundle.verify_artifact()

    def test_ios_rejects_foreign_or_missing_bundle_identity_before_freeze(self) -> None:
        app = self.app()
        bundle = self.bundle("ios")
        for bundle_id in (None, BUNDLE_ID + ".foreign", 42):
            document = {"CFBundleExecutable": "Synthetic"}
            if bundle_id is not None:
                document["CFBundleIdentifier"] = bundle_id
            (app / "Info.plist").write_bytes(plistlib.dumps(document))
            with (
                self.subTest(bundle_id=bundle_id),
                self.assertRaisesRegex(evidence.EvidenceError, "CFBundleIdentifier"),
            ):
                bundle.prepare_artifact(app)
            self.assertFalse((self.attempt / "candidate").exists())
        (app / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleExecutable": "Synthetic",
                    "CFBundleIdentifier": BUNDLE_ID,
                }
            )
        )
        snapshot = bundle.prepare_artifact(app)
        bundle.verify_artifact()
        (snapshot / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleExecutable": "Synthetic",
                    "CFBundleIdentifier": BUNDLE_ID + ".foreign",
                }
            )
        )
        with self.assertRaisesRegex(evidence.EvidenceError, "CFBundleIdentifier"):
            bundle.verify_artifact()

    def test_ios_rejects_unsafe_plist_symlinks_special_and_missing_payloads(
        self,
    ) -> None:
        app = self.app()
        for executable in ("../outside", "/foreign", "C:\\foreign", "not-macho", None):
            with self.subTest(executable=executable):
                document = (
                    {} if executable is None else {"CFBundleExecutable": executable}
                )
                document["CFBundleIdentifier"] = BUNDLE_ID
                (app / "Info.plist").write_bytes(plistlib.dumps(document))
                with self.assertRaises(evidence.EvidenceError):
                    evidence._app(app)
        (app / "Info.plist").write_bytes(
            plistlib.dumps(
                {"CFBundleExecutable": "Synthetic", "CFBundleIdentifier": BUNDLE_ID}
            )
        )
        linked = app / "Resources/linked"
        linked.symlink_to(self.project, target_is_directory=True)
        with self.assertRaisesRegex(evidence.EvidenceError, "symlink/special"):
            evidence._app(app)
        linked.unlink()
        if hasattr(os, "mkfifo"):
            os.mkfifo(linked)
            with self.assertRaisesRegex(evidence.EvidenceError, "symlink/special"):
                evidence._app(app)
            linked.unlink()
        (app / "main.jsbundle").unlink()
        with self.assertRaisesRegex(evidence.EvidenceError, "main.jsbundle"):
            evidence._app(app)

    def test_partial_failure_preserves_primary_without_requiring_missing_proof(
        self,
    ) -> None:
        bundle = self.bundle()
        bundle.prepare_artifact(self.apk())
        raw = self.runtime(count=2, close=False)
        raw += (
            f"{evidence.PREFIX}flow-started:index=2:mode=rf-gatt\npartial tail".encode()
        )
        self.log.write_bytes(raw)
        self.finish(
            bundle,
            completed=False,
            error=TimeoutError("original RF timeout"),
            cleanup=["cleanup failed"],
        )
        document = self.manifest()
        self.assertEqual(document["primary_error"]["message"], "original RF timeout")
        self.assertEqual(document["cleanup_errors"], ["cleanup failed"])
        self.assertEqual(
            [row["status"] for row in document["flow_index"]["flows"]],
            ["passed", "passed", "partial", *(["missing"] * 7)],
        )
        self.assertTrue(all(value is None for value in document["references"].values()))
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_missing_log_and_artifact_can_be_published_as_failed(self) -> None:
        bundle = self.bundle()
        self.finish(bundle, completed=False, error=RuntimeError("build failed"))
        self.assertEqual(self.manifest()["status"], "failed")
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_cleanup_failure_without_primary_cannot_return_a_pass(self) -> None:
        bundle, _ = self.ready()
        with self.assertRaises(evidence.EvidenceError):
            self.finish(bundle, cleanup=["stop-owned failed"])
        document = self.manifest()
        self.assertEqual(document["status"], "failed")
        self.assertEqual(document["cleanup_errors"], ["stop-owned failed"])
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_raw_line_ranges_segment_hashes_and_hash_only_references(self) -> None:
        bundle, _ = self.ready()
        nested = self.attempt / "raw/subprocess.bin"
        nested.parent.mkdir()
        nested.write_bytes(b"\x00\xffraw bytes")
        self.finish(bundle)
        document = self.manifest()
        lines = self.log.read_bytes().splitlines(keepends=True)
        for row in document["flow_index"]["flows"]:
            segment = b"".join(lines[row["start_line"] - 1 : row["end_line"]])
            self.assertEqual(row["sha256"], hashlib.sha256(segment).hexdigest())
            self.assertEqual(row["size"], len(segment))
        files = {row["path"]: row for row in document["files"]}
        for reference in document["references"].values():
            self.assertEqual(reference, files[reference["path"]])
            self.assertNotIn("synthetic", reference)
        self.assertEqual(
            files["raw/subprocess.bin"]["sha256"],
            hashlib.sha256(nested.read_bytes()).hexdigest(),
        )
        nested.write_bytes(b"drift")
        with self.assertRaisesRegex(evidence.EvidenceError, "raw evidence"):
            evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_android_install_identity_is_required_and_hash_referenced(self) -> None:
        bundle, _ = self.ready()
        identity = self.attempt / "artifact-install-identity.json"
        content = identity.read_bytes()
        identity.unlink()
        with self.assertRaisesRegex(
            evidence.EvidenceError, "missing required evidence"
        ):
            self.finish(bundle)
        document = self.manifest()
        self.assertEqual(document["status"], "failed")
        self.assertIsNone(document["references"]["artifact_install_identity"])
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        identity.write_bytes(content)
        self.finish(bundle)
        document = self.manifest()
        self.assertEqual(document["status"], "passed")
        self.assertEqual(
            document["references"]["artifact_install_identity"]["sha256"],
            hashlib.sha256(content).hexdigest(),
        )
        identity.write_bytes(b"modified synthetic identity; not RF evidence")
        with self.assertRaisesRegex(evidence.EvidenceError, "raw evidence"):
            evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_each_required_file_missing_forces_failed_manifest_and_raise(self) -> None:
        bundle, _ = self.ready()
        paths = [
            row["path"]
            for row in evidence._references(
                evidence._inventory(self.attempt), "android", "runtime.log"
            ).values()
        ]
        for relative in paths:
            with self.subTest(missing=relative):
                path = self.attempt / relative
                saved = path.read_bytes()
                path.unlink()
                with self.assertRaises(evidence.EvidenceError):
                    self.finish(bundle)
                self.assertEqual(self.manifest()["status"], "failed")
                evidence.verify_bundle(self.attempt / evidence.MANIFEST)
                path.write_bytes(saved)
        self.finish(bundle)
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_flow_duplicates_order_partial_terminal_and_identity_drift_fail_closed(
        self,
    ) -> None:
        bundle, _ = self.ready()
        original = self.runtime().decode()
        start = f"{evidence.PREFIX}flow-started:index=0:mode=rf"
        close = f"runtime:session={SESSION}:nonce=synthetic-runtime:phase=close"
        cases = [
            original.replace(
                "flow-started:index=1:mode=rf-connect",
                "flow-started:index=2:mode=rf-gatt",
            ),
            original + f"{start}\n",
            original + start,  # Truncated extra attempt must not be ignored.
            original.replace(
                "flow-passed:index=5:mode=rf-notification",
                "flow-failed:index=5:mode=rf-notification:reason=fake",
            ),
            original.replace(
                close, close.replace(SESSION, "22345678-1234-4123-8123-123456789abc")
            ),
            original.replace(
                close, close.replace("synthetic-runtime", "foreign-runtime")
            ),
            original.replace("passedCount=10", "passedCount=9"),
            original
            + f"{evidence.PREFIX}passed:flows=10:passedCount=10:failureCount=0\n",
            original.rstrip("\r\n"),
            original.replace("flow-passed:index=9:mode=rf-watchdog", "SWAP")
            .replace(close, "flow-passed:index=9:mode=rf-watchdog")
            .replace("SWAP", close),
        ]
        for index, raw in enumerate(cases):
            with self.subTest(case=index):
                self.log.write_text(raw, encoding="utf-8", newline="")
                with self.assertRaises(evidence.EvidenceError):
                    self.finish(bundle)
                self.assertEqual(self.manifest()["status"], "failed")
                evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_offline_rejects_manifest_ref_range_hash_and_path_tampering(self) -> None:
        bundle, _ = self.ready()
        self.finish(bundle)
        path = self.attempt / evidence.MANIFEST
        original = path.read_text(encoding="utf-8")
        for change in (
            lambda d: d["references"]["fixture_receipt"].update(sha256="0" * 64),
            lambda d: d["flow_index"]["flows"][0].update(end_line=999),
            lambda d: d["flow_index"]["flows"][0].update(sha256="0" * 64),
            lambda d: d["artifact"].update(path="candidate/../../outside.apk"),
            lambda d: d.update(runtime_log="../outside.log"),
            lambda d: d.update(cleanup_errors=["hidden cleanup failure"]),
            lambda d: d.update(completed=False),
            lambda d: d.pop("host"),
            lambda d: d.update(finished_at="2000-01-01T00:00:00+00:00"),
            lambda d: d["artifact"].update(source_signature_sha256="0" * 64),
        ):
            document = json.loads(original)
            change(document)
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(evidence.EvidenceError):
                evidence.verify_bundle(path)
        path.write_text(original, encoding="utf-8")
        evidence.verify_bundle(path)

    def test_internal_read_failure_publishes_failed_then_raises_for_runner_note(
        self,
    ) -> None:
        bundle, _ = self.ready()
        primary = RuntimeError("original runtime error")
        with (
            mock.patch.object(
                evidence, "_inventory", side_effect=OSError("unreadable evidence")
            ),
            self.assertRaisesRegex(OSError, "unreadable") as caught,
        ):
            self.finish(bundle, completed=False, error=primary)
        primary.add_note(f"bundle finalization failed: {caught.exception}")
        document = self.manifest()
        self.assertEqual(document["status"], "failed")
        self.assertEqual(document["primary_error"]["message"], "original runtime error")
        self.assertIn("OSError", document["evidence_errors"][-1])
        self.assertTrue(primary.__notes__)

    def test_whole_attempt_budget_counts_candidate_and_raw_evidence(self) -> None:
        bundle, _ = self.ready()
        self.finish(bundle)
        total = sum(
            info.st_size
            for _, _, info in evidence._walk(self.attempt)
            if stat.S_ISREG(info.st_mode)
        )
        with (
            mock.patch.object(evidence, "MAX_TOTAL_BYTES", total - 1),
            self.assertRaisesRegex(evidence.EvidenceError, "total bytes"),
        ):
            evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        evidence.verify_bundle(self.attempt / evidence.MANIFEST)

    def test_raw_symlink_and_new_unreferenced_file_are_not_ignored(self) -> None:
        bundle, _ = self.ready()
        self.finish(bundle)
        extra = self.attempt / "unexpected.log"
        extra.write_bytes(b"new evidence after finalization")
        with self.assertRaisesRegex(evidence.EvidenceError, "raw evidence"):
            evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        extra.unlink()
        extra.symlink_to(self.log)
        with self.assertRaisesRegex(evidence.EvidenceError, "symlink/special"):
            evidence.verify_bundle(self.attempt / evidence.MANIFEST)
        with self.assertRaisesRegex(evidence.EvidenceError, "symlink/special"):
            self.finish(bundle, completed=False, error=RuntimeError("original failure"))
        self.assertEqual(self.manifest()["status"], "failed")
        self.assertEqual(
            self.manifest()["primary_error"]["message"], "original failure"
        )

    def test_offline_cli_needs_no_hardware_and_reports_drift(self) -> None:
        bundle, _ = self.ready()
        self.finish(bundle)
        path = self.attempt / evidence.MANIFEST
        command = [
            sys.executable,
            "-m",
            "tools.react_native.rf_evidence_bundle",
            str(path),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not RF reauthorization", result.stdout)
        self.log.write_bytes(b"changed")
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=False
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid", result.stderr)
        with (
            mock.patch.object(sys, "argv", ["bundle", str(path)]),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(evidence.main(), 1)


if __name__ == "__main__":
    unittest.main()
