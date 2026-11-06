"""Pure complete-bundle and mutation regressions; no device or RF execution."""

from __future__ import annotations

import hashlib
import json
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.react_native import ios_adapter_startup_evidence as owner
from tools.react_native import rf_evidence_bundle as api
from tools.tests.react_native.support.evidence_ios_adapter_startup import StartupBundle
from tools.tests.react_native.support.ios_adapter_startup_samples import DEVICE, put


class IOSAdapterStartupEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fixture(self) -> StartupBundle:
        return StartupBundle(self.root)

    def verify(self, fixture: StartupBundle) -> None:
        api.verify_bundle(fixture.attempt / api.MANIFEST)

    def test_complete_replays_actual_semantic_owners_and_relocates_offline(
        self,
    ) -> None:
        fixture = self.fixture()
        fixture.finish()
        self.verify(fixture)
        self.assertEqual(fixture.bundle.document["status"], "passed")
        self.assertEqual(
            fixture.bundle.document["flow_index"]["size"], fixture.log.stat().st_size
        )
        self.assertEqual(fixture.runtime["counts"]["records"], 39)
        moved = self.root / "relocated"
        shutil.move(fixture.attempt, moved)
        api.verify_bundle(moved / api.MANIFEST)

    def test_installed_container_uuid_may_differ_from_runtime_executable(self) -> None:
        runtime_executable = (
            "file:///private/var/containers/Bundle/Application/"
            "11111111-1111-4111-8111-111111111111/"
            "BleHubNitroHost.app/BleHubNitroHost"
        )
        fixture = StartupBundle(self.root, executable=runtime_executable)
        fixture.finish()
        install = fixture.load("device_install")
        installed_url = install["result"]["installedApplications"][0]["installationURL"]
        install["result"]["installedApplications"][0]["installationURL"] = (
            installed_url.replace(
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
            )
        )
        fixture.write("device_install", install)
        fixture.reindex()
        self.verify(fixture)

    def test_complete_bundle_accepts_distinct_valid_identity_axes_not_one_sample(
        self,
    ) -> None:
        for pid, boot, nonce, service in (
            (
                17,
                "22222222-2222-4222-8222-222222222222",
                "0-0-0",
                "fd770000-0000-4000-8000-000000000000",
            ),
            (
                2_147_483_647,
                "33333333-3333-4333-8333-333333333333",
                "a1-b2-c3",
                "fd880000-0000-4000-8000-000000000000",
            ),
        ):
            with (
                self.subTest(pid=pid, service=service),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = StartupBundle(
                    Path(directory),
                    native_pid=pid,
                    boot_id=boot,
                    nonce=nonce,
                    service_uuid=service,
                )
                fixture.finish()
                self.verify(fixture)
                self.assertEqual(fixture.runtime["pid"], pid)
                self.assertEqual(fixture.runtime["boot_id"], boot)
                self.assertEqual(fixture.runtime["runtime_nonce"], nonce)
                self.assertEqual(fixture.runtime["service_uuid"], service)
                self.assertNotEqual(boot, service)
                self.assertEqual(fixture.load("phone_lease")["device"], DEVICE)

    def test_scenario_does_not_relax_android_restrictions_or_allow_active_loss(
        self,
    ) -> None:
        for platform, scenario, mode in (
            ("android", owner.SCENARIO, None),
            ("ios", "adapter-transition", None),
            ("ios", "permission-transition", None),
            ("ios", owner.SCENARIO, "active-loss"),
            ("ios", owner.SCENARIO, "startup-off"),
        ):
            with (
                self.subTest(platform=platform, scenario=scenario, mode=mode),
                self.assertRaises(api.EvidenceError),
            ):
                api.EvidenceBundle(
                    self.root / "invalid",
                    platform=platform,
                    device=DEVICE,
                    project_root=self.root,
                    scenario=scenario,
                    adapter_mode=mode,
                )

    def test_every_required_omission_fails_pass_but_retains_failed_bundle(self) -> None:
        for role in (*owner.names(), "actual_attachment"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary:
                fixture = StartupBundle(Path(temporary))
                (
                    fixture.attachment
                    if role == "actual_attachment"
                    else fixture.path(role)
                ).unlink()
                with self.assertRaises(api.EvidenceError):
                    fixture.finish()
                self.assertEqual(fixture.bundle.document["status"], "failed")
                self.verify(fixture)

    def test_semantic_mutations_rejected_even_with_refreshed_inventory(self) -> None:
        cases = (
            ("ledger", ("runtime", "counts", "records"), 999),
            ("ledger", ("control", "identity", "nonce"), "foreign-one-two"),
            ("ledger", ("physical", "connections"), 20),
            ("fixture_receipt", ("fixture_pid",), 457),
            (
                "fixture_receipt",
                ("session_id",),
                "22222222-2222-4222-8222-222222222222",
            ),
            ("readiness", ("off_journal_sha256",), "00" * 32),
            ("readiness", ("runtime_nonce",), "foreign-one-two"),
            ("physical", ("off", "journal_sha256"), "00" * 32),
            ("physical", ("events", 0, "peer"), ["aabbccddeeff", 1]),
            ("physical", ("cleanup_errors",), ["cleanup failed"]),
            ("runtime_capture", ("status",), "passed"),
            ("runtime_capture", ("ready_delivered",), False),
            ("runtime_capture", ("primary_error",), "failed"),
            ("runtime_capture", ("cleanup_errors",), ["failed"]),
            ("runtime_capture", ("test_run_sha256",), "00" * 32),
            ("runtime_capture", ("window", "identity", "nonce"), "foreign-one-two"),
            ("startup_ready_payload", ("result", "process", "processIdentifier"), 999),
            (
                "ready_before_processes",
                ("result", "runningProcesses", 0, "executable"),
                "file:///other.app/Other",
            ),
            (
                "startup_ready_after_processes",
                ("result", "deviceIdentifier"),
                "foreign",
            ),
            ("journal_copy", ("result", "deviceIdentifier"), "foreign"),
            ("off_journal_copy", ("info", "outcome"), "failed"),
            ("control", ("observations", 10, "switch_value"), "0"),
            ("control", ("observations",), []),
            ("control", ("same_session",), False),
            ("summary", ("skippedTests",), 1),
            ("summary", ("failedTests",), 1),
            ("summary", ("testFailures",), [{"message": "failed"}]),
            (
                "summary",
                ("devicesAndConfigurations", 0, "device", "deviceId"),
                "foreign",
            ),
            ("attachments", (0, "testIdentifier"), "Foreign/test()"),
            ("attachments", (0, "attachments", 0, "deviceId"), "foreign"),
            ("attachments", (0, "attachments", 0, "isAssociatedWithFailure"), True),
            (
                "attachments",
                (0, "attachments", 0, "suggestedHumanReadableName"),
                "foreign_0_AAAA.json",
            ),
            (
                "attachments",
                (0, "attachments", 0, "exportedFileName"),
                "../outside.json",
            ),
            ("phone_lease", ("status",), "quarantined"),
            ("phone_lease", ("restoration",), "unknown"),
            ("phone_lease", ("control_sha256",), "00" * 32),
            ("phone_lease", ("host_pid",), 457),
            ("phone_lease", ("error",), "restoration failed"),
            ("phone_lease", ("released_monotonic",), 0),
        )
        for role, keys, replacement in cases:
            with (
                self.subTest(role=role, keys=keys),
                tempfile.TemporaryDirectory() as temporary,
            ):
                fixture = StartupBundle(Path(temporary))
                fixture.finish()
                value = fixture.load(role)
                target = value
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = replacement
                fixture.write(role, value)
                fixture.reindex()
                with self.assertRaises((api.EvidenceError, ValueError)):
                    self.verify(fixture)

    def test_source_journal_pid_nonce_url_and_off_prefix_not_ledger_status(
        self,
    ) -> None:
        for prefix, key, value in (
            ("native:bootstrap:", "pid", 999),
            ("native:ready:", "nonce", "foreign-one-two"),
            ("native:signal:", "url", "blehub-rn-spike://adapter/foreign/on"),
        ):
            with (
                self.subTest(prefix=prefix),
                tempfile.TemporaryDirectory() as temporary,
            ):
                fixture = StartupBundle(Path(temporary))
                fixture.finish()
                fixture.edit_journal(prefix, key, value)
                fixture.reindex()
                with self.assertRaises(api.EvidenceError):
                    self.verify(fixture)
        fixture = self.fixture()
        fixture.finish()
        value = fixture.load("off_journal")
        value["records"].append(fixture.journal["records"][8])
        fixture.write("off_journal", value)
        fixture.reindex()
        with self.assertRaises(api.EvidenceError):
            self.verify(fixture)

    def test_raw_window_duplicates_truncation_foreign_projection_and_app_console(
        self,
    ) -> None:
        for kind in (
            "duplicate",
            "same-line-duplicate",
            "truncated",
            "foreign",
            "app-console",
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                fixture = StartupBundle(Path(temporary))
                fixture.finish()
                marker = fixture.log.read_bytes()
                raw = {
                    "duplicate": marker + marker,
                    "same-line-duplicate": marker.rstrip() + marker,
                    "truncated": marker.rstrip(),
                    "foreign": marker.replace(b"boot-123-abc", b"foreign-one-two"),
                    "app-console": fixture.runtime["runtime_sha256"].encode(),
                }[kind]
                fixture.write("raw_runtime", raw)
                fixture.reindex()
                with self.assertRaises(api.EvidenceError):
                    self.verify(fixture)

    def test_control_raw_bytes_binding_not_json_equivalence(self) -> None:
        fixture = self.fixture()
        fixture.finish()
        rewritten = json.dumps(fixture.load("control"), separators=(",", ":")).encode()
        fixture.write("control", rewritten)
        phone = fixture.load("phone_lease")
        phone["control_sha256"] = hashlib.sha256(rewritten).hexdigest()
        fixture.write("phone_lease", phone)
        fixture.reindex()
        with self.assertRaisesRegex(api.EvidenceError, "attachment bytes"):
            self.verify(fixture)

    def test_staged_test_run_checks_selection_env_app_and_dependency_even_if_rehashed(
        self,
    ) -> None:
        for key, value in (
            ("OnlyTestIdentifiers", ["Other/test"]),
            ("SkipTestIdentifiers", [owner.TEST_METHOD]),
            ("UITargetAppPath", "/mutable/BleHubNitroHost.app"),
            ("DependentProductPaths", ["/mutable/BleHubNitroHost.app"]),
            ("TestingEnvironmentVariables", {"BLEHUB_IOS_RADIO_CONTROL_GATE": "1"}),
            ("UITargetAppCommandLineArguments", ["--console"]),
            ("EnvironmentVariables", {"BLEHUB_IOS_ADAPTER_STARTUP_GATE": "1"}),
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                fixture = StartupBundle(Path(temporary))
                fixture.finish()
                staged = plistlib.loads(fixture.path("test_run").read_bytes())
                staged[owner.TEST_TARGET][key] = value
                raw = plistlib.dumps(staged)
                fixture.write("test_run", raw)
                capture = fixture.load("runtime_capture")
                capture["test_run_sha256"] = hashlib.sha256(raw).hexdigest()
                fixture.write("runtime_capture", capture)
                fixture.reindex()
                with self.assertRaises(api.EvidenceError):
                    self.verify(fixture)

    def test_official_argument_substitution_replacement_and_copy_route(self) -> None:
        for role, option, value in (
            ("device_install", None, "/mutable/BleHubNitroHost.app"),
            (
                "startup_ready_payload",
                "--payload-url",
                "blehub-rn-spike://adapter/foreign/ready",
            ),
            ("journal_copy", "--source", "Library/Caches/other.json"),
            ("off_journal_copy", "--destination", "/other/off.json"),
            ("startup_ready_payload", "--console", None),
            ("startup_ready_payload", "--terminate-existing", None),
        ):
            with (
                self.subTest(role=role, option=option),
                tempfile.TemporaryDirectory() as temporary,
            ):
                fixture = StartupBundle(Path(temporary))
                fixture.finish()
                receipt = fixture.load(role)
                args = receipt["info"]["arguments"]
                if option is None:
                    args[-1] = value
                elif value is None:
                    args.insert(4, option)
                else:
                    args[args.index(option) + 1] = value
                fixture.write(role, receipt)
                fixture.reindex()
                with self.assertRaises(api.EvidenceError):
                    self.verify(fixture)

    def test_quiet_cannot_be_rehashed_into_a_live_control_command(self) -> None:
        fixture = self.fixture()
        fixture.finish()
        self.verify(fixture)
        capture = fixture.load("runtime_capture")
        self.assertNotIn("-quiet", capture["command"])
        capture["command"].insert(2, "-quiet")
        fixture.write("runtime_capture", capture)
        fixture.reindex()
        with self.assertRaisesRegex(api.EvidenceError, "single XCTest command"):
            self.verify(fixture)

    def test_repeat_options_cannot_be_rehashed_into_a_single_authorized_window(
        self,
    ) -> None:
        for options in (
            ["-test-iterations", "1"],
            ["-test-iterations", "2"],
            ["-retry-tests-on-failure"],
            ["-run-tests-until-failure"],
        ):
            with (
                self.subTest(options=options),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = StartupBundle(Path(directory))
                fixture.finish()
                capture = fixture.load("runtime_capture")
                capture["command"][-1:-1] = options
                fixture.write("runtime_capture", capture)
                fixture.reindex()
                with self.assertRaisesRegex(api.EvidenceError, "single XCTest command"):
                    self.verify(fixture)

    def test_artifact_inventory_drift_and_rehashed_candidate_drift(self) -> None:
        fixture = self.fixture()
        fixture.finish()
        put(fixture.frozen / "main.jsbundle", b"changed candidate")
        with self.assertRaises(api.EvidenceError):
            self.verify(fixture)
        fixture.reindex()
        with self.assertRaises(api.EvidenceError):
            self.verify(fixture)

    def test_failed_before_install_preserves_partial_bundle(self) -> None:
        bundle = api.EvidenceBundle(
            self.root / "early",
            platform="ios",
            device=DEVICE,
            project_root=self.root,
            scenario=owner.SCENARIO,
        )
        bundle.finish(
            completed=False,
            primary_error=RuntimeError("before install"),
            cleanup_errors=["quarantined"],
            runtime_log=self.root / "early" / owner.LOG,
        )
        api.verify_bundle(self.root / "early" / api.MANIFEST)

    def test_quarantined_partial_attempt_preserves_primary_and_cleanup(self) -> None:
        fixture = self.fixture()
        fixture.path("control").unlink()
        fixture.write(
            "phone_lease", {"status": "quarantined", "error": "missing final ON"}
        )
        fixture.write("window_log", b"partial window")
        fixture.finish(
            completed=False, error=RuntimeError("UI failed"), cleanup=["quarantined"]
        )
        self.verify(fixture)
        self.assertEqual(
            fixture.bundle.document["primary_error"]["message"], "UI failed"
        )
        self.assertEqual(fixture.bundle.document["cleanup_errors"], ["quarantined"])


if __name__ == "__main__":
    unittest.main()
