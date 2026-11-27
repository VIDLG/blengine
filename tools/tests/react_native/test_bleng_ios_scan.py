"""Offline iOS managed-scene input, freeze and evidence-fact regressions."""

from __future__ import annotations

import os
import plistlib
import tempfile
import time
import unittest
from pathlib import Path

from tools.apple.app_artifact import fingerprint
from tools.react_native import bleng_ios_scan as producer
from tools.react_native import bleng_scan_evidence as evidence
from tools.react_native.ios_radio_lease import SCAN_OBSERVATION_SCOPE


class IosScanInputTests(unittest.TestCase):
    device = "00008110-001234567890ABCD"
    bundle = "FWVYQ287W5.bleng-rn-host"
    executable = "BleHubBlengHost"
    team = "FWVYQ287W5"
    identity = "Apple Development: ZiJian LIU (6J7DUSCP6X)"

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.app = self.root / "BleHubBlengHost.app"
        self.app.mkdir()
        (self.app / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": self.bundle,
                    "CFBundleExecutable": self.executable,
                }
            )
        )
        (self.app / self.executable).write_bytes(b"synthetic executable")
        self.xctestrun = self.root / "host.xctestrun"
        self.xctestrun.write_bytes(b"<plist/>")
        self.output = self.root / "output"
        self.sha = fingerprint(self.app)

    def values(self, **changes: object) -> dict:
        values = {
            "device": self.device,
            "app": self.app,
            "app_tree_sha256": self.sha,
            "app_id": self.bundle,
            "executable": self.executable,
            "team": self.team,
            "identity": self.identity,
            "xctestrun": self.xctestrun,
            "output_root": self.output,
            "cwd": self.root,
            "deadline": time.monotonic() + 60,
        }
        values.update(changes)
        return values

    def test_accepts_one_physical_device_frozen_app_and_its_test_bundle(self) -> None:
        producer.validate_ios_scan_inputs(**self.values())

    def test_rejects_ambiguous_or_unreviewed_inputs(self) -> None:
        cases = (
            ({"device": "mock-device"}, "physical"),
            ({"device": "00008110-001234567890ABCD "}, "physical"),
            ({"app": self.root / "BleHubBlengHost"}, "frozen device .app"),
            ({"app": self.root / "missing.app"}, "frozen device .app"),
            ({"app_tree_sha256": "A" * 64}, "App tree SHA256"),
            ({"app_id": "no-dot"}, "bundle identifier"),
            ({"executable": "../host"}, "executable"),
            ({"team": "fwvyq287w5"}, "signing team"),
            ({"identity": "Apple Development"}, "Apple Development identity"),
            ({"xctestrun": self.root / "absent.xctestrun"}, "xctestrun"),
            ({"xctestrun": self.root}, "xctestrun"),
            ({"cwd": Path("relative")}, "absolute"),
        )
        for changes, message in cases:
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, message),
            ):
                producer.validate_ios_scan_inputs(**self.values(**changes))
        with (
            self.subTest(changes={"deadline": "past"}),
            self.assertRaisesRegex(TimeoutError, "deadline exceeded"),
        ):
            producer.validate_ios_scan_inputs(
                **self.values(deadline=time.monotonic() - 1)
            )

    def test_authorization_is_explicit_and_bounded(self) -> None:
        xctestrun = self.root / "host.xctestrun"
        xctestrun.write_bytes(b"<plist/>")
        base = {
            "device": self.device,
            "app": self.app,
            "app_tree_sha256": self.sha,
            "app_id": self.bundle,
            "executable": self.executable,
            "team": self.team,
            "identity": self.identity,
            "xctestrun": xctestrun,
            "fixture_contract": self.root / "contract.json",
            "fixture_profile": self.root / "profile.json",
            "fixture_port": "test-port",
            "fixture_resource_identity": "jlink:123456",
            "output_root": self.output,
            "timeout": 600,
            "cleanup_seconds": 30,
            "authorize_install": True,
            "authorize_fixture_start": True,
            "release_retained_app": False,
            "cwd": self.root,
        }
        # The scan scene runs in-process, so it needs no WDA build authorization at all.
        for name in ("authorize_install", "authorize_fixture_start"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "authorization required"),
            ):
                producer.run_ios_scan(**{**base, name: False})
        for name, value, message in (
            ("timeout", producer.MAX_TOTAL_SECONDS + 1, "at most"),
            ("timeout", float("nan"), "at most"),
            ("cleanup_seconds", producer.MAX_CLEANUP_SECONDS + 1, "at most"),
        ):
            with (
                self.subTest(name=name, value=value),
                self.assertRaisesRegex(ValueError, message),
            ):
                producer.run_ios_scan(**{**base, name: value})

    def test_freeze_copies_the_exact_reviewed_tree(self) -> None:
        frozen = self.root / "frozen" / "candidate.app"
        frozen.parent.mkdir()
        producer._freeze_app(self.app, frozen, self.sha, time.monotonic() + 30)
        self.assertEqual(fingerprint(frozen), self.sha)
        self.assertEqual(
            (frozen / self.executable).read_bytes(), b"synthetic executable"
        )

    def test_freeze_refuses_a_tree_that_is_not_the_reviewed_one(self) -> None:
        frozen = self.root / "frozen" / "candidate.app"
        frozen.parent.mkdir()
        with self.assertRaisesRegex(RuntimeError, "differs from the reviewed"):
            producer._freeze_app(self.app, frozen, "0" * 64, time.monotonic() + 30)


class IosEvidenceFactTests(unittest.TestCase):
    run_id = "12345678-1234-4234-8234-123456789abc"
    device = "00008110-001234567890ABCD"
    # What CoreDevice reports for that device; deliberately a different namespace.
    core_device = "130256F7-7150-5A81-A906-6806AAFBFADA"
    app_id = "FWVYQ287W5.bleng-rn-host"
    scope = evidence.SCAN_SCENE.scope("ios")
    inner_scope = evidence.SCAN_SCENE.inner_scope
    service = "12345678-1234-4234-8234-123456789abc"

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.attempt = self.root / "attempt"
        self.attempt.mkdir()
        self.sha = "a" * 64

    def result(self, **changes: object) -> dict:
        value = {
            "schema_version": 1,
            "scope": self.scope,
            "run_id": self.run_id,
            "platform": "ios",
            "device": self.device,
            "app_id": self.app_id,
            "app_tree_sha256": self.sha,
            "status": "passed",
            "host_gate_status": "pending",
            "rf": evidence.SCAN_SCENE.rf,
            "scenario_status": "passed",
            "session_status": "passed",
            "phone_status": "passed",
            "fixture_status": "passed",
            "fixture_close": None,
            "primary_error": None,
            "cleanup_errors": [],
            "phone_cleanup_errors": [],
            "cleanup_order": [],
        }
        value.update(changes)
        return value

    def request(self, result: dict, **changes: object) -> dict:
        value = {
            "schema_version": 1,
            "scope": self.scope,
            "run_id": self.run_id,
            "device": self.device,
            "app_id": self.app_id,
            "executable": "BleHubBlengHost",
            "app_tree_sha256": result["app_tree_sha256"],
            "team": "FWVYQ287W5",
            "identity": "Apple Development: ZiJian LIU (6J7DUSCP6X)",
            "driver_kind": evidence.IOS_NATIVE_DRIVER_KIND,
            "xctestrun_sha256": "e" * 64,
            "fixture_resource_identity": "jlink:123456",
            "fixture_port": "test-port",
            "service_uuid": self.service,
            "authorize_install": True,
            "authorize_fixture_start": True,
            "timeout": 900,
            "cleanup_seconds": 30,
            "fixture_pid": os.getpid(),
            "fixture_pgid": os.getpgid(0),
        }
        value.update(changes)
        return value

    # ------------------------------------------------------------ result shape

    def test_ios_result_shape_is_accepted_and_android_digest_is_not(self) -> None:
        evidence._result(self.result())
        with self.assertRaisesRegex(evidence.ScanEvidenceError, "result fields"):
            evidence._result(self.result(apk_sha256="a" * 64))
        with self.assertRaisesRegex(evidence.ScanEvidenceError, "App tree hash"):
            evidence._result(self.result(app_tree_sha256="not-a-hash"))
        with self.assertRaisesRegex(evidence.ScanEvidenceError, "result fields"):
            evidence._result(self.result(platform="windows"))
        with self.assertRaisesRegex(evidence.ScanEvidenceError, "inconsistent"):
            evidence._result(self.result(phone_status="failed"))

    # ----------------------------------------------------------- ios request

    def test_ios_request_accepts_the_producer_shape(self) -> None:
        result = self.result()
        evidence._request(self.request(result), result)

    def test_ios_request_rejects_unproved_or_aliased_facts(self) -> None:
        result = self.result()
        cases = (
            ({"app_tree_sha256": "b" * 64}, "identity conflict"),
            ({"team": "fwvyq287w5"}, "signing team"),
            ({"identity": "Apple Development"}, "signing identity"),
            ({"xctestrun_sha256": "not-a-hash"}, "xctestrun hash"),
            ({"driver_kind": "appium"}, "must name the in-process driver"),
            ({"timeout": producer.MAX_TOTAL_SECONDS + 1}, "invalid scan timeout"),
            ({"cleanup_seconds": 0}, "invalid scan cleanup budget"),
            ({"fixture_resource_identity": "dev/tty0"}, "fixture resource identity"),
            ({"service_uuid": "not-a-uuid"}, "service UUID"),
            ({"expected_helper_sha256": {}}, "request fields"),
        )
        for changes, message in cases:
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(evidence.ScanEvidenceError, message),
            ):
                evidence._request(self.request(result, **changes), result)

    def candidate_docs(self, frozen: Path) -> dict[str, dict]:
        # CoreDevice answers with its own identifier, which is not the UDID a run names;
        # the receipts carry `--device <udid>` in their own argv, and that is what ties
        # them to this device. Fixtures must keep both namespaces realistic.
        return {
            "candidate/02-install.json": {
                "info": {
                    "commandType": "devicectl.device.install.app",
                    "arguments": [
                        "devicectl",
                        "device",
                        "install",
                        "app",
                        "--device",
                        self.device,
                    ],
                },
                "result": {
                    "deviceIdentifier": self.core_device,
                    "installedApplications": [
                        {
                            "bundleID": self.app_id,
                            "installationURL": (
                                "file:///private/var/containers/Bundle/Application/"
                                "1111/BleHubBlengHost.app/"
                            ),
                        }
                    ],
                },
            },
            "candidate/04-launch.json": {
                "info": {
                    "commandType": "devicectl.device.process.launch",
                    "arguments": [
                        "devicectl",
                        "device",
                        "process",
                        "launch",
                        "--device",
                        self.device,
                        self.app_id,
                    ],
                },
                "result": {"deviceIdentifier": self.core_device},
            },
            "candidate/finalization/result.json": {
                "scope": "formal-ios-candidate-finalization",
                "run_id": self.run_id,
                "action": "retain",
                "released_retained_app": False,
                "identity": {
                    "device": self.device,
                    "run_id": self.run_id,
                    "bundle_id": self.app_id,
                    "pid": 4321,
                },
                "status": "passed",
                "error": None,
            },
            "candidate/ios-identity-challenge/01-processes-before.json": {},
        }

    def frozen_app(self) -> tuple[Path, str]:
        app = self.attempt / "candidate.app"
        app.mkdir()
        (app / "Info.plist").write_bytes(b"<plist/>")
        (app / "BleHubBlengHost").write_bytes(b"executable")
        return app, fingerprint(app)

    def test_ios_candidate_facts_bind_the_frozen_tree_and_official_receipts(
        self,
    ) -> None:
        frozen, digest = self.frozen_app()
        result = self.result(app_tree_sha256=digest)
        # The measured shape: CoreDevice answers with its own UUID, the run names the UDID.
        self.assertNotEqual(self.core_device, result["device"])
        evidence._ios_candidate_facts(
            self.attempt,
            self.candidate_docs(frozen),
            result,
            time.monotonic() + 30,
        )
        # A different reviewed hash is not the tree on disk.
        with self.assertRaisesRegex(evidence.ScanEvidenceError, "tree hash mismatch"):
            evidence._ios_candidate_facts(
                self.attempt,
                self.candidate_docs(frozen),
                self.result(app_tree_sha256="b" * 64),
                time.monotonic() + 30,
            )

    def test_ios_candidate_facts_reject_foreign_or_unretained_evidence(self) -> None:
        frozen, digest = self.frozen_app()
        result = self.result(app_tree_sha256=digest)
        cases = (
            (
                "candidate/02-install.json",
                {
                    "info": {
                        "commandType": "devicectl.device.install.app",
                        "arguments": [
                            "devicectl",
                            "device",
                            "install",
                            "app",
                            "--device",
                            "00008110-FFFFFFFFFFFFFFFF",
                        ],
                    }
                },
                "names another device",
            ),
            (
                "candidate/04-launch.json",
                {
                    "result": {
                        "deviceIdentifier": "9E1D2C3B-0000-1111-2222-333344445555"
                    }
                },
                "disagree on the device",
            ),
            (
                "candidate/04-launch.json",
                {
                    "info": {
                        "commandType": "devicectl.device.process.launch",
                        "arguments": [
                            "devicectl",
                            "device",
                            "process",
                            "launch",
                            "--device",
                            self.device,
                            "other.bundle",
                        ],
                    }
                },
                "not this App",
            ),
            (
                "candidate/finalization/result.json",
                {"action": "terminate", "released_retained_app": False},
                "retained App or an authorized release",
            ),
            (
                "candidate/finalization/result.json",
                {"status": "failed"},
                "not this run's installed App",
            ),
        )
        for name, changes, message in cases:
            docs = self.candidate_docs(frozen)
            docs[name] = {**docs[name], **changes}
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(evidence.ScanEvidenceError, message),
            ):
                evidence._ios_candidate_facts(
                    self.attempt, docs, result, time.monotonic() + 30
                )

    def test_ios_candidate_success_requires_a_fresh_identity_challenge(self) -> None:
        frozen, digest = self.frozen_app()
        docs = self.candidate_docs(frozen)
        docs.pop("candidate/ios-identity-challenge/01-processes-before.json")
        with self.assertRaisesRegex(evidence.ScanEvidenceError, "identity challenge"):
            evidence._ios_candidate_facts(
                self.attempt,
                docs,
                self.result(app_tree_sha256=digest),
                time.monotonic() + 30,
            )

    # ------------------------------------------------------------- ios phone

    def phone(self) -> dict:
        return {
            "schema_version": 1,
            "platform": "ios",
            "device": self.device,
            "run_id": self.run_id,
            "host_pid": os.getpid(),
            "status": "released",
            "scope": SCAN_OBSERVATION_SCOPE,
            "control_sha256": None,
            "restoration": "unverified",
            "error": None,
            "acquired_monotonic": 1.0,
            "released_monotonic": 2.0,
            "primary_error": None,
            "cleanup_errors": [],
        }

    def test_ios_phone_journal_must_be_a_released_observation_lease(self) -> None:
        evidence._phone_facts(
            self.attempt, {"phone.json": self.phone()}, [], self.result()
        )
        for changes, message in (
            ({"scope": "ios-adapter-startup-phone-owner"}, "identity/scope"),
            ({"device": "00008110-FFFFFFFFFFFFFFFF"}, "identity/scope"),
            ({"status": "held"}, "cannot support scan success"),
            ({"primary_error": "X: boom"}, "cannot support scan success"),
            ({"cleanup_errors": ["x"]}, "cannot support scan success"),
            ({"error": "boom"}, "invalid iOS phone journal status"),
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(evidence.ScanEvidenceError, message),
            ):
                evidence._phone_facts(
                    self.attempt,
                    {"phone.json": {**self.phone(), **changes}},
                    [],
                    self.result(),
                )
        quarantined = {**self.phone(), "status": "quarantined", "error": "boom"}
        evidence._phone_facts(
            self.attempt, {"phone.json": quarantined}, [], self.result(status="failed")
        )


if __name__ == "__main__":
    unittest.main()
