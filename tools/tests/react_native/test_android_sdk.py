from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.android.state_control import AndroidRadioController
from tools.react_native import android_sdk, bleng_scan_host


class AndroidSdkCommandTests(unittest.TestCase):
    def test_adb_command_uses_selected_serial_and_preserves_arguments(self) -> None:
        with patch.object(
            android_sdk, "adb_executable", return_value=Path("/sdk/platform-tools/adb")
        ):
            self.assertEqual(
                android_sdk.adb_command("phone", "shell", "pidof", "com.example"),
                [
                    "/sdk/platform-tools/adb",
                    "-s",
                    "phone",
                    "shell",
                    "pidof",
                    "com.example",
                ],
            )

    def test_run_command_bounds_execution_and_combines_stderr(self) -> None:
        completed = subprocess.CompletedProcess(["tool"], 0, "output")
        with patch.object(android_sdk.subprocess, "run", return_value=completed) as run:
            result = android_sdk.run_command(
                ["tool"], cwd=Path("/work"), timeout=3.5, check=True
            )
        self.assertIs(result, completed)
        run.assert_called_once_with(
            ["tool"],
            cwd=Path("/work"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=3.5,
        )

    def test_run_command_rejects_nonzero_result_with_bounded_output(self) -> None:
        with (
            patch.object(
                android_sdk.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 7, "failure"),
            ),
            self.assertRaisesRegex(android_sdk.AndroidSdkError, "exit=7"),
        ):
            android_sdk.run_command(["tool"], timeout=1, check=True)


class AndroidInstallTests(unittest.TestCase):
    def test_explicit_sdk_modes_reach_real_installer_subprocess_boundary(self):
        for root in (False, True):
            with self.subTest(root=root), tempfile.TemporaryDirectory() as directory:
                apk = Path(directory) / "fixture.apk"
                apk.write_bytes(b"fixture")
                calls = []
                guards = []

                def command(
                    argv, *, calls=calls, guards=guards, directory=directory, **kwargs
                ):
                    calls.append(argv)
                    self.assertEqual(len(guards), 2 * len(calls) - 1)
                    self.assertEqual(argv[:3], ["/sdk/adb", "-s", "synthetic"])
                    self.assertEqual(kwargs["cwd"], Path(directory))
                    self.assertLessEqual(kwargs["timeout"], 2.5)
                    output = "Success\n" if "install" in " ".join(argv) else ""
                    return subprocess.CompletedProcess(argv, 0, output)

                with (
                    patch.object(
                        android_sdk, "adb_executable", return_value=Path("/sdk/adb")
                    ),
                    patch.object(android_sdk.time, "monotonic", return_value=100.0),
                    patch.object(android_sdk.subprocess, "run", side_effect=command),
                ):
                    selected = android_sdk.install_apk(
                        "synthetic",
                        apk,
                        log=io.StringIO(),
                        use_root=root,
                        deadline=102.5,
                        require_owner=lambda guards=guards: guards.append("guard"),
                        cwd=Path(directory),
                    )
                self.assertIs(selected, root)
                self.assertEqual(len(guards), 2 * len(calls))
                self.assertEqual(len(calls), 5 if root else 3)
                self.assertFalse(any("-g" in " ".join(c).split() for c in calls))
                self.assertEqual(sum(c[3:4] == ["push"] for c in calls), 1)
                self.assertEqual(
                    sum(c[3:6] == ["shell", "su", "-c"] for c in calls), int(root)
                )

    def test_guarded_auto_detection_is_refused_without_device_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "fixture.apk"
            apk.write_bytes(b"fixture")
            with patch.object(android_sdk.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "explicit use_root"):
                    android_sdk.install_apk(
                        "synthetic", apk, log=io.StringIO(), deadline=102.0
                    )
                run.assert_not_called()

    def test_legacy_explicit_nonroot_client_keeps_unbounded_owner_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "fixture.apk"
            apk.write_bytes(b"fixture")
            with (
                patch.object(
                    android_sdk, "adb_executable", return_value=Path("/sdk/adb")
                ),
                patch.object(
                    android_sdk.subprocess,
                    "run",
                    side_effect=[
                        subprocess.CompletedProcess([], 0, ""),
                        subprocess.CompletedProcess([], 0, "Success\n"),
                        subprocess.CompletedProcess([], 0, ""),
                    ],
                ) as run,
            ):
                self.assertIs(
                    android_sdk.install_apk(
                        "synthetic", apk, use_root=False, log=io.StringIO()
                    ),
                    False,
                )
            self.assertEqual(
                [c.kwargs["timeout"] for c in run.call_args_list], [60, 90, 15]
            )
            self.assertTrue(all("su" not in c.args[0] for c in run.call_args_list))


class AndroidPermissionTests(unittest.TestCase):
    def test_api_31_grants_bluetooth_permissions_without_location(self) -> None:
        calls: list[list[str]] = []

        def command(
            arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            if "getprop" in arguments:
                return subprocess.CompletedProcess(arguments, 0, "34\n")
            if "dumpsys" in arguments:
                return subprocess.CompletedProcess(arguments, 0, "permission state\n")
            return subprocess.CompletedProcess(arguments, 0, "")

        with (
            patch.object(android_sdk, "run_command", side_effect=command),
            patch.object(android_sdk, "adb_executable", return_value=Path("/adb")),
            patch.object(android_sdk, "has_root", return_value=False),
        ):
            android_sdk.grant_bluetooth_permissions("phone")

        grants = [" ".join(arguments) for arguments in calls if "grant" in arguments]
        self.assertEqual(len(grants), 2)
        self.assertTrue(any("BLUETOOTH_SCAN" in grant for grant in grants))
        self.assertTrue(any("BLUETOOTH_CONNECT" in grant for grant in grants))
        self.assertFalse(any("ACCESS_FINE_LOCATION" in grant for grant in grants))

    def test_api_30_uses_root_when_available_and_skips_granted_permission(self) -> None:
        calls: list[list[str]] = []

        def command(
            arguments: list[str], **_: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            if "getprop" in arguments:
                return subprocess.CompletedProcess(arguments, 0, "30\n")
            if "dumpsys" in arguments:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    "android.permission.ACCESS_FINE_LOCATION: granted=true\n",
                )
            self.fail(f"unexpected command: {arguments}")

        with (
            patch.object(android_sdk, "run_command", side_effect=command),
            patch.object(android_sdk, "adb_executable", return_value=Path("/adb")),
            patch.object(android_sdk, "has_root", return_value=True) as has_root,
        ):
            android_sdk.grant_bluetooth_permissions("phone")

        has_root.assert_called_once_with(Path("/adb"), "phone")
        self.assertFalse(any("grant" in arguments for arguments in calls))

    def test_invalid_api_level_fails_before_permission_mutation(self) -> None:
        with (
            patch.object(
                android_sdk,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "unknown\n"),
            ),
            self.assertRaisesRegex(
                android_sdk.AndroidSdkError, "invalid Android device API"
            ),
        ):
            android_sdk.grant_bluetooth_permissions("phone")


class _IdentitySdkCase(unittest.TestCase):
    serial = "synthetic-phone"
    package = "com.blehubblenghost"
    digest = "ab" * 32
    boot = "11111111-2222-4333-8444-555555555555"
    apk = "/data/app/~~token==/com.blehubblenghost-token==/base.apk"

    def setUp(self) -> None:
        self.clock = 100.0
        self.events: list[object] = []
        self.calls: list[tuple[str, ...]] = []
        self.overrides: dict[int, object] = {}
        self.enterContext(
            patch.object(
                android_sdk.subprocess,
                "run",
                side_effect=AssertionError(
                    "real subprocess forbidden in identity tests"
                ),
            )
        )
        self.enterContext(
            patch.object(android_sdk, "adb_executable", return_value=Path("/adb"))
        )
        self.enterContext(patch("time.monotonic", side_effect=lambda: self.clock))
        self.command = self.enterContext(
            patch.object(android_sdk, "run_command", side_effect=self.sdk_command)
        )

    def stat(self, ticks: int = 700) -> str:
        # Field 22 follows the comm field (which can contain spaces and ')').
        return "321 (app ) worker) S " + "0 " * 18 + f"{ticks} 0\n"

    def sdk_command(
        self, args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.assertEqual(args[:4], ["/adb", "-s", self.serial, "shell"])
        self.assertEqual(kwargs["cwd"], Path("/work"))
        self.assertTrue(kwargs["check"])
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 105.0 - self.clock)
        command = tuple(args[4:])
        self.calls.append(command)
        self.events.append(command)
        outputs = {
            ("pidof", self.package): "321\n",
            ("pm", "path", self.package): f"package:{self.apk}\n",
            ("cat", "/proc/321/stat"): self.stat(),
            (
                "cat",
                "/proc/321/status",
            ): "Name:\tapp\nPid:\t321\nUid:\t10123\t10123\t10123\t10123\n",
            ("cat", "/proc/321/cmdline"): self.package + "\0",
            ("cat", "/proc/sys/kernel/random/boot_id"): self.boot + "\n",
            ("sha256sum", self.apk): f"{self.digest}  {self.apk}\n",
        }
        self.assertIn(command, outputs, "unexpected or mutating device command")
        value = self.overrides.get(len(self.calls), outputs[command])
        self.clock += 0.01
        if isinstance(value, Exception):
            raise value
        if isinstance(value, subprocess.CompletedProcess):
            return value
        return subprocess.CompletedProcess(args, 0, value)

    def owner(self) -> None:
        self.events.append("owner")

    def read(self, **changes: object):
        args = {
            "serial": self.serial,
            "package": self.package,
            "expected_apk_sha256": self.digest,
            "expected_identity": None,
            "cwd": Path("/work"),
            "deadline": 105.0,
            "require_owner": self.owner,
        }
        args.update(changes)
        return android_sdk.read_app_identity(**args)


class AndroidAppIdentityTests(_IdentitySdkCase):
    def test_snapshot_is_frozen_and_uses_only_guarded_readonly_commands(self) -> None:
        identity = self.read()
        self.assertEqual(
            identity,
            android_sdk.AndroidAppIdentity(
                serial=self.serial,
                package=self.package,
                pid=321,
                uid=10123,
                cmdline=self.package,
                start_ticks=700,
                boot_id=self.boot,
                base_apk_path=self.apk,
                apk_sha256=self.digest,
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            identity.pid = 999
        self.assertNotIn("native_started_unix_ms", identity.__dataclass_fields__)
        self.assertEqual(len(self.calls), 13)
        for index, event in enumerate(self.events):
            if isinstance(event, tuple):
                self.assertEqual(self.events[index - 1], "owner")
                self.assertEqual(self.events[index + 1], "owner")
        budgets = [call.kwargs["timeout"] for call in self.command.call_args_list]
        self.assertTrue(all(a > b for a, b in pairwise(budgets)))

    def test_cmdline_allows_only_trailing_nul_padding_in_both_sweeps(self) -> None:
        expected = self.read()
        for forward, reverse in ((1, 1), (81, 81), (81, 1), (1, 81)):
            with self.subTest(forward=forward, reverse=reverse):
                self.calls.clear()
                self.clock = 100.0
                self.overrides = {
                    6: self.package + "\0" * forward,
                    8: self.package + "\0" * reverse,
                }
                identity = self.read(expected_identity=expected)
                self.assertEqual(identity, expected)
                self.assertEqual(identity.cmdline, self.package)
                self.assertEqual(len(self.calls), 13)
                self.assertEqual(self.calls[5], ("cat", "/proc/321/cmdline"))
                self.assertEqual(self.calls[7], ("cat", "/proc/321/cmdline"))

    def test_cmdline_padding_never_admits_foreign_argv_or_missing_terminator(
        self,
    ) -> None:
        invalid = (
            "",
            "\0" * 81,
            self.package,
            self.package + "\n",
            self.package + "\n\0",
            self.package + "\0\n",
            self.package + "\0\n\0",
            "prefix." + self.package + "\0",
            self.package + "suffix\0",
            self.package + ":worker" + "\0" * 81,
            self.package + "\0extra\0",
            self.package + "\0" * 81 + "extra\0",
            self.package + "\0 \0",
            "\0" + self.package + "\0",
        )
        for index in (6, 8):
            for output in invalid:
                with self.subTest(index=index, output=output):
                    self.calls.clear()
                    self.clock = 100.0
                    self.overrides = {index: output}
                    with self.assertRaisesRegex(android_sdk.AndroidSdkError, "cmdline"):
                        self.read()
                    self.assertEqual(len(self.calls), index)

    def test_invalid_explicit_inputs_fail_without_sdk(self) -> None:
        for changes in (
            {"serial": ""},
            {"serial": "phone;bad"},
            {"package": "com.example;bad"},
            {"package": "com..example"},
            {"expected_apk_sha256": "a" * 63},
            {"expected_apk_sha256": "AB" * 32},
            {"deadline": float("nan")},
            {"deadline": float("inf")},
            {"deadline": True},
            {"require_owner": None},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises((ValueError, TypeError)),
            ):
                self.read(**changes)
        self.command.assert_not_called()

    def test_ambiguous_or_malformed_readback_fails_closed(self) -> None:
        bad = {
            1: ("", "not-a-boot", self.boot + "\n" + self.boot),
            2: ("", "321 322\n", "321\n321\n", "0321\n", "denied", "9" * 5000),
            3: (
                "",
                f"package:{self.apk}\npackage:/data/app/split.apk\n",
                "package:/data/app/app/split_config.apk\n",
                "package:/data/app/a/../base.apk\n",
                "package:/data/app/a;bad/base.apk\n",
            ),
            4: (
                "permission denied",
                self.stat(0),
                self.stat().replace("321", "322", 1),
            ),
            5: (
                "Uid: 10123 10123 10123 10123\n",
                "Pid: 321\nUid: 10123 10124 10123 10123\n",
                "Pid: 321\nUid: 0 0 0 0\n",
                "Pid: 321\nUid: 10123 10123 10123 10123\nUid: 10123 10123 10123 10123\n",
                "Pid: 321\nUid: 10123 10123 10123 10123\nUid: denied\n",
            ),
            6: (
                "",
                self.package,
                self.package + ":worker\0",
                self.package + "\0extra\0",
            ),
            7: (
                f"{'cd' * 32}  {self.apk}\n",
                f"{self.digest}  /data/app/other/base.apk\n",
                f"{self.digest}  {self.apk}\nextra\n",
                f"SHA256 ({self.apk}) = {self.digest}\n",
                "Permission denied\n",
            ),
        }
        for index, values in bad.items():
            for value in values:
                with self.subTest(index=index, value=value):
                    self.calls.clear()
                    self.clock = 100.0
                    self.overrides = {index: value}
                    with self.assertRaises(android_sdk.AndroidSdkError):
                        self.read()
                    self.assertEqual(len(self.calls), index)

    def test_second_sweep_rejects_process_boot_install_or_uid_drift(self) -> None:
        for index, output in (
            (8, "com.foreign\0"),
            (9, "Pid: 321\nUid: 10124 10124 10124 10124\n"),
            (10, self.stat(701)),
            (11, "package:/data/app/reinstalled/base.apk\n"),
            (12, "322\n"),
            (13, "22222222-2222-4333-8444-555555555555\n"),
        ):
            with self.subTest(index=index):
                self.calls.clear()
                self.clock = 100.0
                self.overrides = {index: output}
                with self.assertRaises(android_sdk.AndroidSdkError):
                    self.read()

    def test_command_failure_runs_post_owner_and_never_falls_back(self) -> None:
        for index in range(1, 14):
            with self.subTest(index=index):
                self.calls.clear()
                self.events.clear()
                self.clock = 100.0
                self.overrides = {index: android_sdk.AndroidSdkError("procfs denied")}
                with self.assertRaisesRegex(
                    android_sdk.AndroidSdkError, "procfs denied"
                ):
                    self.read()
                self.assertEqual(len(self.calls), index)
                self.assertEqual(self.events[-1], "owner")
        self.calls.clear()
        self.overrides = {1: subprocess.CompletedProcess([], 1, self.boot + "\n")}
        with self.assertRaises(android_sdk.AndroidSdkError):
            self.read()

    def test_pinned_request_mismatch_fails_before_any_command(self) -> None:
        identity = self.read()
        self.calls.clear()
        for change in (
            {"serial": "another-phone"},
            {"package": "com.foreign"},
            {"apk_sha256": "cd" * 32},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.read(expected_identity=replace(identity, **change))
        self.assertEqual(self.calls, [])

    def test_owner_loss_before_and_after_each_command_aborts(self) -> None:
        for stop in range(1, 27):
            with self.subTest(stop=stop):
                self.calls.clear()
                self.clock = 100.0
                count = 0

                def owner(stop: int = stop) -> None:
                    nonlocal count
                    count += 1
                    if count == stop:
                        raise RuntimeError("owner lost")

                with self.assertRaisesRegex(RuntimeError, "owner lost"):
                    self.read(require_owner=owner)
                self.assertEqual(len(self.calls), stop // 2)

    def test_owner_and_command_time_both_consume_one_deadline(self) -> None:
        def owner() -> None:
            self.clock += 6

        with self.assertRaises(TimeoutError):
            self.read(require_owner=owner)
        self.command.assert_not_called()
        self.clock = 104.995
        with self.assertRaises(TimeoutError):
            self.read()
        self.assertEqual(len(self.calls), 1)
        self.clock = 106.0
        with self.assertRaises(TimeoutError):
            self.read()
        self.assertEqual(len(self.calls), 1)


class AndroidAppBindingTests(_IdentitySdkCase):
    # Reuse the strict SDK fake; binding tests never mock the identity reader.
    def setUp(self) -> None:
        super().setUp()
        self.driver = SimpleNamespace(
            session_id="webdriver-session",
            capabilities={
                "platformName": "Android",
                "appium:udid": self.serial,
                "appium:appPackage": self.package,
            },
        )
        self.phone = object.__new__(AndroidRadioController)
        self.phone.device = self.serial
        self.phone.run_id = "11111111-2222-4333-8444-555555555555"
        self.phone.document = {"device": self.serial, "run_id": self.phone.run_id}
        self.owner_mock = self.enterContext(
            patch.object(self.phone, "require_owner", side_effect=self.owner)
        )
        self.candidate = Mock(return_value=self.digest)
        self.protocol_owner = Mock()

    def pin(self, **changes: object):
        args = {
            "driver": self.driver,
            "phone": self.phone,
            "serial": self.serial,
            "run_id": self.phone.run_id,
            "app_id": self.package,
            "expected_apk_sha256": self.digest,
            "read_candidate_sha256": self.candidate,
            "require_owner": self.protocol_owner,
            "cwd": Path("/work"),
            "deadline": 105.0,
        }
        args.update(changes)
        return bleng_scan_host.pin_android_app(**args)

    def current(self, binding, **changes: object) -> None:
        args = {
            "driver": self.driver,
            "phone": self.phone,
            "serial": self.serial,
            "run_id": self.phone.run_id,
            "app_id": self.package,
            "expected_apk_sha256": self.digest,
        }
        args.update(changes)
        binding.require_current(**args)

    def test_callable_rereads_identity_and_never_reselects(self) -> None:
        binding = self.pin()
        pinned = binding.identity
        binding()
        self.current(binding)
        self.assertEqual(len(self.calls), 39)
        self.assertIs(binding.identity, pinned)
        for changed in (
            {"driver": SimpleNamespace(**vars(self.driver))},
            {"phone": object.__new__(AndroidRadioController)},
            {"serial": "other-phone"},
            {"run_id": "other-run"},
            {"app_id": "com.other"},
            {"expected_apk_sha256": "cd" * 32},
        ):
            with self.subTest(changed=changed):
                before = len(self.calls)
                with self.assertRaises(RuntimeError):
                    self.current(binding, **changed)
                self.assertEqual(len(self.calls), before)
        with self.assertRaises(FrozenInstanceError):
            binding.identity = replace(pinned, pid=322)

    def test_restarts_pid_reuse_candidate_and_device_rebind_are_rejected(self) -> None:
        binding = self.pin()
        for index, output in (
            (2, "322\n"),
            (4, self.stat(701)),
            (1, "22222222-2222-4333-8444-555555555555\n"),
            (3, "package:/data/app/reinstalled/base.apk\n"),
        ):
            with self.subTest(index=index):
                self.calls.clear()
                self.overrides = {index: output}
                with self.assertRaises(RuntimeError):
                    binding()
        self.overrides.clear()
        self.candidate.return_value = "cd" * 32
        before = len(self.calls)
        with self.assertRaisesRegex(RuntimeError, "candidate"):
            binding()
        self.assertEqual(len(self.calls), before)
        self.candidate.return_value = self.digest
        for mutate, restore in (
            (
                lambda: setattr(self.driver, "session_id", "new-session"),
                lambda: setattr(self.driver, "session_id", "webdriver-session"),
            ),
            (
                lambda: setattr(self.phone, "device", "other"),
                lambda: setattr(self.phone, "device", self.serial),
            ),
            (
                lambda: self.phone.document.update(run_id="other"),
                lambda: self.phone.document.update(run_id=self.phone.run_id),
            ),
            (
                lambda: self.driver.capabilities.update({"appium:udid": "other"}),
                lambda: self.driver.capabilities.update({"appium:udid": self.serial}),
            ),
            (
                lambda: self.driver.capabilities.update(
                    {"appium:appPackage": "com.other"}
                ),
                lambda: self.driver.capabilities.update(
                    {"appium:appPackage": self.package}
                ),
            ),
        ):
            mutate()
            with self.assertRaises(RuntimeError):
                binding()
            restore()

    def test_pin_requires_explicit_driver_phone_run_and_candidate_agreement(
        self,
    ) -> None:
        for changes in (
            {"serial": "other"},
            {"run_id": "other"},
            {"app_id": "com.other"},
            {"expected_apk_sha256": "cd" * 32},
            {"driver": SimpleNamespace(session_id=None, capabilities={})},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises((RuntimeError, ValueError)),
            ):
                self.pin(**changes)
        self.command.assert_not_called()

    def test_candidate_or_driver_drift_inside_sdk_read_aborts_immediately(self) -> None:
        binding = self.pin()
        for boundary in range(1, 14):
            with self.subTest(boundary=boundary):
                self.calls.clear()
                self.clock = 100.0
                self.candidate.return_value = self.digest

                def command(
                    args: list[str], *, boundary: int = boundary, **kwargs: object
                ) -> subprocess.CompletedProcess[str]:
                    result = self.sdk_command(args, **kwargs)
                    if len(self.calls) == boundary:
                        self.candidate.return_value = "cd" * 32
                    return result

                self.command.side_effect = command
                with self.assertRaisesRegex(RuntimeError, "candidate"):
                    binding()
                self.assertEqual(len(self.calls), boundary)
        self.candidate.return_value = self.digest
        self.calls.clear()

        def rebound(args: list[str], **kwargs: object):
            result = self.sdk_command(args, **kwargs)
            self.driver.session_id = "rebound-session"
            return result

        self.command.side_effect = rebound
        with self.assertRaisesRegex(RuntimeError, "session"):
            binding()
        self.assertEqual(len(self.calls), 1)

    def test_missing_conflicting_or_foreign_capabilities_fail_before_sdk(self) -> None:
        original = self.driver.capabilities.copy()
        for caps in (
            {},
            {**original, "platformName": "iOS"},
            {**original, "platformName": None},
            {**original, "udid": "other"},
            {**original, "appPackage": "com.other"},
            {key: value for key, value in original.items() if key != "appium:udid"},
            {
                key: value
                for key, value in original.items()
                if key != "appium:appPackage"
            },
        ):
            with self.subTest(caps=caps):
                self.driver.capabilities = caps
                with self.assertRaises(RuntimeError):
                    self.pin()
        self.command.assert_not_called()

    def test_protocol_owner_loss_after_sdk_read_is_not_ignored(self) -> None:
        binding = self.pin()
        self.assertGreaterEqual(self.protocol_owner.call_count, 26)
        self.calls.clear()
        error = RuntimeError("protocol owner lost")

        def command(args: list[str], **kwargs: object):
            result = self.sdk_command(args, **kwargs)
            self.protocol_owner.side_effect = error
            return result

        self.command.side_effect = command
        with self.assertRaises(RuntimeError) as caught:
            binding()
        self.assertIs(caught.exception, error)
        self.assertEqual(len(self.calls), 1)

    def test_binding_does_not_refresh_total_deadline(self) -> None:
        binding = self.pin()
        self.clock = 106.0
        before = len(self.calls)
        with self.assertRaises(TimeoutError):
            binding()
        self.assertEqual(len(self.calls), before)


if __name__ == "__main__":
    unittest.main()
