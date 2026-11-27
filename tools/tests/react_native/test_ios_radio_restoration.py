"""Hardware-free inspection export validation; no device or lease operations."""

from __future__ import annotations

import hashlib
import json
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.react_native import ios_radio_restoration as collector
from tools.react_native.evidence_io import EvidenceError

DEVICE = "00008110-000C61160C39401E"
OTHER_DEVICE = "00008110-000C61160C39401F"
NOW = 1_792_466_950.0
OWNER = "a" * 64
JOURNAL = "b" * 64
TREE = """Element subtree:
  Application, 0x1, label: '设置'
    Switch, 0x2, identifier: 'BLUETOOTH', label: '蓝牙', value: 1
      Button, 0x3, identifier: 'BLUETOOTH', label: '蓝牙'
      Switch, 0x4, value: 1
    ActivityIndicator, 0x5, value: 1
"""


class IosRadioRestorationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.counter = 0
        self.prepare()
        sdk = patch.object(collector.subprocess, "run", side_effect=self.export)
        self.sdk = sdk.start()
        self.addCleanup(sdk.stop)
        clock = patch.object(collector.time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def prepare(self) -> None:
        self.counter += 1
        root = self.root / str(self.counter)
        root.mkdir()
        self.input_path = root / "input.json"
        self.staged = root / "inspection.xctestrun"
        self.result = root / "inspection.xcresult"
        self.result.mkdir()
        (self.result / "Info.plist").write_bytes(b"original result")
        self.output = root / "evidence"
        self.output.mkdir()
        self.host = root / "Debug-iphoneos/UI-Runner.app"
        self.app = root / "Debug-iphoneos/BleHubNitroHost.app"
        self.test_binary = self.host / "PlugIns/UITests.xctest/UITests"
        self.test_binary.parent.mkdir(parents=True)
        self.test_binary.write_bytes(b"original UI test binary")
        self.app.mkdir()
        (self.app / "BleHubNitroHost").write_bytes(b"original host App")
        self.target = {
            "IsUITestBundle": True,
            "TestHostPath": "__TESTROOT__/Debug-iphoneos/UI-Runner.app",
            "UITargetAppPath": "__TESTROOT__/Debug-iphoneos/BleHubNitroHost.app",
            "OnlyTestIdentifiers": [collector.METHOD],
            "SkipTestIdentifiers": [],
            "CommandLineArguments": [],
            "UITargetAppCommandLineArguments": [],
            "EnvironmentVariables": {
                "BLEHUB_IOS_RADIO_CONTROL_GATE": "1",
                "BLEHUB_IOS_RADIO_CONTROL_INSPECT_ONLY": "1",
                "TERM": "dumb",
            },
            "TestingEnvironmentVariables": {"XCODE_SCHEME_NAME": "host"},
        }
        self.staging = {
            collector.TARGET: self.target,
            "__xctestrun_metadata__": {"FormatVersion": 1},
        }
        self.receipt = {
            "staged": str(self.staged),
            "result": str(self.result),
            "owner_sha256": OWNER,
            "original_journal_sha256": JOURNAL,
            "command": [
                "pixi",
                "run",
                "--frozen",
                "--no-install",
                "-e",
                "apple-ci",
                "xcodebuild",
                "-quiet",
                "-xctestrun",
                str(self.staged),
                "-destination",
                "id=" + DEVICE,
                "-resultBundlePath",
                str(self.result),
                "-parallel-testing-enabled",
                "NO",
                "-collect-test-diagnostics",
                "never",
                "-only-testing:" + collector.METHOD,
                "test-without-building",
            ],
        }
        counts = {
            "passedTests": 1,
            "failedTests": 0,
            "skippedTests": 0,
            "expectedFailures": 0,
        }
        self.summary = {
            **counts,
            "totalTestCount": 1,
            "result": "Passed",
            "testFailures": [],
            "startTime": NOW - 20,
            "finishTime": NOW - 5,
            "devicesAndConfigurations": [
                {
                    **counts,
                    "device": {"deviceId": DEVICE, "platform": "iOS"},
                    "testPlanConfiguration": {
                        "configurationId": "1",
                        "configurationName": "Test Scheme Action",
                    },
                }
            ],
        }
        self.method = {
            "nodeType": "Test Case",
            "name": "testBluetoothSettingsOffAndRestore()",
            "nodeIdentifier": collector.METHOD + "()",
            "result": "Passed",
        }
        self.tests = {
            "devices": [{"deviceId": DEVICE}],
            "testNodes": [
                {
                    "nodeType": "UI test bundle",
                    "name": collector.TARGET,
                    "result": "Passed",
                    "children": [self.method],
                }
            ],
        }
        self.attachments = {}
        self.original_exports: dict[str, bytes] = {}
        items = []
        for prefix, name, raw in (
            (collector.NOTE_PREFIX, "note.txt", collector.NOTE.encode()),
            (collector.TREE_PREFIX, "tree.txt", TREE.encode()),
            ("screen", "screen.png", b"original image"),
        ):
            items.append(
                {
                    "deviceId": DEVICE,
                    "exportedFileName": name,
                    "suggestedHumanReadableName": prefix + "_0_ABCD-1234.txt",
                    "isAssociatedWithFailure": False,
                }
            )
            self.attachments[name] = raw
        self.manifest = [
            {"testIdentifier": collector.METHOD + "()", "attachments": items}
        ]
        self.effect = lambda operation: None
        self.sync()

    def sync(self) -> None:
        raw = plistlib.dumps(self.staging)
        self.staged.write_bytes(raw)
        self.receipt["staged_sha256"] = hashlib.sha256(raw).hexdigest()
        self.input_path.write_text(json.dumps(self.receipt), encoding="utf-8")

    def export(self, command: list[str], **kwargs) -> None:
        self.assertEqual(command[:2], ["xcrun", "xcresulttool"])
        self.assertEqual(command[command.index("--path") + 1], str(self.result))
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 30)
        self.assertIs(kwargs["check"], True)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertNotIn("input", kwargs)
        self.assertNotIn("shell", kwargs)
        if command[2:4] == ["get", "test-results"]:
            operation = command[4]
            self.assertIn(operation, ("summary", "tests"))
            self.assertEqual(command[-1], "--compact")
            kwargs["stdout"].write(
                self.original_exports.get(
                    operation, json.dumps(getattr(self, operation)).encode()
                )
            )
        else:
            self.assertEqual(command[2:4], ["export", "attachments"])
            operation = "attachments"
            directory = Path(command[command.index("--output-path") + 1])
            self.assertEqual(directory, self.output / "attachments")
            directory.mkdir()
            (directory / "manifest.json").write_text(
                json.dumps(self.manifest), encoding="utf-8"
            )
            for name, raw in self.attachments.items():
                (directory / name).write_bytes(raw)
            kwargs["stdout"].write(b"full export output\n")
        kwargs["stderr"].write(b"full SDK diagnostics\n")
        self.effect(operation)

    def set_command_option(self, option: str, value: str) -> None:
        command = self.receipt["command"]
        command[command.index(option) + 1] = value

    def collect(self, **overrides) -> dict:
        arguments = {"device": DEVICE, "owner_sha256": OWNER, "journal_sha256": JOURNAL}
        arguments.update(overrides)
        return collector.collect_inspection(self.input_path, self.output, **arguments)

    def reject(self) -> None:
        with self.assertRaises((EvidenceError, ValueError, OSError)):
            self.collect()

    def test_read_only_export_binds_original_bytes_and_has_no_rf_or_release_verdict(
        self,
    ) -> None:
        source = {
            path: path.read_bytes()
            for path in (self.input_path, self.staged, self.result / "Info.plist")
        }
        result = self.collect(device=DEVICE.lower())
        self.assertEqual(result["scope"], "ios-radio-restoration-inspection-only")
        self.assertEqual(result["device"], DEVICE)
        self.assertEqual(result["inspected_switch_value"], "1")
        self.assertEqual(result["observed_start_time"], NOW - 20)
        self.assertEqual(result["observed_finish_time"], NOW - 5)
        for role, path in (("TestHostPath", self.host), ("UITargetAppPath", self.app)):
            artifact = result["candidate_artifacts"][role]
            self.assertEqual(artifact["path"], str(path))
            self.assertEqual(
                artifact["sha256"],
                hashlib.sha256(
                    json.dumps(
                        artifact["files"], sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
            )
            for row in artifact["files"]:
                self.assertEqual(
                    row["sha256"],
                    hashlib.sha256((path / row["path"]).read_bytes()).hexdigest(),
                )
        for key, path in (
            ("input_sha256", self.input_path),
            ("staged_sha256", self.staged),
        ):
            self.assertEqual(result[key], hashlib.sha256(source[path]).hexdigest())
        self.assertEqual(
            result["note_sha256"],
            hashlib.sha256(self.attachments["note.txt"]).hexdigest(),
        )
        self.assertEqual(
            result["tree_sha256"],
            hashlib.sha256(self.attachments["tree.txt"]).hexdigest(),
        )
        for path, raw in source.items():
            self.assertEqual(path.read_bytes(), raw)
        for item in result["files"]:
            self.assertEqual(
                hashlib.sha256((self.output / item["path"]).read_bytes()).hexdigest(),
                item["sha256"],
            )
        self.assertEqual(self.sdk.call_count, 3)
        self.assertNotIn("status", result)
        self.assertNotIn("restored", result)
        self.assertTrue((self.output / "attachments-export.stdout.log").read_bytes())
        self.assertTrue((self.output / "summary.log").read_bytes())

    def test_original_inspection_output_replays_independently_of_python_constants(
        self,
    ) -> None:
        source = Path(__file__).parent / "support/ios_radio_inspection"
        provenance = json.loads((source / "provenance.json").read_bytes())
        for name, digest in provenance["sha256"].items():
            self.assertEqual(
                hashlib.sha256((source / name).read_bytes()).hexdigest(), digest
            )
        self.staging = {provenance["target"]: self.target}
        self.target.update(provenance["staging_contract"])
        command = self.receipt["command"]
        selected = next(
            i for i, value in enumerate(command) if value.startswith("-only-testing:")
        )
        command[selected] = provenance["only_testing_argument"]
        self.original_exports = {
            name: (source / (name + ".json")).read_bytes()
            for name in ("summary", "tests")
        }
        self.summary = json.loads(self.original_exports["summary"])
        self.tests = json.loads(self.original_exports["tests"])
        self.manifest = json.loads((source / "manifest.json").read_bytes())
        self.attachments = {
            exported: (source / name).read_bytes()
            for name, exported in provenance["original_exported_files"].items()
        }
        device = self.summary["devicesAndConfigurations"][0]["device"]["deviceId"]
        self.set_command_option("-destination", "id=" + device)
        self.sync()
        with patch.object(
            collector.time, "time", return_value=self.summary["finishTime"] + 1
        ):
            proof = self.collect(device=device)
            collector.verify_inspection(json.loads(json.dumps(proof)), self.output)
        self.assertEqual(
            proof["note_sha256"], provenance["sha256"]["inspection-note.txt"]
        )
        self.assertEqual(proof["tree_sha256"], provenance["sha256"]["toggle-tree.txt"])
        self.assertEqual(proof["summary_sha256"], provenance["sha256"]["summary.json"])
        self.assertEqual(proof["tests_sha256"], provenance["sha256"]["tests.json"])
        self.assertEqual(self.sdk.call_count, 3)
        for name, raw in self.attachments.items():
            self.assertEqual((self.output / "attachments" / name).read_bytes(), raw)

    def test_quarantine_hash_corruption_and_replay_command_rejected_before_sdk(
        self,
    ) -> None:
        for mutation in (
            lambda: self.receipt.update(owner_sha256="c" * 64),
            lambda: self.receipt.update(original_journal_sha256="c" * 64),
            lambda: self.receipt["command"].extend(["-retry-tests-on-failure"]),
            lambda: self.receipt["command"].extend(["-test-iterations", "2"]),
            lambda: self.receipt["command"].append("-only-testing:Other/test"),
            lambda: self.receipt["command"].append("-skip-testing:" + collector.TARGET),
            lambda: self.set_command_option("-destination", "id=" + OTHER_DEVICE),
            lambda: self.set_command_option(
                "-xctestrun", str(self.staged.with_name("other.xctestrun"))
            ),
            lambda: self.set_command_option(
                "-resultBundlePath", str(self.result.with_name("other.xcresult"))
            ),
        ):
            self.prepare()
            mutation()
            self.sync()
            with self.subTest(receipt=self.receipt):
                self.reject()
                self.assertEqual(list(self.output.iterdir()), [])
        self.sdk.assert_not_called()

    def test_xctestrun_method_opt_in_environment_and_retry_boundaries(self) -> None:
        for mutation in (
            lambda: self.target.pop("IsUITestBundle"),
            lambda: self.target.update(IsUITestBundle=False),
            lambda: self.target.update(IsUITestBundle=1),
            lambda: self.target.update(IsUITestBundle="true"),
            lambda: self.staging.update(OtherTests={}),
            lambda: self.target.update(
                OnlyTestIdentifiers=[collector.METHOD, "Other/test"]
            ),
            lambda: self.target.update(SkipTestIdentifiers=[collector.METHOD]),
            lambda: self.target.update(CommandLineArguments=["--anything"]),
            lambda: self.target.update(UITargetAppCommandLineArguments=["--anything"]),
            lambda: self.target["EnvironmentVariables"].pop(
                "BLEHUB_IOS_RADIO_CONTROL_INSPECT_ONLY"
            ),
            lambda: self.target["EnvironmentVariables"].update(
                BLEHUB_IOS_RADIO_CONTROL_GATE="0"
            ),
            lambda: self.target["EnvironmentVariables"].update(BLEHUB_OTHER="1"),
            lambda: self.target["EnvironmentVariables"].update(PREFIX_BLEHUB_OTHER="1"),
            lambda: self.target["TestingEnvironmentVariables"].update(BLEHUB_OTHER="1"),
            lambda: self.target["TestingEnvironmentVariables"].update(
                PREFIX_BLEHUB_OTHER="1"
            ),
            lambda: self.target.update(TestRepetitionCount=2),
        ):
            self.prepare()
            mutation()
            self.sync()
            with self.subTest(staging=self.staging):
                self.reject()
        self.sdk.assert_not_called()
        self.prepare()
        self.staged.write_bytes(b"corrupt")
        self.reject()
        self.sdk.assert_not_called()

    def test_staging_nonstring_keys_fail_with_evidence_error(self) -> None:
        for select in (
            lambda: self.staging,
            lambda: self.target,
            lambda: self.target["EnvironmentVariables"],
            lambda: self.target["TestingEnvironmentVariables"],
            lambda: self.staging["__xctestrun_metadata__"],
        ):
            self.prepare()
            select()[1] = "invalid key"
            with (
                self.subTest(staging=self.staging),
                patch.object(collector.plistlib, "loads", return_value=self.staging),
                self.assertRaises(EvidenceError),
            ):
                self.collect()
        self.sdk.assert_not_called()

    def test_summary_device_counts_time_and_expected_failure_boundaries(self) -> None:
        mutations = [
            lambda: self.summary["devicesAndConfigurations"][0]["device"].update(
                deviceId=OTHER_DEVICE
            ),
            lambda: self.summary["devicesAndConfigurations"].append(
                self.summary["devicesAndConfigurations"][0]
            ),
            lambda: self.summary.update(expectedFailures=1),
            lambda: self.summary.update(passedTests=True),
            lambda: self.summary.update(totalTestCount=2),
            lambda: self.summary.update(testFailures=[{"message": "failure"}]),
            lambda: self.summary.update(startTime=NOW - 4000, finishTime=NOW - 3601),
            lambda: self.summary.update(finishTime=NOW + 6),
            lambda: self.summary.update(startTime=NOW, finishTime=NOW - 1),
            lambda: self.summary.update(startTime=NOW - 4000),
            lambda: self.summary.update(finishTime=float("nan")),
        ]
        for mutation in mutations:
            self.prepare()
            mutation()
            with self.subTest(summary=self.summary):
                self.reject()

    def test_tests_tree_requires_actual_selected_passed_method_not_system_failures(
        self,
    ) -> None:
        for mutation in (
            lambda: self.method.update(nodeIdentifier="Other/test()"),
            lambda: self.method.update(result="Failed"),
            lambda: self.method.update(name="System Failures"),
            lambda: self.method.pop("result"),
            lambda: self.tests["testNodes"][0]["children"].append(dict(self.method)),
            lambda: self.tests["testNodes"].clear(),
            lambda: self.tests["devices"][0].update(deviceId=OTHER_DEVICE),
        ):
            self.prepare()
            mutation()
            with self.subTest(tests=self.tests):
                self.reject()

    def test_manifest_method_device_path_and_duplicate_proof_boundaries(self) -> None:
        for mutation in (
            lambda: self.manifest[0].update(testIdentifier="Other/test()"),
            lambda: self.manifest.append(dict(self.manifest[0])),
            lambda: self.manifest[0]["attachments"][2].update(deviceId=OTHER_DEVICE),
            lambda: self.manifest[0]["attachments"][2].update(
                isAssociatedWithFailure=True
            ),
            lambda: self.manifest[0]["attachments"][0].update(
                exportedFileName="../note.txt"
            ),
            lambda: self.manifest[0]["attachments"][0].update(
                exportedFileName="/note.txt"
            ),
            lambda: self.manifest[0]["attachments"][0].update(
                exportedFileName="x" * 256
            ),
            lambda: self.manifest[0]["attachments"][2].update(
                exportedFileName="note.txt"
            ),
            lambda: self.manifest[0]["attachments"].append(
                dict(self.manifest[0]["attachments"][0], exportedFileName="screen.png")
            ),
            lambda: self.attachments.pop("note.txt"),
        ):
            self.prepare()
            mutation()
            with self.subTest(manifest=self.manifest):
                self.reject()

    def test_exact_note_and_both_targeted_switches_not_random_value_one(self) -> None:
        for note in (
            collector.NOTE.replace("switch_value=1", "switch_value=0"),
            collector.NOTE + "; RF passed",
            "switch_value=1",
            collector.NOTE + "\n\n",
        ):
            self.prepare()
            self.attachments["note.txt"] = note.encode()
            with self.subTest(note=note):
                self.reject()
        for tree in (
            TREE.replace("0x2, identifier", "0x2, value: 1, identifier").replace(
                "label: '蓝牙', value: 1", "label: '蓝牙', value: 0"
            ),
            TREE.replace("0x4, value: 1", "0x4, value: 0"),
            TREE.replace("Switch, 0x4", "Button, 0x4"),
            TREE.replace("identifier: 'BLUETOOTH'", "identifier: 'WIFI'"),
            TREE.replace("      Switch, 0x4", "    Switch, 0x4"),
            TREE + "    Switch, 0x6, identifier: 'BLUETOOTH', value: 1\n",
            "ActivityIndicator, 0x1, value: 1\n",
        ):
            self.prepare()
            self.attachments["tree.txt"] = tree.encode()
            with self.subTest(tree=tree):
                self.reject()
        self.prepare()
        self.attachments["note.txt"] += b"\n"
        self.assertEqual(self.collect()["inspected_switch_value"], "1")

    def test_candidate_paths_missing_symlinks_and_binary_drift_fail_closed(
        self,
    ) -> None:
        for role in ("TestHostPath", "UITargetAppPath"):
            for value in (
                None,
                "relative.app",
                "__TESTROOT__/../outside.app",
                "__TESTROOT__/missing.app",
                str(self.staged),
            ):
                self.prepare()
                self.target[role] = value
                if value is None:
                    self.target.pop(role)
                self.sync()
                with self.subTest(role=role, value=value):
                    self.reject()
        self.sdk.assert_not_called()
        for role in ("TestHostPath", "UITargetAppPath"):
            self.prepare()
            path = self.host if role == "TestHostPath" else self.app
            moved = path.with_name("actual.app")
            path.rename(moved)
            path.symlink_to(moved, target_is_directory=True)
            with self.subTest(symlink=role):
                self.reject()
        self.sdk.assert_not_called()
        for operation in ("summary", "tests", "attachments"):
            for candidate in ("test", "app"):
                self.prepare()

                def drift(
                    when: str, operation: str = operation, candidate: str = candidate
                ) -> None:
                    if when == operation:
                        path = (
                            self.test_binary
                            if candidate == "test"
                            else self.app / "BleHubNitroHost"
                        )
                        path.write_bytes(b"changed candidate")

                self.effect = drift
                calls = self.sdk.call_count
                with self.subTest(operation=operation, candidate=candidate):
                    self.reject()
                    self.assertEqual(
                        self.sdk.call_count - calls,
                        ("summary", "tests", "attachments").index(operation) + 1,
                    )
        self.prepare()
        self.target.update(TestHostPath=str(self.host), UITargetAppPath=str(self.app))
        self.sync()
        self.assertEqual(
            self.collect()["candidate_artifacts"]["TestHostPath"]["path"],
            str(self.host),
        )

    def test_sources_and_exported_proofs_cannot_drift_during_sdk_or_validation(
        self,
    ) -> None:
        for source in ("input", "staged", "result", "note"):
            self.prepare()

            def drift(operation: str, source: str = source) -> None:
                if operation != "attachments":
                    return
                path = {
                    "input": self.input_path,
                    "staged": self.staged,
                    "result": self.result / "Info.plist",
                    "note": self.output / "attachments/note.txt",
                }[source]
                path.write_bytes(b"changed")

            self.effect = drift
            with self.subTest(source=source):
                self.reject()
        self.prepare()
        original = collector._switch_tree

        def mutate_after_read(raw: bytes) -> None:
            original(raw)
            (self.output / "attachments/tree.txt").write_bytes(b"changed after read")

        with patch.object(collector, "_switch_tree", side_effect=mutate_after_read):
            self.reject()

    def test_symlink_sources_output_and_exported_attachments_are_rejected(self) -> None:
        for source in ("input", "staged", "result", "output"):
            self.prepare()
            path = {
                "input": self.input_path,
                "staged": self.staged,
                "result": self.result,
                "output": self.output,
            }[source]
            moved = path.with_name(path.name + "-actual")
            path.rename(moved)
            path.symlink_to(moved, target_is_directory=moved.is_dir())
            with self.subTest(source=source):
                self.reject()
        self.sdk.assert_not_called()
        self.prepare()

        def symlink(operation: str) -> None:
            if operation == "attachments":
                path = self.output / "attachments/note.txt"
                path.unlink()
                path.symlink_to(self.input_path)

        self.effect = symlink
        self.reject()
        self.prepare()
        calls = self.sdk.call_count

        def redirected_export(operation: str) -> None:
            if operation == "tests":
                (self.output / "attachments").symlink_to(
                    self.result, target_is_directory=True
                )

        self.effect = redirected_export
        self.reject()
        self.assertEqual(self.sdk.call_count - calls, 2)
        self.assertEqual(list(self.result.iterdir()), [self.result / "Info.plist"])

    def test_existing_output_replay_missing_directory_and_sdk_timeout_preserve_sources(
        self,
    ) -> None:
        self.collect()
        calls = self.sdk.call_count
        self.reject()
        self.assertEqual(self.sdk.call_count, calls)
        self.prepare()
        self.output.rmdir()
        self.reject()
        self.assertFalse(self.output.exists())
        self.prepare()
        self.sdk.side_effect = subprocess.TimeoutExpired(["xcrun", "xcresulttool"], 30)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.collect()
        self.assertEqual((self.result / "Info.plist").read_bytes(), b"original result")
        self.assertTrue((self.output / "summary.log").is_file())
        self.assertTrue(self.input_path.is_file())

    def verify(self, proof: dict, **kwargs) -> None:
        calls = self.sdk.call_count
        try:
            collector.verify_inspection(proof, self.output, **kwargs)
        finally:
            self.assertEqual(
                self.sdk.call_count, calls, "verification must not export again"
            )

    def test_verify_roundtrip_with_exact_extra_files_is_read_only(self) -> None:
        proof = json.loads(json.dumps(self.collect()))
        self.verify(proof)
        extras = {
            "original-owner.json": b"original owner bytes",
            "original-phone-journal.json": b"original journal bytes",
            "restoration-release.json": json.dumps({"inspection": proof}).encode(),
        }
        for name, raw in extras.items():
            (self.output / name).write_bytes(raw)
        snapshot = collector._inventory(self.output)
        original_proof = json.dumps(proof, sort_keys=True)
        self.verify(proof, added_files=extras)
        self.verify(proof, added_files=extras)
        self.assertEqual(collector._inventory(self.output), snapshot)
        self.assertEqual(json.dumps(proof, sort_keys=True), original_proof)
        self.assertEqual(self.sdk.call_count, 3)
        with self.assertRaises(EvidenceError):
            self.verify(proof)
        for name, raw in extras.items():
            self.assertEqual((self.output / name).read_bytes(), raw)

    def test_verify_rejects_postcollect_source_bytes_or_inode_replacement(self) -> None:
        for kind in ("input", "staged", "result", "test", "app"):
            for replacement in (False, True):
                self.prepare()
                proof = self.collect()
                path = {
                    "input": self.input_path,
                    "staged": self.staged,
                    "result": self.result / "Info.plist",
                    "test": self.test_binary,
                    "app": self.app / "BleHubNitroHost",
                }[kind]
                if replacement:
                    other = path.with_name(path.name + ".replacement")
                    other.write_bytes(path.read_bytes())
                    other.replace(path)
                else:
                    path.write_bytes(b"changed after collection")
                with (
                    self.subTest(kind=kind, replacement=replacement),
                    self.assertRaises(EvidenceError),
                ):
                    self.verify(proof)

    def test_verify_rejects_replaced_output_result_and_app_directories(self) -> None:
        for kind in ("output", "result", "host", "app"):
            self.prepare()
            proof = self.collect()
            path = {
                "output": self.output,
                "result": self.result,
                "host": self.host,
                "app": self.app,
            }[kind]
            moved = path.with_name(path.name + "-original")
            path.rename(moved)
            shutil.copytree(moved, path)
            with self.subTest(kind=kind), self.assertRaises(EvidenceError):
                self.verify(proof)

    def test_verify_rejects_any_changed_export_and_unexpected_entries(self) -> None:
        names = [row["path"] for row in self.collect()["files"]]
        for name in names:
            self.prepare()
            proof = self.collect()
            (self.output / name).write_bytes(b"changed after collection")
            with (
                self.subTest(name=name),
                self.assertRaises((EvidenceError, ValueError)),
            ):
                self.verify(proof)
        for kind in ("file", "directory", "symlink", "missing"):
            self.prepare()
            proof = self.collect()
            path = self.output / "unexpected"
            if kind == "file":
                path.write_bytes(b"unexpected")
            elif kind == "directory":
                path.mkdir()
            elif kind == "symlink":
                path.symlink_to(self.input_path)
            else:
                (self.output / "attachments/screen.png").unlink()
            with self.subTest(kind=kind), self.assertRaises((EvidenceError, OSError)):
                self.verify(proof)

    def test_verify_extra_files_cannot_override_escape_or_misstate_bytes(self) -> None:
        proof = self.collect()
        for extras in (
            {"../escape": b"no"},
            {"/absolute": b"no"},
            {"nested/extra": b"no"},
            {"source-input.json": b"no"},
            {"SUMMARY.JSON": b"no"},
            {"missing.json": b"no"},
            {"original-owner.json": "not bytes"},
        ):
            with self.subTest(extras=extras), self.assertRaises(EvidenceError):
                self.verify(proof, added_files=extras)
        extra = self.output / "original-owner.json"
        extra.write_bytes(b"actual")
        with self.assertRaises(EvidenceError):
            self.verify(proof, added_files={extra.name: b"different"})
        extra.unlink()
        extra.symlink_to(self.input_path)
        with self.assertRaises(EvidenceError):
            self.verify(proof, added_files={extra.name: self.input_path.read_bytes()})

    def test_verify_rejects_returned_hash_binding_and_timestamp_corruption(
        self,
    ) -> None:
        proof = json.loads(json.dumps(self.collect()))
        changes = {
            **{key: "0" * 64 for key in proof if key.endswith("_sha256")},
            "scope": "rf-pass",
            "device": OTHER_DEVICE,
            "output_identity": [0, 0],
            "output_path": str(self.root),
            "source_signatures": {},
            "candidate_artifacts": {},
            "observed_start_time": NOW - 21,
            "observed_finish_time": NOW - 6,
            "inspected_switch_value": "0",
        }
        for key, value in changes.items():
            altered = {**proof, key: value}
            with self.subTest(key=key), self.assertRaises(EvidenceError):
                self.verify(altered)
        altered = json.loads(json.dumps(proof))
        altered["candidate_artifacts"]["TestHostPath"]["path"] = str(self.app)
        with self.assertRaises(EvidenceError):
            self.verify(altered)

    def test_verify_rechecks_ttl_without_sdk_or_rewriting_evidence(self) -> None:
        proof = self.collect()
        with patch.object(collector.time, "time", return_value=NOW + 3595):
            self.verify(proof)
        for now in (NOW + 3595.001, NOW - 11):
            with (
                self.subTest(now=now),
                patch.object(collector.time, "time", return_value=now),
                self.assertRaises(EvidenceError),
            ):
                self.verify(proof)
        snapshot = collector._inventory(self.output)
        self.verify(proof)
        self.assertEqual(collector._inventory(self.output), snapshot)

    def test_verify_detects_source_drift_and_expiry_during_validation(self) -> None:
        original = collector._switch_tree
        for kind in ("input", "result", "test", "app", "expiry"):
            self.prepare()
            proof = self.collect()

            def drift(raw: bytes, kind: str = kind) -> None:
                original(raw)
                if kind == "expiry":
                    self.enterContext(
                        patch.object(collector.time, "time", return_value=NOW + 4000)
                    )
                else:
                    path = {
                        "input": self.input_path,
                        "result": self.result / "Info.plist",
                        "test": self.test_binary,
                        "app": self.app / "BleHubNitroHost",
                    }[kind]
                    path.write_bytes(b"drift during verification")

            with (
                self.subTest(kind=kind),
                patch.object(collector, "_switch_tree", side_effect=drift),
                self.assertRaises(EvidenceError),
            ):
                self.verify(proof)


if __name__ == "__main__":
    unittest.main()
