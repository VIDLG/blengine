"""Pure filesystem fixtures, never device, session authority or RF evidence."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tools.react_native import bleng_lifecycle_evidence as evidence

RUN = "12345678-1234-4234-8234-123456789abc"
HOST = "host-session-1"


class LifecycleEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.attempt = self.root / "attempt"
        self.attempt.mkdir()
        self.source = self.root / "source.apk"
        self.apk()
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def apk(self, *, omit: str = "", extra=()) -> None:
        files = {
            "AndroidManifest.xml": b"tiny manifest; not a package gate",
            "assets/index.android.bundle": b"tiny bundled JS",
            "classes.dex": b"dex\n035\x00tiny fixture",
            "lib/arm64-v8a/libbleng_uniffi.so": b"\x7fELFtiny BLeng fixture",
            "lib/arm64-v8a/libBleHubReactNativeBleng.so": b"\x7fELFtiny Nitro fixture",
        }
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.source, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, raw in [*files.items(), *extra]:
                    if name != omit:
                        archive.writestr(name, raw)

    def freeze(self, **kwargs) -> None:
        evidence.freeze_lifecycle_apk(
            self.source,
            self.attempt / "candidate.apk",
            expected_sha256=kwargs.pop("expected_sha256", self.sha),
            deadline=kwargs.pop("deadline", time.monotonic() + 10),
            **kwargs,
        )

    def document(self, name: str, value: dict) -> None:
        path = self.attempt / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def success(self) -> dict:
        self.freeze()
        result = {
            "schema_version": 1,
            "scope": "formal-android-managed-lifecycle",
            "run_id": RUN,
            "platform": "android",
            "device": "filesystem-fixture-only",
            "app_id": "dev.blehub.formal",
            "apk_sha256": self.sha,
            "scenario_status": "passed",
            "session_status": "passed",
            "host_gate_status": "pending",
            "status": "passed",
            "observations": ["idle", "started", "closed"],
            "startup_snapshot": {
                "state": "poweredOff",
                "scan_authorization": "denied",
                "connection_authorization": "notDetermined",
            },
            "primary_error": None,
            "cleanup_errors": [],
            "phone_cleanup_errors": [],
            "phone_status": "passed",
            "cleanup_order": [
                "automation.close",
                "candidate.finalize",
                "bundle.prepare",
                "phone.finish",
            ],
            "rf": "not_run",
        }
        identity = {
            "serial": result["device"],
            "package": result["app_id"],
            "pid": 123,
            "uid": 10123,
            "cmdline": result["app_id"],
            "start_ticks": 456,
            "boot_id": RUN,
            "base_apk_path": "/data/app/fixture/base.apk",
            "apk_sha256": self.sha,
        }
        self.document("candidate/bound-identity.json", identity)
        self.document(
            "candidate/finalization.json",
            {
                "run_id": RUN,
                "status": "passed",
                "action": "retain",
                "error": None,
                "identity": identity,
            },
        )
        log = self.attempt / "candidate/003-install.log"
        log.write_bytes(b"Success\n")
        self.document(
            "candidate/003-install.json",
            {
                "run_id": RUN,
                "serial": result["device"],
                "apk_sha256": self.sha,
                "status": "passed",
                "error": None,
                "grant": False,
                "use_root": True,
                "test_only": False,
                "stdout_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            },
        )
        self.document(
            "automation/start.json",
            {
                "run_id": RUN,
                "platform": "android",
                "device": result["device"],
                "app_id": result["app_id"],
                "status": "passed",
                "session_id": HOST,
                "error": None,
            },
        )
        self.document(
            "automation/close.json",
            {
                "run_id": RUN,
                "session_id": HOST,
                "session_closed": True,
                "server_closed": True,
                "status": "passed",
                "errors": [],
                "platform_backend_cleanup": "passed",
                "helper_restoration": "passed",
            },
        )
        self.document(
            "platform/bound-session.json",
            {
                "run_id": RUN,
                "host_session_id": HOST,
                "device_session_id": "downstream-1",
            },
        )
        self.document(
            "platform/finalized.json",
            {
                "run_id": RUN,
                "status": "passed",
                "session_deleted": True,
                "host_gate_status": "pending",
                "resource_zero": "not-claimed",
            },
        )
        self.document(
            "helpers/restored.json",
            {
                "run_id": RUN,
                "status": "restored",
                "preparation_error": None,
                "host_gate_status": "pending",
                "phone_released": False,
            },
        )
        self.document(
            "phone.json",
            {
                "run_id": RUN,
                "platform": "android",
                "device": result["device"],
                "status": "passed",
                "actions": [],
                "primary_error": None,
                "cleanup_errors": [],
                "initial_state": None,
                "final_state": None,
            },
        )
        prepared = copy.deepcopy(result)
        prepared.update(
            host_gate_status="pending",
            session_status="not_run",
            status="failed",
            phone_status="not_acquired",
        )
        prepared["cleanup_order"] = prepared["cleanup_order"][:-1]
        self.document("prepared-result.json", prepared)
        return result

    def test_idle_control_has_distinct_scope_and_cannot_claim_lifecycle_progress(self):
        result = self.success()
        self.close_diagnostics()
        result.update(
            scope="formal-android-managed-idle-control",
            observations=["idle"],
            startup_snapshot={},
        )
        prepared = json.loads((self.attempt / "prepared-result.json").read_bytes())
        prepared.update(
            scope=result["scope"], observations=["idle"], startup_snapshot={}
        )
        self.document("prepared-result.json", prepared)
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        for changes in (
            {"scope": "formal-android-managed-lifecycle"},
            {"observations": ["idle", "started", "closed"]},
            {"startup_snapshot": {"state": "poweredOn"}},
            {"observations": []},
        ):
            with self.subTest(changes=changes):
                self.document("result.json", result | changes)
                self.document("prepared-result.json", prepared | changes)
                self.reseal_file("result.json")
                self.reseal_file("prepared-result.json")
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)

    def test_start_hold_requires_its_own_record_and_exact_observations(self):
        result = self.success()
        result["scope"] = "formal-android-managed-start-hold-control"
        prepared = json.loads((self.attempt / "prepared-result.json").read_bytes())
        prepared["scope"] = result["scope"]
        self.document("prepared-result.json", prepared)
        proof = {
            "schema_version": 1,
            "run_id": RUN,
            "scope": result["scope"],
            "browse": False,
            "quiet_seconds": 5,
            "hold_observation": {"started_monotonic": 10, "finished_monotonic": 15},
        }
        with self.assertRaises(ValueError):
            evidence.publish_lifecycle_bundle(self.attempt, result)
        self.document("automation/start-hold-control.json", proof)
        with self.assertRaises(ValueError):
            evidence.publish_lifecycle_bundle(self.attempt, result)
        self.close_diagnostics()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        for changes in (
            {"scope": "formal-android-managed-lifecycle"},
            {
                "scope": "formal-android-managed-idle-control",
                "observations": ["idle"],
                "startup_snapshot": {},
            },
            {"observations": []},
            {"observations": ["idle", "closed"]},
            {"observations": ["idle", "started", "started", "closed"]},
            {"observations": ["started", "idle", "closed"]},
            {"observations": ["idle", "started", "closed", "closed"]},
            {"host_gate_status": "passed"},
            {"startup_snapshot": {}},
        ):
            with self.subTest(changes=changes):
                self.document("result.json", result | changes)
                self.document("prepared-result.json", prepared | changes)
                self.reseal_file("result.json")
                self.reseal_file("prepared-result.json")
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)
        self.document("result.json", result)
        self.document("prepared-result.json", prepared)
        self.reseal_file("result.json")
        self.reseal_file("prepared-result.json")
        for changes in (
            {"scope": "formal-android-managed-lifecycle"},
            {"run_id": "12345678-1234-4234-8234-000000000000"},
            {"browse": True},
            {"quiet_seconds": True},
            {"hold_observation": {}},
            {
                "hold_observation": {
                    "started_monotonic": 10,
                    "finished_monotonic": 14.99,
                }
            },
            {"hold_observation": {"started_monotonic": True, "finished_monotonic": 15}},
        ):
            with self.subTest(proof_changes=changes):
                self.document("automation/start-hold-control.json", proof | changes)
                self.reseal_file("automation/start-hold-control.json")
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)

    def test_failed_start_hold_still_rejects_scope_washing_and_invalid_progress(self):
        result = self.success()
        result.update(
            scope="formal-android-managed-start-hold-control",
            status="failed",
            scenario_status="failed",
            host_gate_status="failed",
            primary_error="TimeoutError: hold did not finish",
            observations=["idle", "started"],
        )
        prepared = json.loads((self.attempt / "prepared-result.json").read_bytes())
        prepared.update(
            {
                key: result[key]
                for key in ("scope", "scenario_status", "primary_error", "observations")
            }
        )
        self.document("prepared-result.json", prepared)
        self.document(
            "automation/start-hold-control.json",
            {
                "schema_version": 1,
                "run_id": RUN,
                "scope": result["scope"],
                "browse": False,
                "quiet_seconds": 5,
                "hold_observation": {"started_monotonic": 10},
            },
        )
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        for changes in (
            {"scope": "formal-android-managed-lifecycle"},
            {"observations": ["closed"]},
            {"observations": ["idle", "started", "started"]},
        ):
            with self.subTest(changes=changes):
                self.document("result.json", result | changes)
                self.document("prepared-result.json", prepared | changes)
                self.reseal_file("result.json")
                self.reseal_file("prepared-result.json")
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)

    def close_diagnostics(self, original: str = "") -> None:
        """Recorded SDK receipts only; no controller, lease or device dispatch."""
        prefix = "platform/close-diagnostics/"
        tag = "log.tag.BleHubBlengClose"
        identity = self.attempt / "candidate/bound-identity.json"
        app = json.loads(identity.read_bytes())
        pin = [1, 2, hashlib.sha256(identity.read_bytes()).hexdigest()]
        commands = [
            (["shell", "getprop", tag], original + "\n"),
            (["shell", "setprop", tag, "DEBUG"], ""),
            (["shell", "getprop", tag], "DEBUG\n"),
            (
                [
                    "logcat",
                    "-d",
                    "-v",
                    "threadtime",
                    "--pid",
                    str(app["pid"]),
                    "-t",
                    "1000",
                    "BleHubBlengClose:D",
                    "ReactNativeJS:V",
                    "*:S",
                ],
                "",  # Empty diagnostics do not prove or disprove Close completion.
            ),
            (["shell", "getprop", tag], "DEBUG\n"),
            (["shell", "setprop", tag, original or '""'], ""),
            (["shell", "getprop", tag], original + "\n"),
        ]
        for index, (args, stdout) in enumerate(commands, 1):
            doc = {
                "run_id": RUN,
                "argv": ["/mock/adb", "-s", app["serial"], *args],
                "stdout": stdout,
                "returncode": 0,
                "stdout_bytes": len(stdout.encode()),
                "error": None,
            }
            if args[0] == "logcat":
                doc["candidate_identity"] = {"path": str(identity), "pin": pin}
            if args[:2] == ["shell", "setprop"]:
                self.document(
                    prefix + f"{index:03d}-intent.json",
                    {
                        **doc,
                        "stdout": "",
                        "returncode": None,
                        "original_value": original,
                    },
                )
            self.document(prefix + f"{index:03d}-command.json", doc)
        self.document(
            prefix + "restored.json",
            {"run_id": RUN, "value": original, "verified": True},
        )

    def test_live_logcat_scope_and_arrival_bounds_survive_consistent_resealing(self):
        result = self.success()
        self.close_diagnostics()
        prefix = "platform/close-diagnostics/"
        identity = self.attempt / "candidate/bound-identity.json"
        raw_identity = identity.read_bytes()
        (self.attempt / (prefix + "live-bound-identity.json")).write_bytes(raw_identity)
        raw = b"tag-filtered diagnostic bytes\n"
        raw_path = self.attempt / (prefix + "live-logcat.raw")
        raw_path.write_bytes(raw)
        intent = {
            "run_id": RUN,
            "argv": [
                "/mock/adb",
                "-s",
                result["device"],
                "logcat",
                "-b",
                "main,system,crash,events",
                "-v",
                "threadtime",
                "-T",
                "1",
                *evidence.LIVE_LOGCAT_TAGS,
            ],
            "candidate_identity": {
                "path": str(identity),
                "pin": [1, 2, hashlib.sha256(raw_identity).hexdigest()],
            },
            "started_monotonic": 10.0,
            "deadline_monotonic": 70.0,
            "maximum_raw_bytes": 8 * 1024 * 1024,
        }
        stop = {
            **intent,
            "raw": {
                "path": str(raw_path),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            },
            "arrivals": [{"offset": len(raw), "monotonic": 11.0}],
        }
        self.document(prefix + "live-intent.json", intent)
        self.document(prefix + "live-stop.json", stop)
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        name = prefix + "live-stop.json"
        for changes in (
            {"argv": ["/mock/adb", "-s", result["device"], "logcat"]},
            {"deadline_monotonic": 311.0},
            {"maximum_raw_bytes": 9 * 1024 * 1024},
            {"raw": {**stop["raw"], "sha256": "0" * 64}},
            {"arrivals": [{"offset": len(raw) + 1, "monotonic": 11.0}]},
            {"arrivals": [{"offset": 1, "monotonic": 9.0}]},
        ):
            with self.subTest(changes=changes):
                self.document(name, stop | changes)
                self.reseal_file(name)
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)
        self.document(name, stop)
        self.reseal_file(name)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)

    def before_close(self) -> dict:
        """Uninterpreted raw samples and the capture owner's metadata fields."""
        prefix = "platform/close-diagnostics/"
        identity = self.attempt / "candidate/bound-identity.json"
        app = json.loads(identity.read_bytes())
        rawfiles = {}
        for kind, raw in (
            ("xml", b"not parsed as UI evidence"),
            ("png", b"\x89PNG\r\n\x1a\ndiagnostic only"),
        ):
            path = self.attempt / prefix / f"before-close.{kind}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            rawfiles[kind] = {
                "path": str(path),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        rect = {"x": -20, "y": 2000.5, "width": 100, "height": 40}
        return {
            "run_id": RUN,
            "candidate_identity_sha256": hashlib.sha256(
                identity.read_bytes()
            ).hexdigest(),
            "serial": app["serial"],
            "pid": app["pid"],
            "elementId": "already-located-close-element",
            "before_rect": rect,
            "after_rect": rect.copy(),
            "window_rect": {"x": -5, "y": -10, "width": 480, "height": 800},
            "rawfiles": rawfiles,
        }

    def mutate(self, name: str, **changes) -> None:
        path = self.attempt / name
        doc = json.loads(path.read_bytes())
        doc.update(changes)
        self.document(name, doc)

    def test_freeze_real_tiny_zip_then_replay_retained_app_without_ready_or_rf_claim(
        self,
    ) -> None:
        result = self.success()
        path = evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(path, self.attempt / "result.json")
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        manifest = json.loads((self.attempt / "manifest.json").read_bytes())
        self.assertNotIn("manifest.json", [row["path"] for row in manifest["files"]])
        self.assertEqual(manifest["apk_sha256"], self.sha)
        self.assertEqual(
            (self.attempt / "candidate.apk").read_bytes(), self.source.read_bytes()
        )
        self.assertEqual(result["host_gate_status"], "pending")
        self.assertEqual(result["rf"], "not_run")

    def test_cli_reports_only_offline_integrity_without_mutating_bundle(self) -> None:
        result = self.success()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        before = {
            path.relative_to(self.attempt): path.read_bytes()
            for path in self.attempt.rglob("*")
            if path.is_file()
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            mock.patch.object(sys, "argv", ["lifecycle-evidence", str(self.attempt)]),
            mock.patch.object(
                evidence,
                "verify_lifecycle_bundle",
                wraps=evidence.verify_lifecycle_bundle,
            ) as verify,
            mock.patch(
                "subprocess.run", side_effect=AssertionError("offline: no commands")
            ),
        ):
            self.assertEqual(evidence.main(), 0)
        verify.assert_called_once_with(self.attempt)
        self.assertEqual(stderr.getvalue(), "")
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["verification"], "offline_integrity")
        self.assertEqual(summary["integrity_status"], "passed")
        self.assertIs(summary["host_gate_upgraded"], False)
        for field in (
            "scope",
            "run_id",
            "device",
            "app_id",
            "apk_sha256",
            "status",
            "scenario_status",
            "session_status",
            "phone_status",
            "host_gate_status",
            "rf",
        ):
            self.assertEqual(summary[field], result[field])
        self.assertEqual(summary["host_gate_status"], "pending")
        self.assertEqual(summary["rf"], "not_run")
        self.assertNotIn("startup_snapshot", summary)
        self.assertEqual(
            before,
            {
                path.relative_to(self.attempt): path.read_bytes()
                for path in self.attempt.rglob("*")
                if path.is_file()
            },
        )

    def test_cli_rejects_missing_malformed_and_tampered_evidence(self) -> None:
        result = self.success()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        for kind in ("missing", "malformed", "tampered"):
            with self.subTest(kind=kind):
                attempt = self.root / kind
                shutil.copytree(self.attempt, attempt)
                if kind == "missing":
                    (attempt / "manifest.json").unlink()
                elif kind == "malformed":
                    (attempt / "manifest.json").write_text("{", encoding="utf-8")
                else:
                    (attempt / "candidate.apk").write_bytes(b"tampered")
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(evidence.main([str(attempt)]), 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn(
                    "offline integrity verification failed:", stderr.getvalue()
                )
                self.assertNotIn("Traceback", stderr.getvalue())
                if kind == "missing":
                    self.assertFalse((attempt / "manifest.json").exists())

    def test_cli_verifier_errors_return_two_but_keyboard_interrupt_propagates(
        self,
    ) -> None:
        for error in (
            TimeoutError("bounded replay expired"),
            PermissionError("read refused"),
            RuntimeError("unexpected replay failure"),
            KeyboardInterrupt(),
        ):
            with self.subTest(error=type(error).__name__):
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                    mock.patch.object(
                        evidence, "verify_lifecycle_bundle", side_effect=error
                    ) as verify,
                ):
                    if isinstance(error, KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            evidence.main([str(self.attempt)])
                        self.assertEqual(stderr.getvalue(), "")
                    else:
                        self.assertEqual(evidence.main([str(self.attempt)]), 2)
                        self.assertIn(str(error), stderr.getvalue())
                verify.assert_called_once_with(self.attempt)
                self.assertEqual(stdout.getvalue(), "")

    def test_module_cli_entrypoint_and_argument_errors(self) -> None:
        result = self.success()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        command = [sys.executable, "-m", "tools.react_native.bleng_lifecycle_evidence"]
        for arguments, expected in (
            ([str(self.attempt)], 0),
            ([], 2),
            ([str(self.attempt), "extra"], 2),
        ):
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    [*command, *arguments],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=Path(__file__).resolve().parents[3],
                    check=False,
                )
                self.assertEqual(completed.returncode, expected, completed.stderr)
                if expected == 0:
                    summary = json.loads(completed.stdout)
                    self.assertEqual(summary["verification"], "offline_integrity")
                    self.assertEqual(summary["rf"], "not_run")
                    self.assertEqual(summary["host_gate_status"], "pending")
                    self.assertEqual(completed.stderr, "")
                else:
                    self.assertEqual(completed.stdout, "")
                    self.assertIn("usage:", completed.stderr)

    def test_partial_failure_preserves_errors_without_inventing_receipts_or_phone_quarantine(
        self,
    ) -> None:
        result = self.success()
        for directory in ("candidate", "helpers", "platform", "automation"):
            shutil.rmtree(self.attempt / directory)
        result.update(
            status="failed",
            scenario_status="not_run",
            session_status="not_run",
            host_gate_status="failed",
            phone_status="not_acquired",
            primary_error="prepare failed",
            cleanup_errors=["independent cleanup failed"],
            phone_cleanup_errors=["not acquired"],
            observations=[],
            startup_snapshot={},
            cleanup_order=[],
        )
        (self.attempt / "phone.json").unlink()
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        self.assertFalse((self.attempt / "candidate/finalization.json").exists())
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(evidence.main([str(self.attempt)]), 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["integrity_status"], "passed")
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["host_gate_status"], "failed")
        self.assertEqual(summary["rf"], "not_run")
        self.assertIs(summary["host_gate_upgraded"], False)
        self.assertEqual(stderr.getvalue(), "")

    def test_close_diagnostics_success_and_ordinary_bundle_compatibility(self) -> None:
        result = self.success()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        for original in ("", "INFO", "DEBUG", "S"):
            with self.subTest(original=original):
                (self.attempt / "manifest.json").unlink()
                (self.attempt / "result.json").unlink()
                self.close_diagnostics(original)
                if original == "INFO":
                    stdout = "Close cleanup failed (diagnostic text, not a verdict)\n"
                    self.mutate(
                        "platform/close-diagnostics/004-command.json",
                        stdout=stdout,
                        stdout_bytes=len(stdout.encode()),
                    )
                evidence.publish_lifecycle_bundle(self.attempt, result)
                self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)

    def test_before_close_optional_complete_offscreen_and_relocated_raw_paths(
        self,
    ) -> None:
        result = self.success()
        self.close_diagnostics()
        doc = self.before_close()
        for kind, ref in doc["rawfiles"].items():
            ref["path"] = (
                f"/historical/run/platform/close-diagnostics/before-close.{kind}"
            )
        self.document("platform/close-diagnostics/before-close.json", doc)
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        self.assertEqual(result["observations"], ["idle", "started", "closed"])
        self.assertEqual(result["rf"], "not_run")

    def test_before_close_rejects_resealed_metadata_and_raw_reference_conflicts(
        self,
    ) -> None:
        result = self.success()
        self.close_diagnostics()
        doc = self.before_close()
        name = "platform/close-diagnostics/before-close.json"
        self.document(name, doc)
        evidence.publish_lifecycle_bundle(self.attempt, result)
        cases = [{field: None} for field in doc]
        cases += [
            {"candidate_identity_sha256": "0" * 64},
            {"serial": "foreign"},
            {"pid": 999},
            {"pid": True},
            {"elementId": " \t"},
            {"elementId": 42},
            {"after_rect": {**doc["after_rect"], "x": -21}},
            {"rawfiles": {}},
            {"rawfiles": {"xml": doc["rawfiles"]["xml"]}},
        ]
        for field in ("before_rect", "after_rect", "window_rect"):
            for key, value in (
                ("width", 0),
                ("height", -1),
                ("x", True),
                ("y", "0"),
                ("x", float("inf")),
                ("height", float("nan")),
            ):
                cases.append({field: {**doc[field], key: value}})
        for kind, ref in doc["rawfiles"].items():
            for changes in (
                {"path": f"platform/close-diagnostics/before-close.{kind}"},
                {"path": f"/old/wrong/close-diagnostics/before-close.{kind}"},
                {"path": f"/old/../platform/close-diagnostics/before-close.{kind}"},
                {"path": f"/old/platform/close-diagnostics/other.{kind}"},
                {"sha256": "0" * 64},
                {"size": ref["size"] + 1},
                {"size": True},
            ):
                cases.append(
                    {"rawfiles": {**doc["rawfiles"], kind: {**ref, **changes}}}
                )
        for changes in cases:
            with self.subTest(changes=changes):
                self.document(name, {**doc, **changes})
                self.reseal_file(name)
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)
        self.document(name, doc)
        self.reseal_file(name)
        rawname = "platform/close-diagnostics/before-close.xml"
        (self.attempt / rawname).unlink()
        manifest = json.loads((self.attempt / "manifest.json").read_bytes())
        manifest["files"] = [row for row in manifest["files"] if row["path"] != rawname]
        self.document("manifest.json", manifest)
        with self.assertRaises(ValueError):
            evidence.verify_lifecycle_bundle(self.attempt)

    def test_before_close_raw_limits_and_signature_survive_consistent_resealing(
        self,
    ) -> None:
        result = self.success()
        self.close_diagnostics()
        doc = self.before_close()
        name = "platform/close-diagnostics/before-close.json"
        self.document(name, doc)
        evidence.publish_lifecycle_bundle(self.attempt, result)
        for kind, limit in (("xml", 1024 * 1024), ("png", 8 * 1024 * 1024)):
            rawname = f"platform/close-diagnostics/before-close.{kind}"
            path = self.attempt / rawname
            original = path.read_bytes()
            for size, valid_signature, accepted in (
                (limit, True, True),
                (limit + 1, True, False),
                (16, False, kind == "xml"),
            ):
                with self.subTest(
                    kind=kind, size=size, valid_signature=valid_signature
                ):
                    raw = (
                        b"\x89PNG\r\n\x1a\n"
                        if valid_signature and kind == "png"
                        else b"not PNG!"
                    ) + b"x" * (size - 8)
                    path.write_bytes(raw)
                    doc["rawfiles"][kind].update(
                        size=len(raw), sha256=hashlib.sha256(raw).hexdigest()
                    )
                    self.document(name, doc)
                    self.reseal_file(name)
                    self.reseal_file(rawname)
                    if accepted:
                        self.assertEqual(
                            evidence.verify_lifecycle_bundle(self.attempt), result
                        )
                    else:
                        with self.assertRaises(ValueError):
                            evidence.verify_lifecycle_bundle(self.attempt)
            path.write_bytes(original)
            doc["rawfiles"][kind].update(
                size=len(original), sha256=hashlib.sha256(original).hexdigest()
            )
            self.document(name, doc)
            self.reseal_file(name)
            self.reseal_file(rawname)

    def test_before_close_failed_partial_keeps_known_identity_and_raw_refs_honest(
        self,
    ) -> None:
        result = self.success()
        doc = self.before_close()
        name = "platform/close-diagnostics/before-close-failed.json"
        result.update(
            status="failed",
            host_gate_status="failed",
            primary_error="capture interrupted",
        )
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        partial = {
            "run_id": RUN,
            "elementId": None,
            "rawfiles": {},
            "error": {"message": "capture interrupted"},
        }
        self.document(name, partial)
        evidence.publish_lifecycle_bundle(self.attempt, result)
        stages = [
            partial,
            {**partial, "candidate_identity_sha256": doc["candidate_identity_sha256"]},
            {
                **partial,
                "serial": doc["serial"],
                "pid": doc["pid"],
                "rawfiles": {"xml": doc["rawfiles"]["xml"]},
            },
            {
                **doc,
                "after_rect": {**doc["after_rect"], "y": -99},
                "error": partial["error"],
            },
        ]
        for stage in stages:
            with self.subTest(stage=stage):
                self.document(name, stage)
                self.reseal_file(name)
                self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        for changes in (
            {"candidate_identity_sha256": "0" * 64},
            {"pid": 999},
            {"serial": "foreign"},
            {"rawfiles": {"xml": {**doc["rawfiles"]["xml"], "sha256": "0" * 64}}},
            {"rawfiles": {"png": {**doc["rawfiles"]["png"], "size": 1}}},
        ):
            with self.subTest(changes=changes):
                self.document(name, {**partial, **changes})
                self.reseal_file(name)
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)

    def test_close_diagnostics_rejects_resealed_semantic_tampering(self) -> None:
        result = self.success()
        self.close_diagnostics()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        prefix = "platform/close-diagnostics/"
        originals = {p.name: p.read_bytes() for p in (self.attempt / prefix).iterdir()}
        manifest_path = self.attempt / "manifest.json"
        manifest_raw = manifest_path.read_bytes()
        capture = json.loads(originals["004-command.json"])
        selector = capture["candidate_identity"]
        cases = [
            ("restored.json", None),
            ("restored.json", {"verified": False}),
            ("restored.json", {"verified": 1}),
            ("restored.json", {"verified": None}),
            ("restored.json", {"value": "INFO"}),
            ("restored.json", {"value": "unknown"}),
            ("002-intent.json", None),
            ("001-command.json", {"argv": None}),
            ("003-command.json", {"returncode": None}),
            ("006-command.json", {"returncode": True}),
            ("002-intent.json", {"original_value": "INFO"}),
            ("006-intent.json", {"original_value": "INFO"}),
            ("001-command.json", {"stdout": "INFO\n", "stdout_bytes": 5}),
            ("003-command.json", {"stdout": "INFO\n", "stdout_bytes": 5}),
            ("005-command.json", {"stdout": "WARN\n", "stdout_bytes": 5}),
            ("007-command.json", {"stdout": "INFO\n", "stdout_bytes": 5}),
            ("004-command.json", None),
            ("004-command.json", {"candidate_identity": None}),
            (
                "004-command.json",
                {"candidate_identity": {**selector, "pin": [1, 2, "0" * 64]}},
            ),
            (
                "004-command.json",
                {"candidate_identity": {**selector, "path": "/other/identity.json"}},
            ),
            ("004-command.json", {"returncode": 1}),
            ("004-command.json", {"error": {"message": "capture failed"}}),
            ("004-command.json", {"stdout_bytes": 100}),
        ]
        for index, replacement in ((11, "Other:D"), (8, "999"), (2, "other-phone")):
            argv = capture["argv"].copy()
            argv[index] = replacement
            cases.append(("004-command.json", {"argv": argv}))
        for name in (
            "001-command.json",
            "002-command.json",
            "003-command.json",
            "005-command.json",
            "006-command.json",
            "007-command.json",
            "002-intent.json",
            "006-intent.json",
        ):
            doc = json.loads(originals[name])
            argv = doc["argv"].copy()
            argv[5] = "log.tag.Other"
            cases.append((name, {"argv": argv}))
        for name, changes in cases:
            with self.subTest(name=name, changes=changes):
                for filename, raw in originals.items():
                    (self.attempt / prefix / filename).write_bytes(raw)
                manifest_path.write_bytes(manifest_raw)
                if changes is None:
                    (self.attempt / prefix / name).unlink()
                    manifest = json.loads(manifest_raw)
                    manifest["files"] = [
                        row for row in manifest["files"] if row["path"] != prefix + name
                    ]
                    manifest_path.write_text(json.dumps(manifest))
                else:
                    self.mutate(prefix + name, **changes)
                    self.reseal_file(prefix + name)
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)

    def test_failed_close_diagnostics_preserves_unknown_without_restore_or_capture(
        self,
    ) -> None:
        result = self.success()
        self.close_diagnostics()
        prefix = "platform/close-diagnostics/"
        for path in (self.attempt / prefix).iterdir():
            if path.name != "001-command.json":
                path.unlink()
        self.mutate(prefix + "001-command.json", stdout="unknown\n", stdout_bytes=8)
        result.update(
            status="failed",
            scenario_status="failed",
            host_gate_status="failed",
            primary_error="unknown log level",
        )
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)

    def test_failed_close_diagnostics_still_rejects_existing_identity_conflicts(
        self,
    ) -> None:
        result = self.success()
        self.close_diagnostics()
        prefix = "platform/close-diagnostics/"
        (self.attempt / prefix / "restored.json").unlink()
        result.update(
            status="failed", host_gate_status="failed", primary_error="Close unknown"
        )
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        evidence.publish_lifecycle_bundle(self.attempt, result)
        name = prefix + "004-command.json"
        original = (self.attempt / name).read_bytes()
        capture = json.loads(original)
        for changes in (
            {
                "candidate_identity": {
                    **capture["candidate_identity"],
                    "pin": [1, 2, "0" * 64],
                }
            },
            {"argv": capture["argv"][:8] + ["999"] + capture["argv"][9:]},
        ):
            with self.subTest(changes=changes):
                (self.attempt / name).write_bytes(original)
                self.mutate(name, **changes)
                self.reseal_file(name)
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)

    def server_logs(self) -> str:
        bound = "platform/bound-session.json"
        self.mutate(
            bound,
            server_identity={
                "serial": "filesystem-fixture-only",
                "package": "io.appium.uiautomator2.server",
                "pid": 3210,
            },
        )
        name = "platform/099-read.json"
        self.document(
            name,
            {
                "argv": [
                    "/synthetic/adb",
                    "-s",
                    "filesystem-fixture-only",
                    "logcat",
                    "-d",
                    "-v",
                    "threadtime",
                    "--pid",
                    "3210",
                    "-t",
                    "1000",
                    "appium:I",
                    "*:S",
                ],
                "stdout": "CaptureScreenshot command; not a success verdict\n",
                "returncode": 0,
                "error": None,
            },
        )
        return name

    def test_server_logs_optional_capture_preserves_failed_read_without_verdict(
        self,
    ) -> None:
        result = self.success()
        name = self.server_logs()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        result.update(
            status="failed",
            scenario_status="failed",
            host_gate_status="failed",
            primary_error="screenshot timed out",
        )
        (self.attempt / "result.json").unlink()
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        self.mutate(name, returncode=None, error="read timed out", stdout="")
        (self.attempt / "manifest.json").unlink()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)

    def test_server_logs_resealed_selector_conflicts_rejected_even_on_failure(
        self,
    ) -> None:
        result = self.success()
        name = self.server_logs()
        result.update(
            status="failed", host_gate_status="failed", primary_error="unknown"
        )
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        evidence.publish_lifecycle_bundle(self.attempt, result)
        capture = json.loads((self.attempt / name).read_bytes())
        boundname = "platform/bound-session.json"
        bound = json.loads((self.attempt / boundname).read_bytes())
        mutations = [
            (
                name,
                {
                    "argv": [
                        *capture["argv"][:index],
                        value,
                        *capture["argv"][index + 1 :],
                    ]
                },
            )
            for index, value in (
                (2, "foreign-serial"),
                (8, "999"),
                (10, "1001"),
                (11, "appium:D"),
                (12, "*:V"),
                (6, "raw"),
            )
        ] + [
            (name, {"argv": capture["argv"] + ["Other:I"]}),
            (boundname, {"server_identity": {**bound["server_identity"], "pid": 999}}),
            (boundname, {"server_identity": {**bound["server_identity"], "pid": True}}),
            (
                boundname,
                {"server_identity": {**bound["server_identity"], "serial": "foreign"}},
            ),
            (
                boundname,
                {"server_identity": {**bound["server_identity"], "package": "foreign"}},
            ),
            (boundname, {"server_identity": None}),
        ]
        for target, changes in mutations:
            with self.subTest(target=target, changes=changes):
                self.document(name, capture)
                self.document(boundname, bound)
                self.mutate(target, **changes)
                self.reseal_file(name)
                self.reseal_file(boundname)
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)
        self.document(name, capture)
        (self.attempt / boundname).unlink()
        (self.attempt / "manifest.json").unlink()
        with self.assertRaises(ValueError):
            evidence.publish_lifecycle_bundle(self.attempt, result)

    def server_status(self) -> str:
        identity = json.loads(
            (self.attempt / "candidate/bound-identity.json").read_bytes()
        )
        identity.update(
            package="io.appium.uiautomator2.server",
            cmdline="io.appium.uiautomator2.server",
            pid=3210,
        )
        self.mutate("platform/bound-session.json", server_identity=identity)
        self.document(
            "platform/prepared-owner.json",
            {
                "run_id": RUN,
                "app_id": "dev.blehub.formal",
                "system_port": 18200,
                "status": "prepared",
            },
        )
        name = "platform/server-status.json"
        self.document(
            name,
            {
                "run_id": RUN,
                "host_session_id": HOST,
                "device_session_id": "downstream-1",
                "server_identity": identity,
                "system_port": 18200,
                "endpoint": "http://127.0.0.1:18200/status",
                "started_monotonic": 100.0,
                "finished_monotonic": 100.1,
                "response": {"value": {"ready": False}},
                "error": None,
            },
        )
        return name

    def test_server_status_is_optional_and_preserves_ready_false_or_timeout_without_verdict(
        self,
    ) -> None:
        result = self.success()
        name = self.server_status()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        result.update(
            status="failed",
            scenario_status="failed",
            host_gate_status="failed",
            primary_error="second rect timed out",
        )
        for target in ("result.json", "manifest.json"):
            (self.attempt / target).unlink()
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        self.mutate(
            name,
            response=None,
            finished_monotonic=102.0,
            error={
                "type": "TimeoutError",
                "message": "read timed out",
                "notes": [],
            },
        )
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)

    def test_server_status_resealed_selectors_still_fail_on_failed_bundle(self) -> None:
        result = self.success()
        name = self.server_status()
        result.update(
            status="failed",
            host_gate_status="failed",
            primary_error="second rect timed out",
        )
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        evidence.publish_lifecycle_bundle(self.attempt, result)
        paths = (name, "platform/bound-session.json", "platform/prepared-owner.json")
        original = {
            target: json.loads((self.attempt / target).read_bytes()) for target in paths
        }
        server = original[name]["server_identity"]
        mutations = [
            (name, {"run_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}),
            (name, {"host_session_id": "foreign"}),
            (name, {"device_session_id": "foreign"}),
            (name, {"system_port": 18201}),
            (name, {"system_port": 18200.0}),
            (name, {"system_port": True}),
            (name, {"endpoint": "http://127.0.0.1:4737/status"}),
            (name, {"endpoint": "http://localhost:18200/status"}),
            (name, {"endpoint": "http://127.0.0.1:18200/status?redirect=1"}),
            (name, {"server_identity": None}),
            ("platform/bound-session.json", {"device_session_id": "foreign"}),
            ("platform/prepared-owner.json", {"system_port": 18201}),
            ("platform/prepared-owner.json", {"system_port": 18200.0}),
        ] + [
            (target, {"server_identity": {**server, key: value}})
            for target in (name, "platform/bound-session.json")
            for key, value in (
                ("serial", "foreign"),
                ("package", "foreign"),
                ("pid", 9999),
                ("pid", 3210.0),
                ("uid", True),
                ("start_ticks", 999),
                ("boot_id", "foreign"),
                ("base_apk_path", "/foreign/base.apk"),
                ("apk_sha256", "f" * 64),
            )
        ]
        for target, changes in mutations:
            with self.subTest(target=target, changes=changes):
                for path, value in original.items():
                    self.document(path, value)
                    self.reseal_file(path)
                self.mutate(target, **changes)
                self.reseal_file(target)
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)

    def test_server_status_requires_original_bound_and_prepared_selectors_even_on_failure(
        self,
    ) -> None:
        result = self.success()
        self.server_status()
        result.update(
            status="failed", host_gate_status="failed", primary_error="unknown"
        )
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        for name in ("platform/bound-session.json", "platform/prepared-owner.json"):
            with self.subTest(missing=name):
                path = self.attempt / name
                raw = path.read_bytes()
                path.unlink()
                with self.assertRaises(ValueError):
                    evidence.publish_lifecycle_bundle(self.attempt, result)
                path.write_bytes(raw)

    def adb_transport(self) -> str:
        """Reuse original server selectors without requiring the HTTP observation."""
        status = self.server_status()
        identity = json.loads((self.attempt / status).read_bytes())["server_identity"]
        (self.attempt / status).unlink()
        boot = identity["boot_id"]
        values = ["0029", "device", "OKAY", "OKAY", boot]
        outputs = ["OKAY00040029", "OKAY0006device", "OKAY", "OKAY", boot + "\n"]
        names = [
            "server_version",
            "cached_state",
            "transport_select",
            "service_accept",
            "boot_id",
        ]
        name = "platform/adb-transport.json"
        self.document(
            name,
            {
                "run_id": RUN,
                "device": identity["serial"],
                "host_session_id": HOST,
                "device_session_id": "downstream-1",
                "server_identity": identity,
                "system_port": 18200,
                "endpoint": "127.0.0.1:5037",
                "started_monotonic": 100.0,
                # Publication follows the postguard; it is not the socket I/O window.
                "finished_monotonic": 104.0,
                "budget_seconds": 3.0,
                "expected_boot_id": boot,
                "boot_id_matches_expected": True,
                "error": None,
                "limitations": [
                    "Expected comparison only; not ownership, closure or RF proof."
                ],
                "stages": [
                    {
                        "name": stage,
                        "started_monotonic": 100.0 + index / 10,
                        "finished_monotonic": 100.0 + (index + 1) / 10,
                        "output": output,
                        "value": value,
                        "error": None,
                    }
                    for index, (stage, value, output) in enumerate(
                        zip(names, values, outputs, strict=True)
                    )
                ],
            },
        )
        return name

    def test_adb_transport_optional_success_and_failed_observations_never_change_verdict(
        self,
    ) -> None:
        result = self.success()
        name = self.adb_transport()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        result.update(
            status="failed",
            host_gate_status="failed",
            primary_error="original UI failure",
        )
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        for target in ("result.json", "manifest.json"):
            (self.attempt / target).unlink()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        original = json.loads((self.attempt / name).read_bytes())
        error = {"type": "TimeoutError", "message": "ADB read failed"}
        for count in range(6):
            with self.subTest(prefix=count):
                doc = json.loads(json.dumps(original))
                doc.update(
                    stages=doc["stages"][:count],
                    error=error,
                    boot_id_matches_expected=None,
                )
                if count:
                    doc["stages"][-1].update(output="", value=None, error=error)
                self.document(name, doc)
                self.reseal_file(name)
                self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        doc = json.loads(json.dumps(original))
        wrong = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        doc.update(error=error, boot_id_matches_expected=False)
        doc["stages"][-1].update(value=wrong, output=wrong + "\n", error=error)
        self.document(name, doc)
        self.reseal_file(name)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        self.mutate(name, boot_id_matches_expected=True)
        self.reseal_file(name)
        with self.assertRaises(ValueError):
            evidence.verify_lifecycle_bundle(self.attempt)

    def test_adb_transport_resealed_selectors_are_checked_even_on_failed_bundles(
        self,
    ) -> None:
        result = self.success()
        name = self.adb_transport()
        result.update(
            status="failed",
            host_gate_status="failed",
            primary_error="original UI failure",
        )
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        evidence.publish_lifecycle_bundle(self.attempt, result)
        paths = (name, "platform/bound-session.json", "platform/prepared-owner.json")
        original = {
            path: json.loads((self.attempt / path).read_bytes()) for path in paths
        }
        server = original[name]["server_identity"]
        mutations = [
            (name, {"run_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}),
            (name, {"device": "foreign"}),
            (name, {"host_session_id": "foreign"}),
            (name, {"device_session_id": "foreign"}),
            (name, {"system_port": 18201}),
            (name, {"system_port": 18200.0}),
            (name, {"system_port": True}),
        ]
        mutations.extend(
            (name, {"endpoint": endpoint})
            for endpoint in (
                "localhost:5037",
                "127.0.0.1:5038",
                "tcp:127.0.0.1:5037",
                "127.0.0.1:5037?x=1",
            )
        )
        mutations.extend(
            [
                (name, {"expected_boot_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}),
                (name, {"server_identity": None}),
                ("platform/bound-session.json", {"device_session_id": "foreign"}),
                ("platform/bound-session.json", {"host_session_id": None}),
                ("platform/prepared-owner.json", {"system_port": 18201}),
                ("platform/prepared-owner.json", {"system_port": 18200.0}),
                ("platform/prepared-owner.json", {"app_id": "foreign"}),
                ("platform/prepared-owner.json", {"status": "failed"}),
            ]
        )
        mutations.extend(
            (target, {"server_identity": {**server, key: value}})
            for target in (name, "platform/bound-session.json")
            for key, value in (
                ("serial", "foreign"),
                ("pid", 9999),
                ("uid", True),
                ("start_ticks", 456.0),
                ("boot_id", "foreign"),
                ("apk_sha256", "f" * 64),
            )
        )
        for target, changes in mutations:
            with self.subTest(target=target, changes=changes):
                for path, doc in original.items():
                    self.document(path, doc)
                    self.reseal_file(path)
                self.mutate(target, **changes)
                self.reseal_file(target)
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)
        for path, doc in original.items():
            self.document(path, doc)
        for sealed in ("result.json", "manifest.json"):
            (self.attempt / sealed).unlink()
        for target in paths[1:]:
            raw = (self.attempt / target).read_bytes()
            (self.attempt / target).unlink()
            with self.assertRaises(ValueError):
                evidence.publish_lifecycle_bundle(self.attempt, result)
            (self.attempt / target).write_bytes(raw)

    def test_adb_transport_resealed_timing_sequence_output_and_value_boundaries(
        self,
    ) -> None:
        result = self.success()
        name = self.adb_transport()
        result.update(
            status="failed",
            host_gate_status="failed",
            primary_error="original UI failure",
        )
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        evidence.publish_lifecycle_bundle(self.attempt, result)
        original = json.loads((self.attempt / name).read_bytes())
        mutations = (
            [
                {"budget_seconds": value}
                for value in (
                    True,
                    0,
                    -1,
                    3.001,
                    "3",
                    float("inf"),
                    float("nan"),
                    10**400,
                )
            ]
            + [
                {"started_monotonic": value}
                for value in (True, -1, "100", float("inf"), 10**400)
            ]
            + [
                {"finished_monotonic": value}
                for value in (True, "104", float("inf"), float("nan"), 10**400)
            ]
            + [
                {"finished_monotonic": 99},
                {"boot_id_matches_expected": 1},
                {"boot_id_matches_expected": False},
                {"boot_id_matches_expected": None},
                {"stages": []},
                {"stages": original["stages"][:-1]},
                {"stages": list(reversed(original["stages"]))},
                {"stages": original["stages"] + original["stages"][-1:]},
            ]
        )
        for index, changes in [
            (0, {"name": "cached_state"}),
            (0, {"value": "nope"}),
            (0, {"output": "OKAY0004ffff"}),
            (0, {"output": "FAIL0004oops"}),
            (1, {"value": "offline"}),
            (1, {"output": "OKAY0007offline"}),
            (2, {"value": "FAIL"}),
            (3, {"output": "FAIL0004deny"}),
            (4, {"value": "foreign"}),
            (4, {"output": RUN}),
            (0, {"started_monotonic": True}),
            (0, {"started_monotonic": float("inf")}),
            (0, {"finished_monotonic": float("nan")}),
            (0, {"finished_monotonic": 10**400}),
            (0, {"finished_monotonic": 99}),
            (1, {"started_monotonic": 100}),
            (4, {"finished_monotonic": 103.001}),
            (0, {"error": {"type": "TimeoutError", "message": "stopped"}}),
            (0, {"error": "bad"}),
        ]:
            stages = json.loads(json.dumps(original["stages"]))
            stages[index].update(changes)
            mutations.append({"stages": stages})
        for index, maximum in ((0, 264), (1, 264), (2, 264), (3, 264), (4, 516)):
            stages = json.loads(json.dumps(original["stages"][: index + 1]))
            stages[-1].update(
                output="x" * (maximum + 1),
                value=None,
                error={"type": "RuntimeError", "message": "bad frame"},
            )
            mutations.append(
                {
                    "stages": stages,
                    "error": {"type": "RuntimeError", "message": "bad frame"},
                    "boot_id_matches_expected": None,
                }
            )
        for changes in mutations:
            with self.subTest(changes=changes):
                self.document(name, original | changes)
                self.reseal_file(name)
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)

    def test_adb_transport_late_timeout_keeps_failure_without_extending_io_budget(
        self,
    ) -> None:
        result = self.success()
        name = self.adb_transport()
        result.update(
            status="failed",
            host_gate_status="failed",
            primary_error="original UI failure",
        )
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        doc = json.loads((self.attempt / name).read_bytes())
        timeout = {"type": "TimeoutError", "message": "timed out", "notes": []}
        # A socket timeout requested within the original 3s budget can return late.
        doc.update(
            stages=doc["stages"][:2], error=timeout, boot_id_matches_expected=None
        )
        doc["stages"][-1].update(
            finished_monotonic=103.003, output="OK", value=None, error=timeout
        )
        self.document(name, doc)
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        for began in (103.0, 103.001):
            with self.subTest(no_io_after_deadline=began):
                stopped = json.loads(json.dumps(doc))
                stopped["stages"][-1].update(started_monotonic=began, output="")
                self.document(name, stopped)
                self.reseal_file(name)
                self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        mutations = [
            {"error": None, "value": "device", "output": "OKAY0006device"},
            {"started_monotonic": 103.001},
            {"started_monotonic": 103.0},
            {"started_monotonic": 103.001, "output": "", "value": "device"},
            {"finished_monotonic": float("inf")},
            {"finished_monotonic": 100.0},
            {"finished_monotonic": 104.001},
        ]
        for changes in mutations:
            with self.subTest(stage_changes=changes):
                altered = json.loads(json.dumps(doc))
                altered["stages"][-1].update(changes)
                self.document(name, altered)
                self.reseal_file(name)
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)
        for changes in (
            {"error": None},
            {
                "stages": doc["stages"]
                + [
                    {
                        "name": "transport_select",
                        "started_monotonic": 103.003,
                        "finished_monotonic": 103.004,
                        "output": "OKAY",
                        "value": "OKAY",
                        "error": None,
                    }
                ]
            },
        ):
            with self.subTest(receipt_changes=changes):
                self.document(name, doc | changes)
                self.reseal_file(name)
                with self.assertRaises(ValueError):
                    evidence.verify_lifecycle_bundle(self.attempt)

    def test_adb_transport_failure_cannot_be_laundered_into_a_pass(self) -> None:
        result = self.success()
        name = self.adb_transport()
        error = {"type": "TimeoutError", "message": "read failed", "notes": []}
        self.mutate(name, error=error)
        with self.assertRaises(ValueError):
            evidence.publish_lifecycle_bundle(self.attempt, result)

    def test_empty_close_diagnostics_directory_cannot_support_success(self) -> None:
        result = self.success()
        (self.attempt / "platform/close-diagnostics").mkdir()
        with self.assertRaises(ValueError):
            evidence.publish_lifecycle_bundle(self.attempt, result)

    def test_frozen_apk_is_exclusive_expected_hash_and_bounded(self) -> None:
        self.freeze()
        original = (self.attempt / "candidate.apk").read_bytes()
        with self.assertRaises(FileExistsError):
            self.freeze()
        self.assertEqual((self.attempt / "candidate.apk").read_bytes(), original)
        (self.attempt / "candidate.apk").unlink()
        with self.assertRaises(ValueError):
            self.freeze(expected_sha256="0" * 64)
        with self.assertRaises((ValueError, TimeoutError)):
            self.freeze(deadline=float("nan"))
        with self.assertRaises(TimeoutError):
            self.freeze(deadline=time.monotonic() - 1)
        with (
            mock.patch.object(evidence, "MAX_APK_BYTES", 10),
            self.assertRaises(ValueError),
        ):
            self.freeze()

    def test_apk_rejects_internal_line_missing_parts_and_unsafe_zip(self) -> None:
        cases = [
            {"omit": "assets/index.android.bundle"},
            {"omit": "classes.dex"},
            {"omit": "lib/arm64-v8a/libbleng_uniffi.so"},
            {"omit": "lib/arm64-v8a/libBleHubReactNativeBleng.so"},
            {"extra": [("lib/arm64-v8a/libblehub_uniffi.so", b"old line")]},
            {"extra": [("lib/arm64-v8a/libBleHubReactNative.so", b"old bridge")]},
            {"extra": [("../escape", b"bad")]},
            {"extra": [("classes.dex", b"duplicate")]},
            {"extra": [("assets/index.android.bundle", b"")]},
            {"extra": [("lib/x86_64/libbleng_uniffi.so", b"missing bridge")]},
        ]
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases.append({"extra": [(link, b"target")]})
        for case in cases:
            with self.subTest(case=case):
                self.apk(**case)
                self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
                with self.assertRaises(ValueError):
                    self.freeze()
                self.assertFalse((self.attempt / "candidate.apk").exists())

    def test_source_links_empty_special_and_parent_links_are_rejected(self) -> None:
        original = self.source.read_bytes()
        for kind in ("symlink", "hardlink", "empty", "fifo", "parent"):
            with self.subTest(kind=kind):
                self.source.unlink(missing_ok=True)
                target = self.root / "target.apk"
                target.write_bytes(original)
                if kind == "symlink":
                    self.source.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, self.source)
                elif kind == "fifo":
                    os.mkfifo(self.source)
                elif kind == "parent":
                    parent = self.root / "alias"
                    parent.symlink_to(self.root, target_is_directory=True)
                    with self.assertRaises(ValueError):
                        evidence.freeze_lifecycle_apk(
                            parent / "target.apk",
                            self.attempt / "candidate.apk",
                            expected_sha256=self.sha,
                            deadline=time.monotonic() + 5,
                        )
                    continue
                else:
                    self.source.touch()
                with self.assertRaises(ValueError):
                    self.freeze()

    def test_tree_tamper_extra_missing_and_empty_directory_are_not_sealed(self) -> None:
        result = self.success()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        path = self.attempt / "candidate/003-install.log"
        original = path.read_bytes()
        path.write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            evidence.verify_lifecycle_bundle(self.attempt)
        path.write_bytes(original)
        extra = self.attempt / "automation/extra.log"
        extra.touch()
        with self.assertRaises(ValueError):
            evidence.verify_lifecycle_bundle(self.attempt)
        extra.unlink()
        extra.mkdir()
        with self.assertRaises(ValueError):
            evidence.verify_lifecycle_bundle(self.attempt)
        extra.rmdir()
        path.unlink()
        with self.assertRaises(ValueError):
            evidence.verify_lifecycle_bundle(self.attempt)

    def test_publish_never_overwrites_existing_result_or_manifest(self) -> None:
        result = self.success()
        self.document("manifest.json", {"old": "keep"})
        with self.assertRaises((ValueError, FileExistsError)):
            evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertFalse((self.attempt / "result.json").exists())
        (self.attempt / "manifest.json").unlink()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        original = (self.attempt / "result.json").read_bytes()
        with self.assertRaises((ValueError, FileExistsError)):
            evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual((self.attempt / "result.json").read_bytes(), original)

    def test_known_owner_conflicts_and_false_success_are_rejected(self) -> None:
        result = self.success()
        cases = [
            ("candidate/finalization.json", {"run_id": "foreign"}),
            ("candidate/finalization.json", {"action": "force-stop"}),
            ("candidate/finalization.json", {"identity": None}),
            ("automation/close.json", {"session_id": "foreign"}),
            ("automation/close.json", {"session_closed": False}),
            ("automation/close.json", {"errors": ["DELETE failed"]}),
            ("platform/bound-session.json", {"host_session_id": "foreign"}),
            ("platform/finalized.json", {"session_deleted": False}),
            ("helpers/restored.json", {"preparation_error": "failed before rollback"}),
            ("phone.json", {"actions": [{"label": "initial"}]}),
            ("phone.json", {"status": "quarantined"}),
            ("candidate/bound-identity.json", {"pid": 999}),
        ]
        for name, changes in cases:
            with self.subTest(name=name, changes=changes):
                path = self.attempt / name
                original = path.read_bytes()
                self.mutate(name, **changes)
                with self.assertRaises(ValueError):
                    evidence.publish_lifecycle_bundle(self.attempt, result)
                self.assertFalse((self.attempt / "result.json").exists())
                path.write_bytes(original)

    def test_success_requires_every_known_receipt(self) -> None:
        result = self.success()
        for name in (
            "candidate/finalization.json",
            "candidate/bound-identity.json",
            "candidate/003-install.json",
            "automation/start.json",
            "automation/close.json",
            "platform/bound-session.json",
            "platform/finalized.json",
            "helpers/restored.json",
            "phone.json",
            "prepared-result.json",
            "candidate.apk",
        ):
            path = self.attempt / name
            raw = path.read_bytes()
            path.unlink()
            with (
                self.subTest(name=name),
                self.assertRaises((ValueError, FileNotFoundError)),
            ):
                evidence.publish_lifecycle_bundle(self.attempt, result)
            path.write_bytes(raw)

    def test_json_duplicates_nonfinite_and_result_shape_are_rejected_before_publication(
        self,
    ) -> None:
        result = self.success()
        path = self.attempt / "helpers/extra.json"
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', "[]"):
            path.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                evidence.publish_lifecycle_bundle(self.attempt, result)
        path.unlink()
        for changes in (
            {"host_gate_status": "passed"},
            {"rf": "passed"},
            {"schema_version": True},
            {"run_id": RUN.upper()},
            {"cleanup_errors": ["hidden failure"]},
            {"observations": ["started", "closed"]},
            {"startup_snapshot": {"state": "ready"}},
            {"phone_status": "quarantined"},
            {"primary_error": "cannot pass"},
            {"status": "other"},
            {
                "cleanup_order": [
                    "phone.finish",
                    "automation.close",
                    "candidate.finalize",
                ]
            },
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                evidence.publish_lifecycle_bundle(self.attempt, {**result, **changes})

    def test_links_special_paths_and_limits_apply_to_all_evidence(self) -> None:
        result = self.success()
        extra = self.attempt / "helpers/extra"
        for kind in ("symlink", "hardlink", "fifo", "unsafe"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    extra.symlink_to(self.source)
                elif kind == "hardlink":
                    os.link(self.source, extra)
                elif kind == "fifo":
                    os.mkfifo(extra)
                else:
                    extra = self.attempt / "helpers/bad:name"
                    extra.touch()
                with self.assertRaises(ValueError):
                    evidence.publish_lifecycle_bundle(self.attempt, result)
                extra.unlink()
        for constant, limit in (
            ("MAX_ENTRIES", 2),
            ("MAX_TOTAL_BYTES", 10),
            ("MAX_FILE_BYTES", 10),
            ("MAX_JSON_BYTES", 10),
        ):
            with (
                self.subTest(constant=constant),
                mock.patch.object(evidence, constant, limit),
                self.assertRaises(ValueError),
            ):
                evidence.publish_lifecycle_bundle(self.attempt, result)

    def test_prepared_result_drift_and_failed_receipts_cannot_be_washed(self) -> None:
        result = self.success()
        self.mutate("prepared-result.json", primary_error="original failure")
        with self.assertRaises(ValueError):
            evidence.publish_lifecycle_bundle(self.attempt, result)
        self.mutate("prepared-result.json", primary_error=None)
        self.document(
            "platform/finalize-failed.json",
            {"run_id": RUN, "status": "failed", "error": "earlier failure"},
        )
        with self.assertRaises(ValueError):
            evidence.publish_lifecycle_bundle(self.attempt, result)

    def test_tree_drift_during_publish_leaves_no_valid_seal_and_no_fake_quarantine(
        self,
    ) -> None:
        result = self.success()
        publish = evidence.publish_new_json

        def drift(path, value, **kwargs):
            digest = publish(path, value, **kwargs)
            if path.name == "manifest.json":
                (self.attempt / "automation/late.log").write_bytes(b"late")
            return digest

        with (
            mock.patch.object(evidence, "publish_new_json", side_effect=drift),
            self.assertRaises(ValueError),
        ):
            evidence.publish_lifecycle_bundle(self.attempt, result)
        with self.assertRaises((ValueError, FileNotFoundError)):
            evidence.verify_lifecycle_bundle(self.attempt)
        self.assertEqual(
            json.loads((self.attempt / "phone.json").read_bytes())["status"], "passed"
        )

    def reseal_file(self, name: str) -> None:
        """An integrity-only attacker update must still face semantic replay."""
        path = self.attempt / name
        manifest_path = self.attempt / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        for row in manifest["files"]:
            if row["path"] == name:
                row.update(
                    size=path.stat().st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                )
        manifest_path.write_text(json.dumps(manifest))

    def test_offline_rejects_rehashed_conflicts_duplicates_and_traversal_manifest(
        self,
    ) -> None:
        result = self.success()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        path = self.attempt / "platform/bound-session.json"
        original = path.read_bytes()
        manifest_path = self.attempt / "manifest.json"
        original_manifest = manifest_path.read_bytes()
        for raw in (
            json.dumps({"run_id": RUN, "host_session_id": "foreign"}),
            '{"run_id":"'
            + RUN
            + '","host_session_id":"first","host_session_id":"second"}',
            '{"run_id":"' + RUN + '","number":NaN}',
            '{"run_id":"' + RUN + '","number":1e999}',
        ):
            path.write_text(raw)
            self.reseal_file("platform/bound-session.json")
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                evidence.verify_lifecycle_bundle(self.attempt)
            manifest_path.write_bytes(original_manifest)
        path.write_bytes(original)
        for name in ("../outside", "/absolute", "candidate/../escape", "manifest.json"):
            manifest = json.loads(original_manifest)
            manifest["files"][0]["path"] = name
            manifest_path.write_text(json.dumps(manifest))
            with self.subTest(name=name), self.assertRaises(ValueError):
                evidence.verify_lifecycle_bundle(self.attempt)
        manifest_path.write_bytes(original_manifest)
        manifest_path.write_text('{"run_id":"' + RUN + '","run_id":"other"}')
        with self.assertRaises(ValueError):
            evidence.verify_lifecycle_bundle(self.attempt)

    def test_offline_rejects_links_modes_and_changed_frozen_candidate(self) -> None:
        result = self.success()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        path = self.attempt / "automation/close.json"
        original = path.read_bytes()
        outside = self.root / "close.json"
        outside.write_bytes(original)
        for hardlink in (True, False):
            path.unlink()
            if hardlink:
                os.link(outside, path)
            else:
                path.symlink_to(outside)
            with self.subTest(hardlink=hardlink), self.assertRaises(ValueError):
                evidence.verify_lifecycle_bundle(self.attempt)
        path.unlink()
        path.write_bytes(original)
        path.chmod(0o700)
        with self.assertRaises(ValueError):
            evidence.verify_lifecycle_bundle(self.attempt)
        path.chmod(0o644)
        candidate = self.attempt / "candidate.apk"
        candidate.write_bytes(candidate.read_bytes() + b"changed")
        self.reseal_file("candidate.apk")
        with self.assertRaises(ValueError):
            evidence.verify_lifecycle_bundle(self.attempt)

    def test_partial_failed_owner_and_phone_receipts_keep_all_error_channels(
        self,
    ) -> None:
        result = self.success()
        result.update(
            status="failed",
            scenario_status="failed",
            session_status="failed",
            host_gate_status="failed",
            phone_status="quarantined",
            primary_error="UI failed",
            cleanup_errors=["automation.close: DELETE failed"],
            phone_cleanup_errors=["release refused"],
        )
        self.mutate("candidate/finalization.json", action="force-stop")
        self.mutate(
            "automation/close.json",
            status="failed",
            errors=["DELETE failed"],
            session_closed=False,
        )
        self.mutate(
            "phone.json",
            status="quarantined",
            primary_error="UI failed",
            cleanup_errors=result["phone_cleanup_errors"],
        )
        self.document(
            "prepared-result.json",
            {
                **result,
                "host_gate_status": "pending",
                "phone_cleanup_errors": [],
                "phone_status": "not_acquired",
            },
        )
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        self.assertEqual(
            json.loads((self.attempt / "automation/close.json").read_bytes())["errors"],
            ["DELETE failed"],
        )

    def test_publish_failure_does_not_overwrite_foreign_manifest_or_released_phone(
        self,
    ) -> None:
        result = self.success()
        publish = evidence.publish_new_json

        def race(path, value, **kwargs):
            if path.name == "manifest.json":
                path.write_bytes(b"foreign pre-existing seal")
            return publish(path, value, **kwargs)

        with (
            mock.patch.object(evidence, "publish_new_json", side_effect=race),
            self.assertRaises(FileExistsError),
        ):
            evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(
            (self.attempt / "manifest.json").read_bytes(), b"foreign pre-existing seal"
        )
        self.assertEqual(
            json.loads((self.attempt / "phone.json").read_bytes())["status"], "passed"
        )
        self.assertEqual(
            json.loads((self.attempt / "result.json").read_bytes()), result
        )
        with self.assertRaises(ValueError):
            evidence.verify_lifecycle_bundle(self.attempt)

    def test_drift_before_result_publication_leaves_only_original_owner_evidence(
        self,
    ) -> None:
        result = self.success()
        facts = evidence._facts

        def drift(*args):
            facts(*args)
            (self.attempt / "automation/late.log").write_bytes(b"late")

        with (
            mock.patch.object(evidence, "_facts", side_effect=drift),
            self.assertRaises(ValueError),
        ):
            evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertFalse((self.attempt / "result.json").exists())
        self.assertFalse((self.attempt / "manifest.json").exists())

    def test_copy_preserves_old_destination_and_rejects_inode_swap_even_with_same_bytes(
        self,
    ) -> None:
        inspect = evidence._apk

        def swap(path, deadline):
            inspect(path, deadline)
            replacement = self.root / "replacement.apk"
            replacement.write_bytes(self.source.read_bytes())
            replacement.replace(self.source)

        with (
            mock.patch.object(evidence, "_apk", side_effect=swap),
            self.assertRaises(ValueError),
        ):
            self.freeze()
        self.assertFalse((self.attempt / "candidate.apk").exists())
        target = self.attempt / "candidate.apk"

        def replace_destination(path, deadline):
            inspect(path, deadline)
            replacement = self.root / "new-destination.apk"
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)

        with (
            mock.patch.object(evidence, "_apk", side_effect=replace_destination),
            self.assertRaises(ValueError),
        ):
            self.freeze()
        self.assertEqual(target.read_bytes(), self.source.read_bytes())

    def test_expansion_entry_json_and_deadline_budgets_are_not_advisory(self) -> None:
        for constant in ("MAX_APK_EXPANDED_BYTES", "MAX_APK_ENTRIES"):
            with (
                self.subTest(constant=constant),
                mock.patch.object(evidence, constant, 1),
                self.assertRaises(ValueError),
            ):
                self.freeze()
            self.assertFalse((self.attempt / "candidate.apk").exists())
        check = evidence._check_deadline
        calls = 0

        def expire(deadline):
            nonlocal calls
            calls += 1
            if calls > 5:
                raise TimeoutError("bounded copy expired")
            check(deadline)

        with (
            mock.patch.object(evidence, "_check_deadline", side_effect=expire),
            self.assertRaises(TimeoutError),
        ):
            self.freeze()
        self.assertFalse((self.attempt / "candidate.apk").exists())
        result = self.success()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        for constant in (
            "MAX_ENTRIES",
            "MAX_JSON_BYTES",
            "MAX_TOTAL_BYTES",
            "MAX_FILE_BYTES",
        ):
            with (
                self.subTest(constant=constant),
                mock.patch.object(evidence, constant, 1),
                self.assertRaises(ValueError),
            ):
                evidence.verify_lifecycle_bundle(self.attempt)

    def test_offline_rejects_boolean_or_float_manifest_sizes_and_missing_result_fields(
        self,
    ) -> None:
        result = self.success()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        manifest_path = self.attempt / "manifest.json"
        original = manifest_path.read_bytes()
        for changes in (
            {"schema_version": 1.0},
            {"schema_version": True},
            {"size": 1.0},
        ):
            doc = json.loads(original)
            if "size" in changes:
                doc["files"][0]["size"] = float(doc["files"][0]["size"])
            else:
                doc.update(changes)
            manifest_path.write_text(json.dumps(doc))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                evidence.verify_lifecycle_bundle(self.attempt)
        manifest_path.write_bytes(original)
        partial = dict(result)
        partial.pop("run_id")
        self.document("result.json", partial)
        self.reseal_file("result.json")
        with self.assertRaises(ValueError):
            evidence.verify_lifecycle_bundle(self.attempt)

    def test_failed_scenario_phone_release_is_not_recorded_as_phone_pass(self) -> None:
        result = self.success()
        result.update(
            status="failed",
            scenario_status="failed",
            host_gate_status="failed",
            primary_error="UI failed",
        )
        self.mutate("phone.json", status="failed", primary_error="UI failed")
        self.document(
            "prepared-result.json",
            {**result, "host_gate_status": "pending", "phone_status": "not_acquired"},
        )
        with self.assertRaises(ValueError):
            evidence.publish_lifecycle_bundle(self.attempt, result)
        result["phone_status"] = "failed"
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(
            evidence.verify_lifecycle_bundle(self.attempt)["phone_status"], "failed"
        )

    def test_pre_device_freeze_failure_can_seal_a_partial_attempt_without_apk(
        self,
    ) -> None:
        result = self.success()
        for directory in ("candidate", "helpers", "platform", "automation"):
            shutil.rmtree(self.attempt / directory)
        (self.attempt / "candidate.apk").unlink()
        (self.attempt / "phone.json").unlink()
        result.update(
            status="failed",
            scenario_status="not_run",
            session_status="not_run",
            host_gate_status="failed",
            phone_status="not_acquired",
            primary_error="invalid source APK",
            observations=[],
            startup_snapshot={},
            cleanup_order=["bundle.prepare"],
        )
        self.document("prepared-result.json", {**result, "host_gate_status": "pending"})
        evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertEqual(evidence.verify_lifecycle_bundle(self.attempt), result)
        self.assertFalse((self.attempt / "candidate.apk").exists())
        self.assertFalse((self.attempt / "phone.json").exists())

    def test_relocated_offline_replay_needs_no_original_source_or_live_commands(
        self,
    ) -> None:
        result = self.success()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        relocated = self.root / "relocated"
        shutil.copytree(self.attempt, relocated)
        shutil.rmtree(self.attempt)
        self.source.unlink()
        with mock.patch(
            "subprocess.run",
            side_effect=AssertionError("offline must not invoke commands"),
        ):
            replay = evidence.verify_lifecycle_bundle(relocated)
        self.assertEqual(replay, result)
        self.assertEqual(replay["host_gate_status"], "pending")
        self.assertEqual(replay["rf"], "not_run")

    def test_manifest_bytes_drift_during_publication_and_replay_is_not_hidden_by_json_equality(
        self,
    ) -> None:
        result = self.success()
        publish = evidence.publish_new_json

        def drift_manifest(path, value, **kwargs):
            digest = publish(path, value, **kwargs)
            if path.name == "manifest.json":
                path.write_bytes(path.read_bytes() + b" ")
            return digest

        with (
            mock.patch.object(evidence, "publish_new_json", side_effect=drift_manifest),
            self.assertRaises(ValueError),
        ):
            evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertFalse((self.attempt / "manifest.json").exists())
        (self.attempt / "result.json").unlink()
        evidence.publish_lifecycle_bundle(self.attempt, result)
        facts = evidence._facts

        def drift_replay(*args):
            facts(*args)
            path = self.attempt / "manifest.json"
            path.write_bytes(path.read_bytes() + b" ")

        with (
            mock.patch.object(evidence, "_facts", side_effect=drift_replay),
            self.assertRaises(ValueError),
        ):
            evidence.verify_lifecycle_bundle(self.attempt)

    def test_result_bytes_drift_before_manifest_is_not_adopted(self) -> None:
        result = self.success()
        publish = evidence.publish_new_json

        def drift_result(path, value, **kwargs):
            digest = publish(path, value, **kwargs)
            if path.name == "result.json":
                path.write_bytes(path.read_bytes() + b" ")
            return digest

        with (
            mock.patch.object(evidence, "publish_new_json", side_effect=drift_result),
            self.assertRaises(ValueError),
        ):
            evidence.publish_lifecycle_bundle(self.attempt, result)
        self.assertFalse((self.attempt / "manifest.json").exists())

    def test_source_drift_during_copy_is_rejected(self) -> None:
        original = evidence._check_deadline
        calls = 0

        def drift(deadline):
            nonlocal calls
            calls += 1
            if calls == 4:
                self.source.write_bytes(self.source.read_bytes() + b"drift")
            return original(deadline)

        with (
            mock.patch.object(evidence, "_check_deadline", side_effect=drift),
            self.assertRaises(ValueError),
        ):
            self.freeze()
        self.assertFalse((self.attempt / "candidate.apk").exists())


if __name__ == "__main__":
    unittest.main()
