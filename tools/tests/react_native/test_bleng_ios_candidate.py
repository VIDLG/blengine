"""Managed formal candidate regressions at the actual CoreDevice subprocess seam."""

from __future__ import annotations

import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.apple.app_artifact import fingerprint
from tools.react_native import ios_sdk
from tools.react_native.bleng_ios_candidate import IosCandidateOwner
from tools.react_native.ios_radio_lease import SCAN_OBSERVATION_SCOPE, IosRadioLease


class IosCandidateTests(unittest.TestCase):
    device = "00008110-001234567890ABCD"
    core = "130256F7-7150-5A81-A906-6806AAFBFADA"
    run_id = "12345678-1234-4234-8234-123456789abc"
    boot = "23456789-2345-4345-8345-23456789abcd"
    bundle = "dev.blehub.formal-host"
    executable = "FormalHost"
    installed = (
        "file:///private/var/containers/Bundle/Application/"
        "11111111-2222-4333-8444-555555555555/FormalHost.app/"
    )

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.app = self.root / "FormalHost.app"
        self.app.mkdir()
        self.info = {
            "CFBundleIdentifier": self.bundle,
            "CFBundleExecutable": self.executable,
            "CFBundlePackageType": "APPL",
            "CFBundleSupportedPlatforms": ["iPhoneOS"],
        }
        self.write_info()
        (self.app / self.executable).write_bytes(b"synthetic executable")
        self.clock = 100.0
        self.command_deadline = 160.0
        self.commands: list[list[str]] = []
        self.rows: list[dict] = []
        self.installed_apps: list[dict] = []
        self.hook = None
        self.challenge = None
        self.enterContext(patch("time.monotonic", side_effect=lambda: self.clock))
        self.sdk = self.enterContext(
            patch.object(ios_sdk.subprocess, "run", side_effect=self.command)
        )
        self.phone = IosRadioLease(
            self.device,
            self.run_id,
            self.root / "phone.json",
            lock_root=self.root / "locks",
            scope=SCAN_OBSERVATION_SCOPE,
        )
        self.lease = self.enterContext(patch.object(self.phone, "require_owner"))
        self.driver = SimpleNamespace(
            session_id="session-1",
            capabilities={
                "platformName": "iOS",
                "appium:udid": self.device,
                "appium:bundleId": self.bundle,
            },
        )

    def write_info(self) -> None:
        (self.app / "Info.plist").write_bytes(plistlib.dumps(self.info))

    def owner(self, **changes: object) -> IosCandidateOwner:
        values = {
            "app": self.app,
            "expected_sha256": fingerprint(self.app),
            "bundle_id": self.bundle,
            "executable": self.executable,
            "phone": self.phone,
            "device": self.device,
            "run_id": self.run_id,
            "attempt": self.root / "candidate-attempt",
            "cwd": self.root,
            "deadline": 160.0,
        }
        values.update(changes)
        return IosCandidateOwner(**values)

    def command(self, command: list[str], **kwargs: object):
        self.assertEqual(command[:3], ["xcrun", "devicectl", "device"])
        self.assertEqual(command[command.index("--device") + 1], self.device)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], self.command_deadline - self.clock)
        self.assertLessEqual(
            int(command[command.index("--timeout") + 1]), kwargs["timeout"]
        )
        self.commands.append(command.copy())
        action = tuple(command[3:5])
        result = {"deviceIdentifier": self.core}
        native = {
            "run_id": self.run_id,
            "boot_id": self.boot,
            "pid": 321,
            "bundle_id": self.bundle,
            "executable": self.installed.removeprefix("file://") + self.executable,
        }
        if action == ("info", "apps"):
            result["apps"] = self.installed_apps.copy()
        elif action == ("info", "processes"):
            result["runningProcesses"] = self.rows.copy()
        elif action == ("install", "app"):
            self.installed_apps = [
                {"bundleIdentifier": self.bundle, "url": self.installed}
            ]
            result["installedApplications"] = [
                {"bundleID": self.bundle, "installationURL": self.installed}
            ]
        elif action == ("process", "launch"):
            self.assertNotIn("--terminate-existing", command)
            self.assertNotIn("--console", command)
            self.assertEqual(
                command[command.index(self.bundle) :],
                [
                    self.bundle,
                    "--",
                    "--blehub-formal-identity-run-id",
                    self.run_id,
                ],
            )
            self.rows = [
                {
                    "processIdentifier": 321,
                    "executable": self.installed + self.executable,
                }
            ]
            result.update(
                process=self.rows[0].copy(),
                launchOptions={
                    "arguments": ["--blehub-formal-identity-run-id", self.run_id],
                    "activatedWhenStarted": True,
                    "startStopped": False,
                    "terminateExistingInstances": False,
                },
            )
        elif action == ("process", "terminate"):
            # A terminate is always PID-scoped and must name a process this run saw.
            pid = int(command[command.index("--pid") + 1])
            self.assertIn(pid, [row["processIdentifier"] for row in self.rows])
            self.rows = [row for row in self.rows if row["processIdentifier"] != pid]
        elif action == ("copy", "to"):
            request = json.loads(
                Path(command[command.index("--source") + 1]).read_bytes()
            )
            self.assertEqual(request["boot_id"], self.boot)
            self.challenge = request["challenge"]
        elif action == ("copy", "from"):
            source = command[command.index("--source") + 1]
            value = (
                native
                if source.endswith("/native-identity.json")
                else {"identity": native, "challenge": self.challenge}
            )
            Path(command[command.index("--destination") + 1]).write_text(
                json.dumps(value)
            )
        else:
            self.assertEqual(action, ("notification", "post"))
        document = {
            "info": {
                "outcome": "success",
                "commandType": ".".join(command[1:5]),
                "arguments": command[1:],
            },
            "result": result,
        }
        if self.hook:
            self.hook(action, document)
        Path(command[command.index("--json-output") + 1]).write_text(
            json.dumps(document)
        )
        output = kwargs["stdout"]
        if output != subprocess.PIPE:
            output.write(b"synthetic SDK output\n")
        self.clock += 0.01
        return subprocess.CompletedProcess(command, 0, "synthetic SDK output\n")

    def test_managed_receipts_bind_real_identity_reader_and_retirement_revokes(
        self,
    ) -> None:
        owner = self.owner()
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            owner.require_candidate_provenance()
        owner.install_and_launch()
        self.assertEqual(
            [c[3:5] for c in self.commands],
            [
                ["info", "apps"],
                ["info", "processes"],
                ["install", "app"],
                ["info", "apps"],
                ["info", "processes"],
                ["process", "launch"],
            ],
        )
        binding = owner.bind_native(require_owner=lambda: None)
        # The in-process binding returns the re-check itself; the identity it proved is the
        # owner's, because there is no WebDriver session object to carry it.
        self.assertEqual(owner.identity.boot_id, self.boot)
        self.assertEqual(owner.identity.device_identifier, self.core)
        binding()
        self.assertEqual(sum(c[3:5] == ["process", "launch"] for c in self.commands), 1)
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            owner.install_and_launch()
        with self.assertRaisesRegex(RuntimeError, "already bound"):
            owner.bind_native(require_owner=lambda: None)
        owner.retire()
        before = len(self.commands)
        with self.assertRaisesRegex(RuntimeError, "retired"):
            binding()
        self.assertEqual(len(self.commands), before)

    def test_reject_existing_before_install_and_after_install(self) -> None:
        for after_install in (False, True):
            with self.subTest(after_install=after_install):
                self.rows = (
                    []
                    if after_install
                    else [
                        {
                            "processIdentifier": 77,
                            "executable": self.installed + self.executable,
                        }
                    ]
                )
                self.commands.clear()
                owner = self.owner(attempt=self.root / str(after_install))

                def hook(action, document):
                    if action == ("install", "app"):
                        self.rows = [
                            {
                                "processIdentifier": 77,
                                "executable": self.installed + self.executable,
                            }
                        ]

                self.hook = hook
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    owner.install_and_launch()
                actions = [c[3:5] for c in self.commands]
                self.assertNotIn(["process", "launch"], actions)
                self.assertEqual(["install", "app"] in actions, after_install)
                with self.assertRaisesRegex(RuntimeError, "one-shot"):
                    owner.install_and_launch()

    def test_authorized_release_terminates_only_this_bundles_copy_before_install(
        self,
    ) -> None:
        # A copy left behind by an earlier attempt must not block every later run, and the
        # operator cannot release it separately: that entry needs the lease this run holds.
        self.rows = [
            {
                "processIdentifier": 77,
                "executable": self.installed + self.executable,
            }
        ]
        self.commands.clear()
        owner = self.owner(attempt=self.root / "released", release_running=True)

        def hook(action, document):
            if action == ("process", "terminate"):
                self.rows = []

        self.hook = hook
        owner.install_and_launch()
        actions = [c[3:5] for c in self.commands]
        self.assertEqual(actions.count(["process", "terminate"]), 1)
        self.assertIn(["install", "app"], actions)
        self.assertIn(["process", "launch"], actions)
        receipt = json.loads(
            (self.root / "released/01-before-install-release.json").read_bytes()
        )
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["terminated_pids"], [77])

    def test_authorized_release_that_does_not_take_effect_still_refuses(self) -> None:
        self.rows = [
            {
                "processIdentifier": 77,
                "executable": self.installed + self.executable,
            }
        ]
        owner = self.owner(attempt=self.root / "sticky", release_running=True)

        def hook(action, document):
            # A copy that comes straight back must not be installed over silently.
            if action == ("process", "terminate"):
                self.rows = [
                    {
                        "processIdentifier": 77,
                        "executable": self.installed + self.executable,
                    }
                ]

        self.hook = hook
        with self.assertRaisesRegex(RuntimeError, "still running"):
            owner.install_and_launch()
        actions = [c[3:5] for c in self.commands]
        self.assertEqual(actions.count(["process", "terminate"]), 1)
        self.assertNotIn(["install", "app"], actions)

    def test_bad_artifact_and_owner_fail_before_device_commands(self) -> None:
        for field, value in (
            ("CFBundleIdentifier", "dev.blehub.legacy"),
            ("CFBundleSupportedPlatforms", ["iPhoneSimulator"]),
            ("CFBundleExecutable", "../unsafe"),
            ("CFBundlePackageType", "FMWK"),
        ):
            with self.subTest(field=field):
                original = self.info[field]
                self.info[field] = value
                self.write_info()
                with self.assertRaises((ValueError, RuntimeError)):
                    self.owner(attempt=self.root / field).install_and_launch()
                self.info[field] = original
                self.write_info()
        with self.assertRaises(ValueError):
            self.owner(device="any")
        owner = self.owner()
        self.lease.side_effect = RuntimeError("lease lost")
        with self.assertRaisesRegex(RuntimeError, "lease lost"):
            owner.install_and_launch()
        self.sdk.assert_not_called()

    def test_receipt_mismatch_or_candidate_drift_never_launches(self) -> None:
        for fault in (
            "device",
            "arguments",
            "bundle",
            "candidate",
            "lease",
            "deadline",
        ):
            with self.subTest(fault=fault):
                self.commands.clear()
                self.rows = []
                self.clock = 100.0
                self.lease.side_effect = None
                owner = self.owner(attempt=self.root / fault)

                def hook(action, document, fault=fault):
                    if action != ("install", "app"):
                        return
                    if fault == "device":
                        document["result"]["deviceIdentifier"] = "foreign"
                    elif fault == "arguments":
                        document["info"]["arguments"] = document["info"]["arguments"][
                            :-1
                        ] + ["other.app"]
                    elif fault == "bundle":
                        document["result"]["installedApplications"][0]["bundleID"] = (
                            "foreign"
                        )
                    elif fault == "candidate":
                        (self.app / self.executable).write_bytes(b"changed")
                    elif fault == "lease":
                        self.lease.side_effect = RuntimeError("lease lost")
                    else:
                        self.clock = 161.0

                self.hook = hook
                with self.assertRaises((RuntimeError, TimeoutError)):
                    owner.install_and_launch()
                self.assertNotIn(["process", "launch"], [c[3:5] for c in self.commands])

    def test_launch_failure_is_not_retried_or_cleaned_up(self) -> None:
        owner = self.owner()

        def fail(action, document):
            if action == ("process", "launch"):
                raise subprocess.TimeoutExpired("synthetic launch", 5)

        self.hook = fail
        with self.assertRaises(subprocess.TimeoutExpired):
            owner.install_and_launch()
        self.assertTrue(self.rows, "remote launch may have happened before timeout")
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            owner.install_and_launch()
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            owner.require_candidate_provenance()
        self.assertEqual(len(self.commands), 6)

    def test_launch_options_and_executable_are_verified(self) -> None:
        for field, value in (
            ("terminateExistingInstances", True),
            ("startStopped", True),
            ("activatedWhenStarted", False),
            ("arguments", []),
            ("executable", self.installed + "Foreign"),
        ):
            with self.subTest(field=field):
                self.rows = []
                owner = self.owner(attempt=self.root / field)

                def hook(action, document, field=field, value=value):
                    if action == ("process", "launch"):
                        section = (
                            "process" if field == "executable" else "launchOptions"
                        )
                        document["result"][section][field] = value

                self.hook = hook
                with self.assertRaises(RuntimeError):
                    owner.install_and_launch()
                with self.assertRaisesRegex(RuntimeError, "not ready"):
                    owner.require_candidate_provenance()

    def test_provenance_recheck_is_read_only_and_detects_receipt_change(self) -> None:
        owner = self.owner()
        owner.install_and_launch()
        before = len(self.commands)
        owner.require_candidate_provenance()
        original = owner.install_receipt.read_bytes()
        owner.install_receipt.write_bytes(original + b" ")
        with self.assertRaisesRegex(RuntimeError, "receipt.*changed"):
            owner.require_candidate_provenance()
        owner.install_receipt.write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, "invalidated"):
            owner.require_candidate_provenance()
        self.assertEqual(len(self.commands), before)

    def test_preexisting_attempt_is_never_overwritten(self) -> None:
        attempt = self.root / "candidate-attempt"
        attempt.mkdir()
        marker = attempt / "install.json"
        marker.write_bytes(b"historical")
        with self.assertRaises(FileExistsError):
            self.owner()
        self.assertEqual(marker.read_bytes(), b"historical")
        self.sdk.assert_not_called()

    def test_renamed_old_executable_is_refused_via_installed_bundle_directory(
        self,
    ) -> None:
        old = self.installed.replace("FormalHost.app", "Old%20Host.app")
        self.installed_apps = [{"bundleIdentifier": self.bundle, "url": old}]
        self.rows = [{"processIdentifier": 77, "executable": old + "RenamedExecutable"}]
        owner = self.owner()
        with self.assertRaisesRegex(RuntimeError, "already running"):
            owner.install_and_launch()
        self.assertEqual(
            [c[3:5] for c in self.commands], [["info", "apps"], ["info", "processes"]]
        )

    def test_artifact_and_lease_failures_cannot_revive_authority(self) -> None:
        for source in ("artifact", "lease"):
            with self.subTest(source=source):
                self.rows = []
                self.lease.side_effect = None
                owner = self.owner(attempt=self.root / source)
                owner.install_and_launch()
                original = (self.app / self.executable).read_bytes()
                if source == "artifact":
                    (self.app / self.executable).write_bytes(b"drift")
                else:
                    self.lease.side_effect = RuntimeError("lease lost")
                with self.assertRaises(RuntimeError):
                    owner.read_candidate_sha256()
                (self.app / self.executable).write_bytes(original)
                self.lease.side_effect = None
                before = len(self.commands)
                with self.assertRaisesRegex(RuntimeError, "invalidated"):
                    owner.read_candidate_sha256()
                with self.assertRaisesRegex(RuntimeError, "invalidated"):
                    owner.require_candidate_provenance()
                self.assertEqual(len(self.commands), before)

    def test_failed_bind_never_repins_even_after_evidence_is_restored(self) -> None:
        for failure in ("receipt", "native"):
            with self.subTest(failure=failure):
                self.rows = []
                owner = self.owner(attempt=self.root / failure)
                owner.install_and_launch()
                original = owner.install_receipt.read_bytes()
                if failure == "receipt":
                    owner.install_receipt.write_bytes(original + b" ")
                else:
                    self.rows = []
                with self.assertRaises(RuntimeError):
                    owner.bind_native(require_owner=lambda: None)
                owner.install_receipt.write_bytes(original)
                before = len(self.commands)
                with self.assertRaisesRegex(RuntimeError, "already bound"):
                    owner.bind_native(require_owner=lambda: None)
                self.assertEqual(len(self.commands), before)

    def test_launch_receipt_after_deadline_never_becomes_ready(self) -> None:
        owner = self.owner()

        def hook(action, document):
            if action == ("process", "launch"):
                self.clock = 161.0

        self.hook = hook
        with self.assertRaises(TimeoutError):
            owner.install_and_launch()
        self.assertTrue(owner.launch_receipt.is_file())
        before = len(self.commands)
        with self.assertRaises(TimeoutError):
            owner.bind_native(require_owner=lambda: None)
        self.assertEqual(len(self.commands), before)

    def leased_candidate(self, *, name: str = "finish"):
        self.rows = []
        self.phone = IosRadioLease(
            self.device,
            self.run_id,
            self.root / (name + "-phone.json"),
            lock_root=self.root / (name + "-locks"),
            scope=SCAN_OBSERVATION_SCOPE,
        )
        self.phone.acquire()
        owner = self.owner(attempt=self.root / name)
        owner.install_and_launch()
        binding = owner.bind_native(require_owner=lambda: None)
        return owner, binding

    def finish_candidate(self, owner, primary=None):
        self.command_deadline = 210.0
        return self.phone.finish_observation(
            primary,
            finalize_owned=lambda: owner.finalize(
                primary, deadline=self.command_deadline
            ),
        )

    def test_successful_finalization_retains_app_and_releases_real_lease(self) -> None:
        owner, binding = self.leased_candidate()
        before = len(self.commands)
        self.assertEqual(self.finish_candidate(owner), [])
        actions = [c[3:5] for c in self.commands[before:]]
        self.assertIn(["notification", "post"], actions)
        self.assertNotIn(["process", "terminate"], actions)
        self.assertTrue(self.rows)
        self.assertFalse(self.phone.lock.exists())
        self.assertEqual(self.phone.document["status"], "released")
        evidence = json.loads(
            (self.root / "finish/finalization/result.json").read_bytes()
        )
        self.assertEqual(evidence["action"], "retain")
        self.assertEqual(evidence["status"], "passed")
        self.assertNotIn("resource_zero", evidence)
        count = len(self.commands)
        with self.assertRaises(RuntimeError):
            binding()
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            owner.finalize(None, deadline=210.0)
        self.assertEqual(len(self.commands), count)

    def test_failure_cleanup_has_independent_budget_without_reviving_scenario(
        self,
    ) -> None:
        owner, binding = self.leased_candidate()
        self.clock = 161.0
        with self.assertRaises(TimeoutError):
            binding()
        primary = TimeoutError("scan total deadline")
        before = len(self.commands)
        self.assertEqual(self.finish_candidate(owner, primary), [])
        actions = [c[3:5] for c in self.commands[before:]]
        self.assertEqual(actions.count(["process", "terminate"]), 1)
        self.assertLess(
            actions.index(["notification", "post"]),
            actions.index(["process", "terminate"]),
        )
        self.assertEqual(actions[-1], ["info", "processes"])
        self.assertEqual(self.rows, [])
        self.assertFalse(self.phone.lock.exists())
        self.assertEqual(
            self.phone.document["primary_error"], "TimeoutError: scan total deadline"
        )
        self.assertEqual(self.phone.document["cleanup_errors"], [])
        with self.assertRaises((RuntimeError, TimeoutError)):
            binding()

    def test_foreign_boot_missing_process_or_changed_receipt_quarantines_without_kill(
        self,
    ) -> None:
        for failure in ("boot", "missing", "receipt", "artifact"):
            with self.subTest(failure=failure):
                owner, _ = self.leased_candidate(name=failure)
                old_boot = self.boot
                original = (self.app / self.executable).read_bytes()
                if failure == "boot":
                    self.boot = "34567890-3456-4456-8456-34567890abcd"
                elif failure == "missing":
                    self.rows = []
                elif failure == "receipt":
                    owner.install_receipt.write_bytes(
                        owner.install_receipt.read_bytes() + b" "
                    )
                else:
                    (self.app / self.executable).write_bytes(b"drift")
                before = len(self.commands)
                errors = self.finish_candidate(owner, ValueError("scan failed"))
                self.assertTrue(errors)
                self.assertEqual(self.phone.document["status"], "quarantined")
                self.assertEqual(
                    self.phone.document["primary_error"], "ValueError: scan failed"
                )
                self.assertTrue(self.phone.lock.exists())
                self.assertNotIn(
                    ["process", "terminate"], [c[3:5] for c in self.commands[before:]]
                )
                self.boot = old_boot
                (self.app / self.executable).write_bytes(original)

    def test_termination_uncertainty_or_foreign_post_inventory_preserves_failure(
        self,
    ) -> None:
        for failure in (
            "timeout",
            "still-running",
            "replacement",
            "foreign-device",
            "malformed",
        ):
            with self.subTest(failure=failure):
                self.hook = None
                owner, _ = self.leased_candidate(name=failure)
                old_rows = self.rows.copy()
                terminated = False

                def hook(action, document, failure=failure, old_rows=old_rows):
                    nonlocal terminated
                    if action == ("process", "terminate"):
                        terminated = True
                        if failure == "timeout":
                            raise subprocess.TimeoutExpired("terminate", 5)
                        if failure == "still-running":
                            self.rows = old_rows
                        if failure == "replacement":
                            self.rows = [
                                {
                                    "processIdentifier": 999,
                                    "executable": self.installed + "Renamed",
                                }
                            ]
                    if terminated and action == ("info", "processes"):
                        if failure == "foreign-device":
                            document["result"]["deviceIdentifier"] = "foreign"
                        if failure == "malformed":
                            document["result"]["runningProcesses"] = [{}]

                self.hook = hook
                before = len(self.commands)
                errors = self.finish_candidate(
                    owner, RuntimeError("primary UI failure")
                )
                self.assertTrue(errors)
                self.assertEqual(self.phone.document["status"], "quarantined")
                self.assertEqual(
                    self.phone.document["primary_error"],
                    "RuntimeError: primary UI failure",
                )
                self.assertEqual(
                    sum(
                        c[3:5] == ["process", "terminate"]
                        for c in self.commands[before:]
                    ),
                    1,
                )

    def test_invalidated_owner_cannot_gain_cleanup_authority(self) -> None:
        owner, _ = self.leased_candidate()
        original = owner.install_receipt.read_bytes()
        owner.install_receipt.write_bytes(original + b" ")
        with self.assertRaises(RuntimeError):
            owner.require_candidate_provenance()
        owner.install_receipt.write_bytes(original)
        before = len(self.commands)
        self.assertTrue(self.finish_candidate(owner, RuntimeError("primary")))
        self.assertEqual(len(self.commands), before)
        self.assertTrue(self.phone.lock.exists())

    def test_unbound_run_retires_only_its_own_app(self) -> None:
        """A run that never bound can still retire the App it installed.

        Refusing that left the App running, so every later run for this device refused
        takeover. This run's own verified launch receipt names the exact executable in a
        unique container, so cleanup stops that process and nothing else - and it does so
        without reading a native identity it never bound.
        """
        self.phone = IosRadioLease(
            self.device,
            self.run_id,
            self.root / "unbound.json",
            lock_root=self.root / "unbound-locks",
            scope=SCAN_OBSERVATION_SCOPE,
        )
        self.phone.acquire()
        owner = self.owner(attempt=self.root / "unbound")
        owner.install_and_launch()
        before = len(self.commands)
        self.assertEqual(self.finish_candidate(owner, RuntimeError("primary")), [])
        actions = [c[3:5] for c in self.commands[before:]]
        # No native identity is read: no challenge, no second launch.
        self.assertNotIn(["notification", "post"], actions)
        self.assertNotIn(["process", "launch"], actions)
        self.assertEqual(actions.count(["process", "terminate"]), 1)
        self.assertEqual(actions[-1], ["info", "processes"])
        self.assertEqual(self.rows, [])
        # Cleanup succeeded, so the lease releases rather than quarantines.
        self.assertFalse(self.phone.lock.exists())
        self.assertEqual(self.phone.document["status"], "released")
        evidence = json.loads(
            (self.root / "unbound/finalization/result.json").read_bytes()
        )
        self.assertEqual(evidence["action"], "terminate")
        self.assertIsNone(evidence["identity"])

    def test_failed_binding_invalidates_authority_and_is_never_adopted(self) -> None:
        """A failed `bind` is different: the identity could not be pinned, so the run
        loses authority and must not touch the App during cleanup."""
        self.phone = IosRadioLease(
            self.device,
            self.run_id,
            self.root / "failed-bind.json",
            lock_root=self.root / "failed-bind-locks",
            scope=SCAN_OBSERVATION_SCOPE,
        )
        self.phone.acquire()
        owner = self.owner(attempt=self.root / "failed-bind")
        owner.install_and_launch()
        self.rows = []
        with self.assertRaises(RuntimeError):
            owner.bind_native(require_owner=lambda: None)
        before = len(self.commands)
        self.assertTrue(self.finish_candidate(owner, RuntimeError("primary")))
        self.assertEqual(len(self.commands), before)
        self.assertTrue(self.phone.lock.exists())
        self.assertEqual(self.phone.document["status"], "quarantined")

    def test_cleanup_deadline_exhaustion_never_extends_or_retries_commands(
        self,
    ) -> None:
        for stage in ("challenge", "terminate"):
            with self.subTest(stage=stage):
                self.clock = 100.0
                self.command_deadline = 160.0
                self.hook = None
                owner, _ = self.leased_candidate(name=stage)

                def expire(action, document, stage=stage):
                    target = (
                        ("notification", "post")
                        if stage == "challenge"
                        else ("process", "terminate")
                    )
                    if action == target:
                        self.clock = 211.0

                self.hook = expire
                before = len(self.commands)
                self.assertTrue(self.finish_candidate(owner, ValueError("primary")))
                actions = [c[3:5] for c in self.commands[before:]]
                self.assertEqual(
                    actions.count(["process", "terminate"]), int(stage == "terminate")
                )
                self.assertEqual(
                    actions[-1],
                    ["notification", "post"]
                    if stage == "challenge"
                    else ["process", "terminate"],
                )
                self.assertTrue(self.phone.lock.exists())
                self.assertEqual(
                    self.phone.document["primary_error"], "ValueError: primary"
                )
                self.assertEqual(self.phone.document["status"], "quarantined")

    def test_retained_app_with_failed_evidence_publication_does_not_release_phone(
        self,
    ) -> None:
        owner, _ = self.leased_candidate()
        original_open = Path.open

        def fail_result(path, *args, **kwargs):
            if path.name == "result.json" and path.parent.name == "finalization":
                raise OSError("finalization publication failed")
            return original_open(path, *args, **kwargs)

        before = len(self.commands)
        with patch.object(Path, "open", fail_result):
            errors = self.finish_candidate(owner)
        self.assertIn("finalization publication failed", " ".join(errors))
        self.assertEqual(self.phone.document["status"], "quarantined")
        self.assertIsNone(self.phone.document["primary_error"])
        self.assertTrue(self.phone.lock.exists())
        self.assertTrue(self.rows)
        self.assertNotIn(
            ["process", "terminate"], [c[3:5] for c in self.commands[before:]]
        )

    def test_double_cleanup_failure_records_publication_and_owner_loss(self) -> None:
        owner, _ = self.leased_candidate()

        def lose_owner(action, document):
            if action == ("process", "terminate"):
                (self.phone.lock / "owner.json").write_bytes(b"foreign owner")
                raise subprocess.TimeoutExpired("terminate", 5)

        self.hook = lose_owner
        original_open = Path.open

        def fail_result(path, *args, **kwargs):
            if path.name == "result.json" and path.parent.name == "finalization":
                raise OSError("cannot publish finalization evidence")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", fail_result):
            errors = self.finish_candidate(owner, ValueError("original UI error"))
        journal = json.loads(self.phone.path.read_bytes())
        for message in (
            "TimeoutExpired",
            "ownership",
            "cannot publish finalization evidence",
        ):
            self.assertIn(
                message if message != "ownership" else "owner", " ".join(errors)
            )
            self.assertIn(
                message if message != "ownership" else "owner",
                " ".join(journal["cleanup_errors"]),
            )
        self.assertEqual(journal["primary_error"], "ValueError: original UI error")
        self.assertEqual(
            (self.phone.lock / "owner.json").read_bytes(), b"foreign owner"
        )
        self.assertEqual(journal["status"], "quarantined")

    def test_process_replacement_during_final_identity_sweep_refuses_termination(
        self,
    ) -> None:
        owner, _ = self.leased_candidate()

        def replace(action, document):
            if action == ("notification", "post"):
                self.rows = [
                    {"processIdentifier": 321, "executable": self.installed + "Foreign"}
                ]

        self.hook = replace
        before = len(self.commands)
        self.assertTrue(self.finish_candidate(owner, ValueError("primary")))
        self.assertNotIn(
            ["process", "terminate"], [c[3:5] for c in self.commands[before:]]
        )
        self.assertEqual(self.phone.document["status"], "quarantined")

    def test_primary_sdk_error_preserved_when_owner_also_lost(self) -> None:
        owner = self.owner()

        def fail(action, document):
            self.lease.side_effect = RuntimeError("lease lost after command")
            raise subprocess.TimeoutExpired("synthetic", 5)

        self.hook = fail
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            owner.install_and_launch()
        self.assertIn("lease lost", " ".join(caught.exception.__notes__))
        self.assertEqual(len(self.commands), 1)
