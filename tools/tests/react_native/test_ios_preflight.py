"""Isolated SDK/signature boundary regressions; no real Apple tools or devices."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import plistlib
import ssl
import subprocess
import tempfile
import unittest
import uuid
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools.apple import ios_preflight as preflight
from tools.apple import signing_identity, xcodegen_toolchain
from tools.local_config import ConfiguredValue
from tools.react_native.ios_sdk import devicectl_list_devices, devicectl_lock_state


class IosPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / "Synthetic.app"
        self.app.mkdir()
        self.team = uuid.uuid4().hex[:10].upper()
        raw = uuid.uuid4().hex.upper()
        self.device = raw[:8] + "-" + raw[8:24]
        self.bundle = "dev.synthetic." + uuid.uuid4().hex
        self.info = {
            "CFBundleIdentifier": self.bundle,
            "CFBundleExecutable": "Synthetic",
            "CFBundlePackageType": "APPL",
            "CFBundleSupportedPlatforms": ["iPhoneOS"],
        }
        (self.app / "Info.plist").write_bytes(plistlib.dumps(self.info))
        (self.app / "Synthetic").write_bytes(
            b"synthetic executable, not a signed binary"
        )
        self.profile = {
            "TeamIdentifier": [self.team],
            "ApplicationIdentifierPrefix": [self.team],
            "ExpirationDate": (
                datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=2)
            ),
            "ProvisionedDevices": [self.device],
            "Entitlements": {
                "application-identifier": f"{self.team}.{self.bundle}",
                "com.apple.developer.team-identifier": self.team,
                "get-task-allow": True,
            },
        }
        self.save_profile()
        self.signed_entitlements = copy.deepcopy(self.profile["Entitlements"])
        self.entitlements_output_override: str | None = None
        self.signature_team = self.team
        self.signature_rc = 0
        self.locked = False
        self.drift = False
        self.timeout_command: str | None = None
        self.xcodegen_version = xcodegen_toolchain.VERSION
        self.identity_count = 1
        self.identity_kind = "Apple Development"
        self.identity_team = self.team
        self.certificate_team = self.team
        self.certificate_pem = ssl.DER_cert_to_PEM_cert(b"synthetic public DER")
        self.identity_fingerprint = (
            hashlib.sha1(
                ssl.PEM_cert_to_DER_cert(self.certificate_pem), usedforsecurity=False
            )
            .hexdigest()
            .upper()
        )
        self.subject_override: str | None = None
        self.certificate_output = self.certificate_pem
        self.command_failure: str | None = None
        self.developer_mode = "enabled"
        self.installed_apps = [
            {
                "bundleIdentifier": f"{self.bundle}.UITests.xctrunner",
                "builtByDeveloper": True,
            }
        ]
        self.public_certificate_paths: list[Path] = []
        self.commands: list[list[str]] = []
        self.lookups: list[str] = []
        stack = self.enterContext(ExitStack())
        stack.enter_context(
            patch.object(preflight.platform, "system", return_value="Darwin")
        )
        stack.enter_context(
            patch.object(preflight.shutil, "which", side_effect=self.which)
        )
        stack.enter_context(patch.object(subprocess, "run", side_effect=self.command))
        stack.enter_context(
            patch.object(
                subprocess,
                "Popen",
                side_effect=AssertionError("real process forbidden"),
            )
        )
        self.config = stack.enter_context(
            patch.object(
                preflight,
                "resolve_local_value",
                return_value=ConfiguredValue(self.team, "synthetic local config"),
            )
        )
        stack.enter_context(
            patch.object(
                xcodegen_toolchain,
                "install",
                side_effect=AssertionError("implicit tool installation forbidden"),
            )
        )

    def save_profile(self) -> None:
        (self.app / "embedded.mobileprovision").write_bytes(
            plistlib.dumps(self.profile)
        )

    def which(self, name: str) -> str:
        self.lookups.append(name)
        self.assertIn(
            name, {"codesign", "security", "xcrun", "xcodebuild", "xcodegen", "openssl"}
        )
        return "/synthetic/bin/" + name

    def command(
        self, command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        name = Path(command[0]).name
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 30)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        args = command[1:]
        if name == self.timeout_command and name != "openssl":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        stdout, stderr, code = "", "", 0
        if name == "codesign" and args[:3] == ["--verify", "--deep", "--strict"]:
            code = self.signature_rc
            if self.drift and args[-1] == str(self.app):
                (self.app / "Synthetic").write_bytes(
                    b"changed during signature verification"
                )
        elif name == "codesign" and args[:2] == ["-d", "--verbose=4"]:
            app = Path(args[-1])
            info = plistlib.loads((app / "Info.plist").read_bytes())
            stderr = (
                f"Identifier={info['CFBundleIdentifier']}\n"
                f"TeamIdentifier={self.signature_team}\n"
            )
        elif name == "codesign" and args[:4] == [
            "-d",
            "--entitlements",
            "-",
            "--xml",
        ]:
            app = Path(args[-1])
            if app == self.app:
                stdout = (
                    self.entitlements_output_override
                    if self.entitlements_output_override is not None
                    else plistlib.dumps(self.signed_entitlements).decode()
                )
            else:
                profile = plistlib.loads(
                    (app / "embedded.mobileprovision").read_bytes()
                )
                stdout = plistlib.dumps(profile["Entitlements"]).decode()
        elif name == "security" and args[:3] == ["cms", "-D", "-i"]:
            stdout = Path(args[-1]).read_text()
        elif name == "security" and args == ["default-keychain", "-d", "user"]:
            stdout = '"/synthetic/login.keychain-db"\n'
        elif name == "security" and args in (
            ["find-identity", "-v", "-p", "codesigning"],
            [
                "find-identity",
                "-v",
                "-p",
                "codesigning",
                "/synthetic/login.keychain-db",
            ],
        ):
            if self.identity_count:
                stdout = (
                    f" 1) {self.identity_fingerprint} "
                    f'"{self.identity_kind}: Synthetic ({self.identity_team})"\n'
                )
            stdout += f" {self.identity_count} valid identities found\n"
        elif name == "security" and args in (
            ["find-certificate", "-a", "-c", "Apple Development", "-p"],
            [
                "find-certificate",
                "-a",
                "-c",
                "Apple Development",
                "-p",
                "/synthetic/login.keychain-db",
            ],
        ):
            stdout = self.certificate_output
        elif name == "openssl":
            self.assertEqual(
                args[:-1],
                [
                    "x509",
                    "-noout",
                    "-subject",
                    "-nameopt",
                    "sep_multiline,oid,utf8",
                    "-in",
                ],
            )
            certificate_path = Path(args[-1])
            self.public_certificate_paths.append(certificate_path)
            self.assertEqual(certificate_path.read_text(), self.certificate_pem)
            if name == self.timeout_command:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            stdout = (
                self.subject_override
                if self.subject_override is not None
                else (
                    "subject=\n"
                    f"    2.5.4.3=Apple Development: Synthetic ({self.identity_team})\n"
                    f"    2.5.4.11={self.certificate_team}\n"
                    "    2.5.4.10=Synthetic Org\n"
                )
            )
        elif name == "xcodebuild" and args == ["-version"]:
            stdout = "Xcode 26.0\nBuild version synthetic\n"
        elif name == "xcrun" and args == ["--sdk", "iphoneos", "--show-sdk-path"]:
            stdout = "/synthetic/Xcode.app/Contents/Developer/SDKs/iPhoneOS.sdk\n"
        elif name == "xcrun" and args == ["--find", "swiftc"]:
            stdout = "/synthetic/Xcode.app/Contents/Developer/Toolchains/swiftc\n"
        elif name == "xcodegen" and args == ["--version"]:
            stdout = "Version: " + self.xcodegen_version
        elif command == devicectl_list_devices():
            stdout = f"Synthetic phone {self.device} available (paired)\n"
        elif command == devicectl_lock_state(self.device):
            stdout = json.dumps(
                {"result": {"passcodeRequired": self.locked, "unlockedSinceBoot": True}}
            )
        elif command == preflight.ios_sdk.devicectl_device_details(self.device):
            stdout = json.dumps(
                {
                    "result": {
                        "deviceProperties": {"developerModeStatus": self.developer_mode}
                    }
                }
            )
        elif command == preflight.ios_sdk.devicectl_device_apps(self.device):
            stdout = json.dumps({"result": {"apps": self.installed_apps}})
        else:
            self.fail(f"unexpected or mutating command: {command!r}")
        if name == self.command_failure:
            code, stderr = 1, "synthetic read-only query failed"
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    def observe(
        self, *, device: str | None = None, xctestrun: str | None = None
    ) -> dict[str, Any]:
        return preflight.preflight(
            mode="prebuilt", app=str(self.app), device=device, xctestrun=xctestrun
        )

    def test_ui_preflight_requires_developer_mode_and_runner_inventory(self) -> None:
        xctestrun = self.root / "candidate.xctestrun"
        xctestrun.write_bytes(
            plistlib.dumps(
                {
                    "SyntheticUITests": {
                        "TestHostBundleIdentifier": f"{self.bundle}.UITests.xctrunner",
                        "ProductModuleName": "SyntheticUITests",
                    }
                }
            )
        )
        runner = xctestrun.parent / "Debug-iphoneos/SyntheticUITests-Runner.app"
        runner.mkdir(parents=True)
        runner_info = {
            "CFBundleIdentifier": f"{self.bundle}.UITests.xctrunner",
            "CFBundleExecutable": "SyntheticUITests-Runner",
            "CFBundlePackageType": "APPL",
            "CFBundleSupportedPlatforms": ["iPhoneOS"],
        }
        (runner / "Info.plist").write_bytes(plistlib.dumps(runner_info))
        (runner / "SyntheticUITests-Runner").write_bytes(b"runner")
        runner_profile = copy.deepcopy(self.profile)
        runner_profile["Entitlements"] = {
            **runner_profile["Entitlements"],
            "application-identifier": f"{self.team}.{runner_info['CFBundleIdentifier']}",
        }
        (runner / "embedded.mobileprovision").write_bytes(
            plistlib.dumps(runner_profile)
        )
        report = self.observe(device=self.device, xctestrun=str(xctestrun))
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["observations"]["developer-mode"]["status"], "verified")
        self.assertEqual(
            report["observations"]["ui-test-runner-inventory"]["status"], "verified"
        )
        self.developer_mode = "disabled"
        blocked = self.observe(device=self.device, xctestrun=str(xctestrun))
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn(
            "Developer Mode", blocked["observations"]["developer-mode"]["detail"]
        )
        self.developer_mode = "enabled"
        self.installed_apps.append(
            {"bundleIdentifier": f"{self.bundle}.xctrunner", "builtByDeveloper": True}
        )
        stale = self.observe(device=self.device, xctestrun=str(xctestrun))
        self.assertEqual(stale["status"], "blocked")
        self.assertIn(
            "stale or duplicate XCTest runners",
            stale["observations"]["ui-test-runner-inventory"]["detail"],
        )

    def test_prebuilt_hash_and_scope_without_build_keychain_or_implicit_device(
        self,
    ) -> None:
        with patch.dict(os.environ, {"BLEHUB_APPLE_DEVICE": self.device}):
            report = self.observe(device="  ")
        self.assertEqual(report["status"], "verified")
        self.config.assert_not_called()
        self.assertEqual(set(self.lookups), {"codesign", "security"})
        self.assertEqual(len(self.commands), 4)
        expected = preflight.fingerprint(self.app)
        observations = report["observations"]
        self.assertEqual(observations["artifact"]["sha256"], expected)
        self.assertEqual(observations["artifact-stability"]["sha256"], expected)
        self.assertTrue(
            observations["provisioning-profile"]["signed_entitlements_match"]
        )
        for name in (
            "private-key-access",
            "corebluetooth-tcc",
            "ui-automation",
            "rf",
            "device-readiness",
        ):
            self.assertEqual(observations[name]["status"], "not-checked")
            self.assertFalse(observations[name]["required"])
        self.assertIn("Only required", report["status_scope"])

    def test_prebuilt_checks_actual_signed_entitlements_against_profile(self) -> None:
        original = copy.deepcopy(self.signed_entitlements)
        for mutation in (
            {"application-identifier": self.team + ".dev.foreign"},
            {"com.apple.developer.team-identifier": "Z" * 10},
            {"get-task-allow": False},
            {"get-task-allow": 1},
            {"application-identifier": None},
        ):
            with self.subTest(mutation=mutation):
                self.signed_entitlements = original | mutation
                report = self.observe(device=self.device)
                self.assertEqual(report["status"], "blocked")
                self.assertEqual(
                    report["observations"]["device-readiness"]["status"], "not-checked"
                )
        self.signed_entitlements = original
        for raw in ("", "not a plist", plistlib.dumps(["not a dictionary"]).decode()):
            with self.subTest(raw=raw):
                self.entitlements_output_override = raw
                report = self.observe()
                self.assertEqual(report["status"], "blocked")
                self.assertEqual(
                    report["observations"]["signature"]["status"], "blocked"
                )

    def test_explicit_device_uses_real_readiness_owner_and_rejects_lock(self) -> None:
        for locked, expected in ((False, "verified"), (True, "blocked")):
            with self.subTest(locked=locked):
                self.locked = locked
                report = self.observe(device=self.device.lower())
                self.assertEqual(report["status"], expected)
                self.assertEqual(report["device"], self.device)
                self.assertEqual(
                    report["observations"]["device-readiness"]["status"], expected
                )
        sdk = [command for command in self.commands if "devicectl" in command]
        self.assertEqual(
            sdk, [devicectl_list_devices(), devicectl_lock_state(self.device)] * 2
        )

    def test_profile_expiration_bundle_team_development_and_foreign_device_rejected(
        self,
    ) -> None:
        original = copy.deepcopy(self.profile)
        mutations = {
            "expired": lambda p: p.update(
                ExpirationDate=datetime(2000, 1, 1, tzinfo=timezone.utc).replace(
                    tzinfo=None
                )
            ),
            "missing expiration": lambda p: p.pop("ExpirationDate"),
            "foreign device": lambda p: p.update(ProvisionedDevices=["B" * 40]),
            "bundle": lambda p: p["Entitlements"].update(
                {"application-identifier": self.team + ".dev.foreign"}
            ),
            "foreign wildcard": lambda p: p["Entitlements"].update(
                {"application-identifier": self.team + ".dev.foreign.*"}
            ),
            "partial component wildcard": lambda p: p["Entitlements"].update(
                {"application-identifier": self.team + ".dev*"}
            ),
            "multiple wildcards": lambda p: p["Entitlements"].update(
                {"application-identifier": self.team + ".*.*"}
            ),
            "profile team": lambda p: p.update(TeamIdentifier=["Z" * 10]),
            "entitlement team": lambda p: p["Entitlements"].update(
                {"com.apple.developer.team-identifier": "Z" * 10}
            ),
            "distribution": lambda p: p["Entitlements"].update(
                {"get-task-allow": False}
            ),
            "all devices": lambda p: p.update(ProvisionsAllDevices=True),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.profile = copy.deepcopy(original)
                mutate(self.profile)
                self.save_profile()
                self.commands.clear()
                report = self.observe(device=self.device)
                self.assertEqual(report["status"], "blocked")
                observations = report["observations"]
                self.assertEqual(
                    observations["provisioning-profile"]["status"], "blocked"
                )
                self.assertEqual(
                    observations["device-readiness"]["status"], "not-checked"
                )
                self.assertEqual(
                    observations["artifact-stability"]["status"], "verified"
                )
                self.assertFalse(
                    any("devicectl" in command for command in self.commands)
                )

    def test_wildcard_profile_requires_exact_signed_app_identifier(self) -> None:
        for permitted in (f"{self.team}.*", f"{self.team}.dev.*"):
            with self.subTest(permitted=permitted):
                self.profile["Entitlements"]["application-identifier"] = permitted
                self.save_profile()
                self.signed_entitlements["application-identifier"] = (
                    f"{self.team}.{self.bundle}"
                )
                report = self.observe()
                self.assertEqual(report["status"], "verified")
                for observation in report["observations"].values():
                    if observation["status"] == "verified":
                        self.assertNotEqual(
                            observation["detail"], "Prerequisite not verified."
                        )
                profile = report["observations"]["provisioning-profile"]
                self.assertEqual(
                    profile["app_id_match"],
                    "wildcard-profile-exact-signed-app",
                )
                self.assertEqual(profile["profile_application_identifier"], permitted)
                for signed in (permitted, f"{self.team}.dev.foreign"):
                    with self.subTest(signed=signed):
                        self.signed_entitlements["application-identifier"] = signed
                        self.assertEqual(self.observe()["status"], "blocked")

    def test_signature_rejection_metadata_team_mismatch_and_fingerprint_drift(
        self,
    ) -> None:
        for name in ("verify failure", "signer team", "drift"):
            with self.subTest(name=name):
                self.signature_rc = 1 if name == "verify failure" else 0
                self.signature_team = "Z" * 10 if name == "signer team" else self.team
                self.drift = name == "drift"
                report = self.observe()
                self.assertEqual(report["status"], "blocked")
                key = {
                    "verify failure": "signature",
                    "signer team": "provisioning-profile",
                    "drift": "artifact-stability",
                }[name]
                self.assertEqual(report["observations"][key]["status"], "blocked")

    def test_malformed_info_profile_simulator_and_linked_artifact_cannot_pass(
        self,
    ) -> None:
        original = plistlib.dumps(self.info)
        for update in (
            {"CFBundleExecutable": "../outside"},
            {"CFBundleIdentifier": "dev.foreign"},
            {"CFBundleSupportedPlatforms": ["iPhoneSimulator"]},
        ):
            with self.subTest(update=update):
                (self.app / "Info.plist").write_bytes(
                    plistlib.dumps({**self.info, **update})
                )
                self.assertEqual(self.observe()["status"], "blocked")
        (self.app / "Info.plist").write_bytes(original)
        for data in (b"not a decoded profile", b'<?xml version="1.0"?><plist><dict>'):
            (self.app / "embedded.mobileprovision").write_bytes(data)
            self.assertEqual(
                self.observe()["observations"]["provisioning-profile"]["status"],
                "blocked",
            )
        self.save_profile()
        (self.app / "linked").symlink_to(self.app / "Synthetic")
        self.assertEqual(
            self.observe()["observations"]["artifact"]["status"], "blocked"
        )

    def test_missing_inputs_and_required_unchecked_observations_never_pass(
        self,
    ) -> None:
        for arguments in (
            {"mode": "prebuilt", "app": "", "device": ""},
            {"mode": "prebuilt", "app": str(self.app), "device": "auto"},
            {"mode": "build-signing", "app": str(self.app)},
            {"mode": "unknown"},
        ):
            with self.subTest(arguments=arguments):
                self.commands.clear()
                report = preflight.preflight(**arguments)
                self.assertEqual(report["status"], "blocked")
                self.assertEqual(
                    report["observations"]["admission"]["status"], "blocked"
                )
                self.assertEqual(
                    report["observations"]["host"]["status"], "not-checked"
                )
                self.assertEqual(self.commands, [])

    def test_build_signing_reuses_verify_and_local_config_without_proving_private_key(
        self,
    ) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                preflight.main(
                    ["--mode", "build-signing", "--app", "", "--device", ""]
                ),
                0,
            )
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "verified")
        self.config.assert_called_once_with(
            "apple", "development_team", "BLEHUB_APPLE_DEVELOPMENT_TEAM", None
        )
        self.assertEqual(
            report["observations"]["private-key-access"]["status"], "not-checked"
        )
        self.assertFalse(any("devicectl" in command for command in self.commands))
        self.assertTrue(
            any(Path(command[0]).name == "xcodegen" for command in self.commands)
        )
        self.xcodegen_version = "0.0.0"
        self.assertEqual(
            preflight.preflight()["observations"]["xcodegen"]["status"], "blocked"
        )
        self.xcodegen_version = xcodegen_toolchain.VERSION
        self.identity_count = 0
        self.assertEqual(
            preflight.preflight()["observations"]["signing-identity"]["status"],
            "blocked",
        )
        self.identity_count = 1
        self.certificate_team = "Z" * 10
        self.assertEqual(
            preflight.preflight()["observations"]["signing-identity"]["status"],
            "blocked",
        )
        self.certificate_team = self.team
        self.config.return_value = ConfiguredValue(None, "unset")
        self.assertEqual(
            preflight.preflight()["observations"]["configured-team"]["status"],
            "blocked",
        )

    def test_certificate_ou_matches_despite_different_cn_suffix(self) -> None:
        self.identity_team = "6J7DUSCP6X"
        self.certificate_team = self.team
        report = preflight.preflight()
        self.assertEqual(report["status"], "verified")
        identity = report["observations"]["signing-identity"]
        self.assertEqual(identity["matching_development_identities"], 1)
        self.assertEqual(identity["team_match_source"], "certificate-subject-ou")
        self.assertIn("SHA-1", identity["detail"])
        self.assertEqual(
            report["observations"]["private-key-access"]["status"], "not-checked"
        )

    def test_cn_suffix_cannot_override_foreign_or_unusable_certificate_ou(self) -> None:
        self.identity_team = self.team
        subjects = (
            f"subject=\n    2.5.4.11={'Z' * 10}\n",
            f"subject=\n    2.5.4.3=Apple Development: Synthetic ({self.team})\n",
            "subject=\n    2.5.4.11=\n",
            "subject=\n    2.5.4.11=not-a-team\n",
            "subject=\n    2.5.4.11=abcdefghij\n",
            f"subject=\n    2.5.4.11={self.team},extra\n",
            f"subject=\n    2.5.4.11={self.team}\n    2.5.4.11={self.team}\n",
            f"subject=\n    2.5.4.11={self.team}\n    2.5.4.11={'Z' * 10}\n",
        )
        for subject in subjects:
            with self.subTest(subject=subject):
                self.subject_override = subject
                observation = preflight.preflight()["observations"]["signing-identity"]
                self.assertEqual(observation["status"], "blocked")
                self.assertIn("certificate", observation["detail"])

    def test_public_certificate_requires_matching_current_valid_identity(self) -> None:
        for count, fingerprint, output in (
            (0, self.identity_fingerprint, self.certificate_pem),
            (1, "0" * 40, self.certificate_pem),
            (1, self.identity_fingerprint, ""),
            (
                1,
                self.identity_fingerprint,
                "-----BEGIN CERTIFICATE-----\n!\n-----END CERTIFICATE-----\n",
            ),
        ):
            with self.subTest(count=count, fingerprint=fingerprint, output=output):
                self.identity_count = count
                self.identity_fingerprint = fingerprint
                self.certificate_output = output
                self.commands.clear()
                observation = preflight.preflight()["observations"]["signing-identity"]
                self.assertEqual(observation["status"], "blocked")
                self.assertFalse(
                    any(Path(c[0]).name == "openssl" for c in self.commands)
                )

    def test_fingerprint_correlation_ignores_unrelated_certificates_and_deduplicates(
        self,
    ) -> None:
        unrelated = ssl.DER_cert_to_PEM_cert(b"unrelated public DER")
        self.certificate_output = unrelated + self.certificate_pem * 2
        self.identity_fingerprint = self.identity_fingerprint.lower()
        observation = preflight.preflight()["observations"]["signing-identity"]
        self.assertEqual(observation["status"], "verified")
        self.assertEqual(observation["matching_development_identities"], 1)
        self.assertEqual(len(self.public_certificate_paths), 1)
        self.assertFalse(self.public_certificate_paths[0].exists())
        self.identity_kind = "Apple Distribution"
        self.commands.clear()
        self.assertEqual(
            preflight.preflight()["observations"]["signing-identity"]["status"],
            "blocked",
        )
        self.assertFalse(any(Path(c[0]).name == "openssl" for c in self.commands))

    def test_scoped_identity_teams_come_from_certificate_ou_not_cn_suffix(self) -> None:
        self.identity_team = "X" * 10
        keychain = "/synthetic/login.keychain-db"
        count, teams = signing_identity.visible_development_identities(
            "/synthetic/bin/security", "/synthetic/bin/openssl", keychain=keychain
        )
        self.assertEqual(count, 1)
        self.assertEqual(set(teams.values()), {self.team})
        security_queries = [
            c[1:] for c in self.commands if Path(c[0]).name == "security"
        ]
        self.assertIn(
            ["find-identity", "-v", "-p", "codesigning", keychain], security_queries
        )
        self.assertIn(
            ["find-certificate", "-a", "-c", "Apple Development", "-p", keychain],
            security_queries,
        )
        self.assertTrue(self.public_certificate_paths)
        self.assertTrue(all(not p.exists() for p in self.public_certificate_paths))

    def test_scoped_identity_rejects_invalid_ou_and_never_matches_unrelated_cert(
        self,
    ) -> None:
        self.subject_override = "subject=\n    2.5.4.3=CN only\n"
        with self.assertRaises(RuntimeError):
            signing_identity.visible_development_identities(
                "/synthetic/bin/security",
                "/synthetic/bin/openssl",
                keychain="/synthetic/login.keychain-db",
            )
        self.subject_override = None
        self.identity_fingerprint = "0" * 40
        count, teams = signing_identity.visible_development_identities(
            "/synthetic/bin/security",
            "/synthetic/bin/openssl",
            keychain="/synthetic/login.keychain-db",
        )
        self.assertEqual(count, 1)
        self.assertEqual(teams, {})

    def test_certificate_command_failures_block_and_remove_temporary_public_cert(
        self,
    ) -> None:
        for failure in ("timeout", "nonzero"):
            with self.subTest(failure=failure):
                self.timeout_command = "openssl" if failure == "timeout" else None
                self.command_failure = "openssl" if failure == "nonzero" else None
                observation = preflight.preflight()["observations"]["signing-identity"]
                self.assertEqual(observation["status"], "blocked")
                self.assertTrue(self.public_certificate_paths)
                self.assertTrue(
                    all(not p.exists() for p in self.public_certificate_paths)
                )
        self.timeout_command = "security"
        self.command_failure = None
        self.assertEqual(
            preflight.preflight()["observations"]["signing-identity"]["status"],
            "blocked",
        )
        self.timeout_command = None
        self.command_failure = "security"
        self.assertEqual(
            preflight.preflight()["observations"]["signing-identity"]["status"],
            "blocked",
        )

    def test_command_timeout_blocks_and_cli_emits_only_json_with_failure_exit(
        self,
    ) -> None:
        self.timeout_command = "codesign"
        output = io.StringIO()
        with redirect_stdout(output):
            code = preflight.main(
                ["--mode", "prebuilt", "--app", str(self.app), "--device", ""]
            )
        self.assertEqual(code, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["observations"]["signature"]["status"], "blocked")
        self.assertEqual(
            report["observations"]["provisioning-profile"]["status"], "not-checked"
        )


if __name__ == "__main__":
    unittest.main()
