"""No-device contracts for the single-owner iOS system-loss runner."""

from __future__ import annotations

import hashlib
import plistlib
import tempfile
import unittest
from pathlib import Path

from tests.peripherals.nrf52840 import system_loss
from tools.apple.run_bleng_ios_system_loss import (
    LocalRelay,
    SystemLossRunnerError,
    _digest,
    _lease_control_without_host,
    _pull_failure_diagnostics,
    _resolve_sniffer_match,
    _validate_fresh_owner_recovery,
    _validate_loss_started,
    stage_xctestrun,
)


class FreshOwnerRecoveryValidationTests(unittest.TestCase):
    def test_requires_same_process_new_owner_and_backend(self) -> None:
        run_id = "11111111-2222-4333-8444-555555555555"
        identity = {
            "pid": 321,
            "boot_id": "22222222-2222-4333-8444-555555555555",
            "owner_id": "33333333-3333-4333-8444-555555555555",
            "backend_id": "44444444-4444-4333-8444-555555555555",
        }
        control = {"session_id": run_id, "case": "write"}
        terminals = {
            "schema_version": 1,
            "state": "loss-terminals",
            "session_id": run_id,
            "control_sha256": system_loss.control_digest(control),
            "witness_sha256": "ignored-by-this-test",
            "terminals": [{"command_id": "10", "result": "connection-lost"}],
        }
        recovery = {
            "schema_version": 1,
            "state": "fresh-owner-recovered",
            "recovery_mode": "fresh-owner",
            "session_id": run_id,
            "control_sha256": system_loss.control_digest(control),
            "terminals_sha256": _digest(terminals),
            "old_identity": identity,
            "new_identity": {
                **identity,
                "owner_id": "55555555-5555-4555-8555-555555555555",
                "backend_id": "66666666-6666-4666-8666-666666666666",
            },
            "old_terminals": terminals["terminals"],
            "new_link": {
                "connection_id": "11",
                "gatt_session_id": "12",
                "subscription_id": "13",
            },
        }
        old, new, link = _validate_fresh_owner_recovery(
            recovery, run_id, control, terminals, identity
        )
        self.assertEqual(old, identity)
        self.assertNotEqual(old["owner_id"], new["owner_id"])
        self.assertEqual(link["subscription_id"], "13")
        with self.assertRaisesRegex(SystemLossRunnerError, "same-process"):
            _validate_fresh_owner_recovery(
                {**recovery, "new_identity": {**recovery["new_identity"], "pid": 999}},
                run_id,
                control,
                terminals,
                identity,
            )


class SystemLossCleanupTests(unittest.TestCase):
    def test_preflight_failure_without_relay_does_not_mask_primary_error(self) -> None:
        _pull_failure_diagnostics(None)

    def test_pre_mutation_failure_binds_empty_fixture_digest(self) -> None:
        control = _lease_control_without_host(
            "12345678-1234-4234-8234-123456789abc",
            hashlib.sha256(b"").hexdigest(),
            RuntimeError("UI automation preflight failed"),
        )
        self.assertFalse(control["mutation_attempted"])
        self.assertEqual(control["observations"], [])
        self.assertEqual(
            control["fixture_receipt_sha256"], hashlib.sha256(b"").hexdigest()
        )


class SystemLossAuthorizationObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_id = "11111111-2222-4333-8444-555555555555"
        self.identity = {
            "pid": 321,
            "boot_id": "22222222-2222-4333-8444-555555555555",
            "owner_id": "33333333-3333-4333-8444-555555555555",
            "backend_id": "44444444-4444-4333-8444-555555555555",
        }
        self.control = {"session_id": self.run_id, "case": "write"}
        self.witness = {
            "schema_version": 1,
            "identity": {**self.identity, "initialize_command_id": "1"},
        }

    def document(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "state": "loss-started",
            "session_id": self.run_id,
            "control_sha256": system_loss.control_digest(self.control),
            "witness_sha256": system_loss.witness_digest(self.witness),
            "identity": self.witness["identity"],
            "authorization": "host-authorized",
        }

    def test_loss_started_is_a_strict_pre_off_identity_witness(self) -> None:
        _validate_loss_started(
            self.document(),
            self.run_id,
            self.control,
            self.witness,
            self.identity,
        )

    def test_loss_started_rejects_foreign_owner_before_off(self) -> None:
        document = self.document()
        document["identity"] = {**self.identity, "pid": 322}
        with self.assertRaisesRegex(SystemLossRunnerError, "owner identity drift"):
            _validate_loss_started(
                document,
                self.run_id,
                self.control,
                self.witness,
                self.identity,
            )


class SystemLossRelayTests(unittest.TestCase):
    def test_local_relay_is_atomic_and_rejects_nested_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / "local"
            remote = root / "remote"
            relay = LocalRelay(local, remote)
            (local / "system-loss-control.json").write_bytes(b"control")
            relay.push("system-loss-control.json")
            self.assertEqual(
                (remote / "system-loss-control.json").read_bytes(), b"control"
            )
            (remote / "system-loss-setup.json").write_bytes(b"setup")
            self.assertTrue(relay.pull("system-loss-setup.json"))
            self.assertEqual((local / "system-loss-setup.json").read_bytes(), b"setup")
            with self.assertRaisesRegex(SystemLossRunnerError, "flat"):
                relay.push("nested/system-loss-control.json")

    def test_optional_pull_does_not_replace_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = LocalRelay(root / "local", root / "remote")
            local = root / "local" / "system-loss-physical.json"
            local.write_bytes(b"old")
            self.assertFalse(relay.pull("system-loss-physical.json", required=False))
            self.assertEqual(local.read_bytes(), b"old")


class SystemLossSnifferTests(unittest.TestCase):
    def test_dominant_attested_service_candidate_is_selected(self) -> None:
        selected, method = _resolve_sniffer_match(
            [
                {
                    "address": "AA:AA:AA:AA:AA:01",
                    "packets": 384,
                    "names": [],
                    "service_uuids": ["fd700000-0000-4000-8000-000000000000"],
                },
                {
                    "address": "AA:AA:AA:AA:AA:02",
                    "packets": 1,
                    "names": [],
                    "service_uuids": ["fd700000-0000-4000-8000-000000000000"],
                },
            ],
            local_name="BleHubMultiRfV2DKB",
            service_uuid="fd700000-0000-4000-8000-000000000000",
        )
        self.assertEqual(selected["address"], "AA:AA:AA:AA:AA:01")
        self.assertEqual(method, "attested-service-uuid-dominant-packet-count")

    def test_weak_multiple_attested_candidates_fail_closed(self) -> None:
        with self.assertRaisesRegex(SystemLossRunnerError, "multiple non-dominant"):
            _resolve_sniffer_match(
                [
                    {
                        "address": "AA:AA:AA:AA:AA:01",
                        "packets": 7,
                        "names": [],
                        "service_uuids": ["fd700000-0000-4000-8000-000000000000"],
                    },
                    {
                        "address": "AA:AA:AA:AA:AA:02",
                        "packets": 2,
                        "names": [],
                        "service_uuids": ["fd700000-0000-4000-8000-000000000000"],
                    },
                ],
                local_name="BleHubMultiRfV2DKB",
                service_uuid="fd700000-0000-4000-8000-000000000000",
            )


class SystemLossXctestRunTests(unittest.TestCase):
    def test_stage_pairs_signed_app_and_injects_only_matching_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "BlengAppleIOSProbe.app"
            app.mkdir()
            (app / "embedded.mobileprovision").write_bytes(b"signed")
            source = root / "candidate.xctestrun"
            source.write_bytes(
                plistlib.dumps(
                    {
                        "BlengAppleIOSProbeUITests": {
                            "TestBundlePath": "BlengAppleIOSProbeUITests.xctest",
                            "EnvironmentVariables": {"EXISTING": "value"},
                        }
                    }
                )
            )
            staged = stage_xctestrun(
                source,
                root / "staged.xctestrun",
                app=app,
                bundle_id="dev.blehub.bleng-apple-ios-probe",
                environment={"BLEHUB_SYSTEM_LOSS_UI_RUN": "1"},
            )
            document = plistlib.loads(staged.read_bytes())
            target = document["BlengAppleIOSProbeUITests"]
            self.assertEqual(target["UITargetAppPath"], str(app.resolve()))
            self.assertFalse(target["UseUITargetAppProvidedByTests"])
            self.assertEqual(
                target["EnvironmentVariables"]["BLEHUB_SYSTEM_LOSS_UI_RUN"], "1"
            )

    def test_stage_refuses_unsigned_app_and_conflicting_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "Host.app"
            app.mkdir()
            source = root / "candidate.xctestrun"
            source.write_bytes(
                plistlib.dumps(
                    {
                        "BlengAppleIOSProbeUITests": {
                            "TestBundlePath": "test.xctest",
                            "EnvironmentVariables": {"BLEHUB_SYSTEM_LOSS_UI_RUN": "0"},
                        }
                    }
                )
            )
            with self.assertRaisesRegex(SystemLossRunnerError, "signed"):
                stage_xctestrun(
                    source,
                    root / "staged.xctestrun",
                    app=app,
                    bundle_id="bundle",
                    environment={"BLEHUB_SYSTEM_LOSS_UI_RUN": "1"},
                )
            (app / "embedded.mobileprovision").write_bytes(b"signed")
            with self.assertRaisesRegex(SystemLossRunnerError, "stale xctestrun"):
                stage_xctestrun(
                    source,
                    root / "staged.xctestrun",
                    app=app,
                    bundle_id="bundle",
                    environment={"BLEHUB_SYSTEM_LOSS_UI_RUN": "1"},
                )


if __name__ == "__main__":
    unittest.main()
