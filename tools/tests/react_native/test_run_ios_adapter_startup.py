"""Offline runner failures and collector replay, never a live RF/full-run claim."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tools.react_native import rf_evidence_bundle as bundle_api
from tools.react_native import run_ios_adapter_startup as runner
from tools.react_native.ios_adapter_startup_journal import parse_journal
from tools.react_native.ios_device import canonical_physical_udid
from tools.tests.react_native.support.evidence_ios_adapter_startup import StartupBundle
from tools.tests.react_native.support.ios_adapter_startup_runner import offline_run
from tools.tests.react_native.support.ios_adapter_startup_samples import (
    DEVICE,
    RUN,
    put,
)


class IOSAdapterStartupRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def manifest(self, case) -> dict:
        path = case.attempt / bundle_api.MANIFEST
        bundle_api.verify_bundle(path)
        return json.loads(path.read_bytes())

    def assert_finalized(self, case, primary: BaseException) -> dict:
        self.assertEqual(case.events.count("physical.finish"), 1)
        self.assertEqual(case.events.count("phone.finish"), 1)
        self.assertLess(
            case.events.index("physical.finish"), case.events.index("phone.finish")
        )
        self.assertIs(case.finish_primaries[0], primary)
        document = self.manifest(case)
        self.assertEqual(document["status"], "failed")
        self.assertEqual(
            document["primary_error"],
            {"type": type(primary).__name__, "message": str(primary)},
        )
        self.assertTrue(document["evidence_errors"])
        return document

    def test_authorization_rejected_before_any_attempt_lease_sdk_or_fixture_mutation(
        self,
    ) -> None:
        for authorization in (False, None, 1, "true"):
            with (
                self.subTest(authorization=authorization),
                offline_run(self.root) as case,
            ):
                with self.assertRaisesRegex(
                    bundle_api.EvidenceError, "--authorize-radio"
                ):
                    case.run(authorize_radio=authorization)
                self.assertEqual(case.events, [])
                self.assertEqual(case.commands, [])
                self.assertFalse(case.build.exists())
                self.assertFalse((case.root / "phone-locks").exists())
            # Each subcase owns separate synthetic input directories.
            shutil.rmtree(self.root / "inputs")
            shutil.rmtree(self.root / "source")

    def test_automation_preflight_failure_precedes_fixture_and_phone_lease(
        self,
    ) -> None:
        with (
            offline_run(self.root) as case,
            patch.object(
                runner,
                "run_handshake",
                side_effect=RuntimeError("automation preflight failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "automation preflight failed"),
        ):
            case.run()
        self.assertEqual(case.events, [])
        self.assertEqual(case.commands, [])
        self.assertEqual(case.phones, [])
        self.assertFalse((case.root / "phone-locks").exists())
        self.assertEqual(len(list(case.build.iterdir())), 1)

    def test_invalid_identity_inputs_or_missing_prebuilt_never_build_or_provision(
        self,
    ) -> None:
        cases = (
            {"app": self.root / "missing/BleHubNitroHost.app"},
            {"test_run": self.root / "missing.xctestrun"},
            {"fixture_contract": self.root / "missing-contract.json"},
            {"fixture_profile": self.root / "missing-profile.json"},
            {"timeout": float("nan")},
            {"timeout": float("inf")},
        )
        for index, overrides in enumerate(cases):
            with self.subTest(overrides=overrides):
                root = self.root / str(index)
                root.mkdir()
                with offline_run(root) as case:
                    with self.assertRaises((OSError, ValueError, RuntimeError)):
                        case.run(**overrides)
                    self.assertEqual(case.events, [])
                    self.assertFalse(case.build.exists())
        with offline_run(self.root) as case:
            for device in (
                "",
                "default",
                "device with spaces",
                None,
                123,
                "G" * 40,
                "00008110-000C61160C39401G",
                "12345678-1234-4234-8234-123456789abc",
                " " + DEVICE,
                DEVICE + "\n",
            ):
                with (
                    self.subTest(device=device),
                    self.assertRaisesRegex(ValueError, "explicit physical iOS UDID"),
                ):
                    runner.run(
                        device,
                        app=case.app,
                        test_run=case.test_run,
                        fixture_contract=case.contract,
                        fixture_profile=case.profile,
                        authorize_radio=True,
                    )
            self.assertEqual(case.events, [])
            self.assertEqual(case.commands, [])
            self.assertEqual(case.phones, [])
            self.assertFalse(case.build.exists())
            self.assertFalse((case.root / "phone-locks").exists())

    def test_modern_lowercase_input_is_canonical_for_sdk_bundle_and_lease(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        with (
            offline_run(self.root) as case,
            patch.object(runner, "IosRadioLease", side_effect=case.phone) as lease,
            patch.object(
                runner,
                "require_ready_unlocked_device",
                wraps=runner.require_ready_unlocked_device,
            ) as ready,
        ):
            # Stop before a window: this is identity propagation, not a live pass.
            primary = case.faults.capture_before = RuntimeError(
                "identity-only capture stop"
            )
            self.assertNotEqual(DEVICE.lower(), DEVICE)
            with self.assertRaises(RuntimeError) as raised:
                case.run(device=DEVICE.lower())
            self.assertIs(raised.exception, primary)
            document = self.assert_finalized(case, primary)
            self.assertEqual(document["device"], DEVICE)
            ready.assert_called_once_with(DEVICE, cwd=case.root)
            self.assertEqual(lease.call_count, 1)
            self.assertEqual(lease.call_args.args[0], DEVICE)
            phone = case.phones[0]
            self.assertEqual(phone.device, DEVICE)
            self.assertEqual(phone.document["device"], DEVICE)
            self.assertEqual(
                phone.lock.name, hashlib.sha256(DEVICE.encode()).hexdigest()
            )
            self.assertNotEqual(
                phone.lock.name, hashlib.sha256(DEVICE.lower().encode()).hexdigest()
            )
            phone_evidence = json.loads(
                (case.attempt / runner.evidence.names()["phone_lease"]).read_bytes()
            )
            self.assertEqual(phone_evidence["device"], DEVICE)
            self.assertEqual(len(case.commands), 1)
            command = case.commands[0]
            self.assertEqual(command[command.index("--device") + 1], DEVICE)
            self.assertEqual(case.events.count("capture.run"), 1)
            self.assertNotIn("physical.open_on", case.events)
            self.assertEqual(document["status"], "failed")
            self.assertTrue(phone.finished)
            self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_physical_udid_helper_canonicalizes_modern_and_legacy_without_devices(
        self,
    ) -> None:
        legacy = "0123456789abcdef0123456789abcdef01234567"
        with (
            patch.object(
                subprocess,
                "run",
                side_effect=AssertionError("real subprocess forbidden"),
            ),
            patch.object(
                subprocess, "Popen", side_effect=AssertionError("real child forbidden")
            ),
        ):
            for raw, expected in (
                (DEVICE.lower(), DEVICE),
                (DEVICE, DEVICE),
                (legacy.upper(), legacy),
                (legacy, legacy),
            ):
                with self.subTest(raw=raw):
                    self.assertEqual(canonical_physical_udid(raw), expected)
                    self.assertEqual(
                        canonical_physical_udid(canonical_physical_udid(raw)), expected
                    )
        self.assertEqual(list(self.root.iterdir()), [])

    def test_lease_acquisition_failure_blocks_all_sdk_fixture_and_app_work(
        self,
    ) -> None:
        with offline_run(self.root) as case:
            primary = case.faults.acquire = RuntimeError("phone occupied")
            with self.assertRaisesRegex(RuntimeError, "phone occupied") as raised:
                case.run()
            self.assertIs(raised.exception, primary)
            self.assertEqual(case.events, ["prepare", "phone.acquire", "phone.finish"])
            self.assertEqual(case.commands, [])
            self.assertEqual(
                self.manifest(case)["primary_error"]["message"], str(primary)
            )

    def test_fixture_start_failure_prevents_install_and_xctest(self) -> None:
        with offline_run(self.root) as case:
            primary = case.faults.start = RuntimeError("attestation unavailable")
            with self.assertRaisesRegex(
                RuntimeError, "attestation unavailable"
            ) as raised:
                case.run()
            self.assertIs(raised.exception, primary)
            self.assertEqual(case.commands, [])
            self.assertEqual(case.captures, [])
            self.assertLess(
                case.events.index("phone.acquire"),
                case.events.index("sdk.device_ready"),
            )
            self.assertLess(
                case.events.index("sdk.device_ready"),
                case.events.index("physical.start"),
            )
            self.assert_finalized(case, primary)
            self.assertEqual(case.phones[0].document["status"], "quarantined")
            self.assertTrue(case.phones[0].lock.exists())

    def test_receipt_must_attest_inprocess_pid_pgid_before_install(self) -> None:
        with offline_run(self.root) as case:
            case.faults.receipt_identity = (os.getpid() + 1, os.getpgid(0))
            with self.assertRaisesRegex(RuntimeError, "run/process/image") as raised:
                case.run()
            self.assertEqual(case.commands, [])
            self.assertEqual(case.captures, [])
            self.assert_finalized(case, raised.exception)

    def test_install_failure_never_launches_or_manufactures_a_ready_callback(
        self,
    ) -> None:
        with offline_run(self.root) as case:
            primary = case.faults.install = RuntimeError("install rejected")
            with self.assertRaisesRegex(RuntimeError, "install rejected") as raised:
                case.run()
            self.assertIs(raised.exception, primary)
            self.assertEqual(case.captures, [])
            self.assertEqual(len(case.commands), 1)
            self.assertNotIn("physical.open_on", case.events)
            self.assertNotIn("collect.control", case.events)
            self.assert_finalized(case, primary)

    def test_real_session_receipt_off_prefix_and_ui_identity_precede_open_on(
        self,
    ) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        with (
            offline_run(self.root) as case,
            patch.object(
                runner, "verify_receipt", wraps=runner.verify_receipt
            ) as receipt,
        ):
            primary = case.faults.capture_after
            with self.assertRaisesRegex(
                RuntimeError, "XCTest failed after window"
            ) as raised:
                case.run()
            self.assertIs(raised.exception, primary)
            plan = case.plans[0]
            document = self.assert_finalized(case, primary)
            self.assertEqual(plan.session_id, document["run_id"])
            self.assertEqual(
                json.loads(plan.request_path.read_bytes())["session_id"],
                document["run_id"],
            )
            self.assertTrue(receipt.call_args_list)
            for call in receipt.call_args_list:
                self.assertEqual(
                    call.kwargs["fixture_identity"], (os.getpid(), os.getpgid(0))
                )
            off_path = case.attempt / runner.evidence.names()["off_journal"]
            expected = runner.ledger.verify_off_journal(
                parse_journal(off_path.read_bytes(), plan.session_id)
            )
            self.assertEqual(case.off, expected)
            self.assertEqual(
                expected["journal_sha256"],
                hashlib.sha256(off_path.read_bytes()).hexdigest(),
            )
            for earlier, later in (
                ("phone.acquire", "sdk.install"),
                ("physical.start", "sdk.install"),
                ("sdk.install", "capture.run"),
                ("capture.snapshot_off_journal", "physical.open_on"),
                ("physical.open_on", "capture.ready_returned"),
                ("collect.control", "physical.finish"),
                ("capture.download_journal", "physical.finish"),
            ):
                self.assertLess(case.events.index(earlier), case.events.index(later))
            self.assertEqual(case.events.count("capture.run"), 1)
            self.assertEqual(case.events.count("physical.open_on"), 1)
            self.assertEqual(len(case.commands), 1)
            self.assertEqual(case.phones[0].document["status"], "released")
            self.assertFalse(case.phones[0].lock.exists())
            self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_invalid_off_snapshot_or_foreign_ui_identity_never_opens_on(self) -> None:
        for fault in (
            "missing-prefix",
            "foreign-native",
            "foreign-nonce",
            "foreign-run",
        ):
            with self.subTest(fault=fault):
                root = self.root / fault
                root.mkdir()
                with offline_run(root) as case:
                    if fault == "missing-prefix":
                        case.faults.off_document = {"schema_version": 1, "records": []}
                    else:
                        # Keep the other fields valid for the current session so
                        # each case reaches the exact UI/OFF identity comparison.
                        original_capture = case.capture

                        def capture(
                            *args,
                            original_capture=original_capture,
                            fault=fault,
                            **kwargs,
                        ):
                            result = original_capture(*args, **kwargs)
                            identity = copy.deepcopy(result.window_identity)
                            if fault == "foreign-native":
                                identity["native"]["pid"] += 1
                            elif fault == "foreign-nonce":
                                identity["nonce"] = "foreign-one-two"
                            else:
                                identity["run_id"] = RUN
                            result.window_identity = identity
                            return result

                        with (
                            patch.object(
                                runner, "StartupWindowCapture", side_effect=capture
                            ),
                            self.assertRaisesRegex(
                                bundle_api.EvidenceError, "UI window identity"
                            ) as raised,
                        ):
                            case.run()
                        self.assert_finalized(case, raised.exception)
                        self.assertNotIn("physical.open_on", case.events)
                        self.assertNotIn("capture.ready_returned", case.events)
                        self.assertNotIn("capture.ui_control_completed", case.events)
                        self.assertEqual(case.control["status"], "failed")
                        continue
                    with self.assertRaises(bundle_api.EvidenceError) as raised:
                        case.run()
                    self.assert_finalized(case, raised.exception)
                    self.assertNotIn("physical.open_on", case.events)
                    self.assertNotIn("capture.ready_returned", case.events)
                    self.assertNotIn("capture.ui_control_completed", case.events)
                    self.assertEqual(case.control["status"], "failed")

    def test_capture_failure_before_window_does_not_simulate_callbacks_or_retry(
        self,
    ) -> None:
        with offline_run(self.root) as case:
            primary = case.faults.capture_before = RuntimeError(
                "runner initialization failed"
            )
            with self.assertRaisesRegex(RuntimeError, "runner initialization failed"):
                case.run()
            self.assertNotIn("capture.snapshot_off_journal", case.events)
            self.assertNotIn("physical.open_on", case.events)
            self.assertNotIn("capture.ui_control_completed", case.events)
            self.assertEqual(case.control["status"], "failed")
            self.assertEqual(case.events.count("capture.run"), 1)
            self.assertEqual(case.events.count("sdk.install"), 1)
            self.assert_finalized(case, primary)

    def test_finally_collectors_are_independent_and_primary_cleanup_evidence_stay_distinct(
        self,
    ) -> None:
        with offline_run(self.root) as case:
            primary = case.faults.capture_before = RuntimeError("original XCTest error")
            case.faults.control = RuntimeError("attachment export failed")
            case.faults.summary = RuntimeError("summary unavailable")
            case.faults.journal = RuntimeError("journal unavailable")
            case.faults.physical_errors = ["physical retirement failed"]
            with self.assertRaisesRegex(
                RuntimeError, "original XCTest error"
            ) as raised:
                case.run()
            self.assertIs(raised.exception, primary)
            for event in (
                "collect.control",
                "collect.summary",
                "capture.download_journal",
            ):
                self.assertEqual(case.events.count(event), 1)
            document = self.assert_finalized(case, primary)
            for text in (
                "attachment export failed",
                "summary unavailable",
                "journal unavailable",
                "physical retirement failed",
            ):
                self.assertTrue(
                    any(text in error for error in document["cleanup_errors"]), text
                )
            self.assertNotIn(
                "original XCTest error", ";".join(document["cleanup_errors"])
            )
            self.assertTrue(
                any("cleanup/evidence" in note for note in primary.__notes__)
            )
            self.assertEqual(case.phones[0].document["status"], "quarantined")
            self.assertTrue(case.phones[0].lock.exists())

    def test_finally_collector_keyboard_interrupts_keep_primary_and_finish_all_owners(
        self,
    ) -> None:
        collectors = (
            ("control", "collect.control", "control export"),
            ("summary", "collect.summary", "summary"),
            ("journal", "capture.download_journal", "journal"),
        )
        for fault, interrupted_event, label in collectors:
            with self.subTest(collector=fault):
                root = self.root / fault
                root.mkdir()
                previous = signal.getsignal(signal.SIGTERM)
                with offline_run(root) as case:
                    primary = case.faults.capture_before = RuntimeError(
                        "original capture error"
                    )
                    interruption = KeyboardInterrupt(f"{fault} collection interrupted")
                    setattr(case.faults, fault, interruption)
                    # Catch BaseException here so a regression becomes a failed
                    # subcase, not a KeyboardInterrupt that aborts the test suite.
                    with self.assertRaises(BaseException) as raised:
                        case.run()
                    self.assertIs(raised.exception, primary)
                    document = self.assert_finalized(case, primary)
                    expected_error = f"{label}: KeyboardInterrupt: {interruption}"
                    self.assertIn(expected_error, document["cleanup_errors"])
                    self.assertIn(expected_error, ";".join(primary.__notes__))
                    self.assertNotIn(str(primary), ";".join(document["cleanup_errors"]))
                    self.assertFalse(document["completed"])
                    self.assertIsNotNone(document["finished_at"])
                    for role, event, _label in collectors:
                        self.assertEqual(case.events.count(event), 1)
                        self.assertEqual(
                            (case.attempt / runner.evidence.names()[role]).is_file(),
                            event != interrupted_event,
                        )
                    for earlier, later in (
                        ("collect.control", "collect.summary"),
                        ("collect.summary", "capture.download_journal"),
                        ("capture.download_journal", "physical.finish"),
                        ("physical.finish", "phone.finish"),
                    ):
                        self.assertLess(
                            case.events.index(earlier), case.events.index(later)
                        )
                    phone = case.phones[0]
                    self.assertTrue(phone.finished)
                    self.assertEqual(phone.document["status"], "quarantined")
                    self.assertTrue(phone.lock.exists())
                    phone_path = case.attempt / runner.evidence.names()["phone_lease"]
                    self.assertEqual(
                        json.loads(phone_path.read_bytes())["status"], "quarantined"
                    )
                    self.assertIn(
                        runner.evidence.names()["phone_lease"],
                        {row["path"] for row in document["files"]},
                    )
                    self.assertEqual(case.events.count("capture.run"), 1)
                    self.assertNotIn("capture.snapshot_off_journal", case.events)
                    self.assertNotIn("physical.open_on", case.events)
                    self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_missing_on_proof_quarantines_phone_without_retry_or_relaunch(self) -> None:
        with offline_run(self.root) as case:
            case.faults.control_update = {
                "status": "failed",
                "restored": False,
                "same_session": False,
                "primary_error": "Settings ON readback absent",
                "observations": [],
            }
            with self.assertRaisesRegex(RuntimeError, "XCTest failed after window"):
                case.run()
            self.assertEqual(case.phones[0].document["status"], "quarantined")
            self.assertTrue(case.phones[0].lock.exists())
            self.assertEqual(case.events.count("capture.run"), 1)
            self.assertEqual(case.events.count("physical.open_on"), 1)
            self.assertEqual(case.events.count("sdk.install"), 1)
            document = self.manifest(case)
            self.assertEqual(document["status"], "failed")
            self.assertEqual(
                json.loads(
                    (case.attempt / runner.evidence.names()["control"]).read_bytes()
                )["status"],
                "failed",
            )

    def test_raised_physical_finish_still_finishes_phone_bundle_and_restores_signal(
        self,
    ) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        with offline_run(self.root) as case:
            primary = case.faults.capture_before = RuntimeError(
                "original capture error"
            )
            case.faults.physical_finish = RuntimeError(
                "unexpected physical finish error"
            )
            with self.assertRaises(RuntimeError) as raised:
                case.run()
            self.assertIs(raised.exception, primary)
            document = self.assert_finalized(case, primary)
            self.assertTrue(
                any(
                    "unexpected physical finish error" in value
                    for value in document["cleanup_errors"]
                )
            )
            self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_sigterm_handler_raises_interruption_then_restores_previous_handler(
        self,
    ) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        with offline_run(self.root) as case:

            def interrupt(*_args, **_kwargs):
                handler = signal.getsignal(signal.SIGTERM)
                self.assertTrue(callable(handler))
                handler(signal.SIGTERM, None)

            with (
                patch.object(runner, "_sdk", side_effect=interrupt),
                self.assertRaisesRegex(InterruptedError, "SIGTERM") as raised,
            ):
                case.run()
            self.assert_finalized(case, raised.exception)
            self.assertIs(signal.getsignal(signal.SIGTERM), previous)
            self.assertEqual(case.captures, [])
            self.assertEqual(case.phones[0].document["status"], "quarantined")

    def test_bundle_publication_failure_adds_note_without_replacing_primary(
        self,
    ) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        with offline_run(self.root) as case:
            primary = case.faults.capture_before = RuntimeError(
                "original capture error"
            )
            with (
                patch.object(
                    bundle_api.EvidenceBundle,
                    "finish",
                    side_effect=OSError("bundle publication failed"),
                ),
                self.assertRaises(RuntimeError) as raised,
            ):
                case.run()
            self.assertIs(raised.exception, primary)
            self.assertIn("bundle publication failed", ";".join(primary.__notes__))
            self.assertLess(
                case.events.index("physical.finish"), case.events.index("phone.finish")
            )
            self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_real_final_verifier_rejects_incomplete_physical_evidence_not_mocked_pass(
        self,
    ) -> None:
        with offline_run(self.root) as case:
            case.faults.capture_after = None
            with self.assertRaises((RuntimeError, OSError)):
                case.run()
            self.assertEqual(case.events.count("physical.complete"), 1)
            document = self.manifest(case)
            self.assertEqual(document["status"], "failed")
            self.assertTrue(document["evidence_errors"])
            self.assertIsNone(document["primary_error"])
            self.assertTrue(document["cleanup_errors"])


class IOSAdapterStartupCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.attempt = self.root / "attempt"
        self.attempt.mkdir()
        self.result = self.root / "capture.xcresult"
        self.result.mkdir()
        self.filename = "01234567-ABCD-ABCD-ABCD-0123456789AB.json"
        self.raw = b'{\r\n "status" : "failed", "primary_error":"source failure"\r\n}\n'
        self.manifest = [
            {
                "testIdentifier": runner.evidence.ATTACHMENT_METHOD,
                "attachments": [
                    {
                        "deviceId": DEVICE,
                        "isAssociatedWithFailure": True,
                        "exportedFileName": self.filename,
                        "suggestedHumanReadableName": "ios-adapter-startup-control_0_01234567-ABCD-ABCD-ABCD-0123456789AB.json",
                    }
                ],
            }
        ]
        self.commands = []

    def sdk(self, command, **kwargs):
        self.commands.append((command, kwargs))
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["check"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["timeout"], 30)
        if command[:4] == ["xcrun", "xcresulttool", "export", "attachments"]:
            self.assertEqual(
                command,
                [
                    "xcrun",
                    "xcresulttool",
                    "export",
                    "attachments",
                    "--path",
                    str(self.result),
                    "--output-path",
                    str(self.attempt / "xcresult-attachments"),
                ],
            )
            directory = Path(command[-1])
            directory.mkdir()
            put(directory / "manifest.json", self.manifest)
            put(directory / self.filename, self.raw)
        else:
            self.assertEqual(
                command,
                [
                    "xcrun",
                    "xcresulttool",
                    "get",
                    "test-results",
                    "summary",
                    "--path",
                    str(self.result),
                    "--compact",
                ],
            )
            kwargs["stdout"].write(self.summary_raw)
        return subprocess.CompletedProcess(command, 0)

    @contextmanager
    def sdk_boundary(self):
        with (
            patch.object(subprocess, "run", side_effect=self.sdk),
            patch.object(
                subprocess,
                "Popen",
                side_effect=AssertionError("real process forbidden"),
            ),
        ):
            yield

    def test_failed_control_retains_exact_attachment_bytes_and_hash_not_reserialized(
        self,
    ) -> None:
        with self.sdk_boundary():
            control, digest = runner.collect_control(self.result, self.attempt, DEVICE)
        self.assertEqual(control["status"], "failed")
        self.assertEqual(digest, hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(
            (self.attempt / runner.evidence.names()["control"]).read_bytes(), self.raw
        )
        self.assertEqual(
            (self.attempt / "xcresult-attachments" / self.filename).read_bytes(),
            self.raw,
        )
        self.assertNotEqual(self.raw, json.dumps(control).encode())
        self.assertEqual(len(self.commands), 1)

    def test_exact_method_device_safe_filename_and_unique_control_required(
        self,
    ) -> None:
        cases = (
            (
                "method",
                lambda m: m[0].update(testIdentifier=runner.evidence.TEST_METHOD),
            ),
            ("foreign-method", lambda m: m[0].update(testIdentifier="Other/test()")),
            (
                "device",
                lambda m: m[0]["attachments"][0].update(deviceId="foreign-device"),
            ),
            (
                "traversal",
                lambda m: m[0]["attachments"][0].update(
                    exportedFileName="../" + self.filename
                ),
            ),
            (
                "absolute",
                lambda m: m[0]["attachments"][0].update(
                    exportedFileName="/tmp/" + self.filename
                ),
            ),
            (
                "suggested",
                lambda m: m[0]["attachments"][0].update(
                    suggestedHumanReadableName="ios-adapter-startup-control.json"
                ),
            ),
            (
                "duplicate",
                lambda m: m[0]["attachments"].append(
                    copy.deepcopy(m[0]["attachments"][0])
                ),
            ),
            (
                "foreign-extra",
                lambda m: m[0]["attachments"].append({"deviceId": "foreign-device"}),
            ),
            ("extra-method", lambda m: m.append(copy.deepcopy(m[0]))),
        )
        original = copy.deepcopy(self.manifest)
        for name, mutate in cases:
            with self.subTest(name=name):
                self.manifest = copy.deepcopy(original)
                mutate(self.manifest)
                with self.sdk_boundary(), self.assertRaises(bundle_api.EvidenceError):
                    runner.collect_control(self.result, self.attempt, DEVICE)
                self.assertFalse(
                    (self.attempt / runner.evidence.names()["control"]).exists()
                )
                shutil.rmtree(self.attempt / "xcresult-attachments")
                (self.attempt / "xcresult-attachments-export.log").unlink()

    def test_existing_export_or_missing_result_refused_before_sdk(self) -> None:
        with self.sdk_boundary():
            self.result.rmdir()
            with self.assertRaisesRegex(bundle_api.EvidenceError, "result unavailable"):
                runner.collect_control(self.result, self.attempt, DEVICE)
            self.result.mkdir()
            (self.attempt / "xcresult-attachments").mkdir()
            with self.assertRaisesRegex(
                bundle_api.EvidenceError, "existing XCTest attachment"
            ):
                runner.collect_control(self.result, self.attempt, DEVICE)
        self.assertEqual(self.commands, [])

    def test_collector_and_summary_replay_through_real_complete_evidence_bundle(
        self,
    ) -> None:
        # This is collector + real bundle/ledger replay, not full runner integration.
        root = self.root / "source"
        root.mkdir()
        fixture = StartupBundle(root)
        self.attempt = fixture.attempt
        self.manifest = json.loads(fixture.path("attachments").read_bytes())
        self.raw = b"\r\n " + fixture.path("control").read_bytes() + b"\t\r\n"
        self.summary_raw = fixture.path("summary").read_bytes() + b"\n"
        shutil.rmtree(self.attempt / "xcresult-attachments")
        fixture.path("control").unlink()
        fixture.path("summary").unlink()
        with self.sdk_boundary():
            control, digest = runner.collect_control(self.result, self.attempt, DEVICE)
            runner.collect_summary(self.result, self.attempt)
        self.assertEqual(fixture.path("control").read_bytes(), self.raw)
        self.assertEqual(fixture.path("summary").read_bytes(), self.summary_raw)
        phone = fixture.load("phone_lease")
        phone["control_sha256"] = digest
        fixture.write("phone_lease", phone)
        self.assertEqual(control["status"], "passed")
        fixture.finish()
        bundle_api.verify_bundle(fixture.attempt / bundle_api.MANIFEST)
        self.assertEqual(fixture.bundle.document["status"], "passed")
        self.assertEqual(len(self.commands), 2)

    def test_failed_export_is_retained_in_failed_bundle_never_normalized_to_pass(
        self,
    ) -> None:
        root = self.root / "source"
        root.mkdir()
        fixture = StartupBundle(root)
        self.attempt = fixture.attempt
        self.manifest = json.loads(fixture.path("attachments").read_bytes())
        self.manifest[0]["attachments"][0]["isAssociatedWithFailure"] = True
        control = fixture.load("control")
        control.update(
            status="failed", primary_error="XCTest source failed", same_session=False
        )
        self.raw = json.dumps(control, indent=3).encode() + b"\r\n"
        shutil.rmtree(self.attempt / "xcresult-attachments")
        fixture.path("control").unlink()
        with self.sdk_boundary():
            collected, digest = runner.collect_control(
                self.result, self.attempt, DEVICE
            )
        self.assertEqual(collected, control)
        self.assertEqual(digest, hashlib.sha256(self.raw).hexdigest())
        fixture.finish(completed=False, error=RuntimeError("XCTest source failed"))
        bundle_api.verify_bundle(fixture.attempt / bundle_api.MANIFEST)
        self.assertEqual(fixture.bundle.document["status"], "failed")
        self.assertEqual(fixture.path("control").read_bytes(), self.raw)
        self.assertTrue(
            json.loads(fixture.path("attachments").read_bytes())[0]["attachments"][0][
                "isAssociatedWithFailure"
            ]
        )


if __name__ == "__main__":
    unittest.main()
