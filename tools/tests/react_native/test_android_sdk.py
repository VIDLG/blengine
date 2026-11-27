"""No-device contracts for locating and invoking the Android SDK toolchain."""

from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from itertools import pairwise
from pathlib import Path
from unittest.mock import Mock, patch

from tools.android import adb
from tools.react_native import android_sdk


class AndroidSdkBoundaryTests(unittest.TestCase):
    def test_sdk_helpers_expose_the_shared_adb_contract(self) -> None:
        self.assertIs(android_sdk.adb_command, adb.adb_command)
        self.assertTrue(issubclass(android_sdk.AndroidSdkError, adb.AdbError))


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
                    patch.object(adb, "adb_executable", return_value=Path("/sdk/adb")),
                    patch.object(android_sdk.time, "monotonic", return_value=100.0),
                    patch.object(adb, "run_command", side_effect=command),
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
            with patch.object(adb, "run_command") as run:
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
                patch.object(adb, "adb_executable", return_value=Path("/sdk/adb")),
                patch.object(
                    adb,
                    "run_command",
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
            patch.object(adb, "run_command", side_effect=command),
            patch.object(adb, "adb_executable", return_value=Path("/adb")),
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
            patch.object(adb, "run_command", side_effect=command),
            patch.object(adb, "adb_executable", return_value=Path("/adb")),
            patch.object(android_sdk, "has_root", return_value=True) as has_root,
        ):
            android_sdk.grant_bluetooth_permissions("phone")

        has_root.assert_called_once_with("phone")
        self.assertFalse(any("grant" in arguments for arguments in calls))

    def test_invalid_api_level_fails_before_permission_mutation(self) -> None:
        with (
            patch.object(
                adb,
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
            patch.object(adb, "adb_executable", return_value=Path("/adb"))
        )
        self.enterContext(patch("time.monotonic", side_effect=lambda: self.clock))
        self.command = self.enterContext(
            patch.object(adb, "run_command", side_effect=self.sdk_command)
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


class AndroidIdentityCommandAuditTests(_IdentitySdkCase):
    def setUp(self) -> None:
        self.clock = 100.0
        self.events: list[object] = []
        self.calls: list[tuple[str, ...]] = []
        self.overrides: dict[int, object] = {}
        self.receipts: list[dict] = []
        self.snapshot = [
            (("cat", "/proc/sys/kernel/random/boot_id"), self.boot + "\n"),
            (("pidof", self.package), "321\n"),
            (("pm", "path", self.package), f"package:{self.apk}\n"),
            (("cat", "/proc/321/stat"), self.stat()),
            (("cat", "/proc/321/status"), "Pid: 321\nUid: 10123 10123 10123 10123\n"),
            (("cat", "/proc/321/cmdline"), self.package + "\0"),
            (("sha256sum", self.apk), f"{self.digest}  {self.apk}\n"),
            (("cat", "/proc/321/cmdline"), self.package + "\0"),
            (("cat", "/proc/321/status"), "Pid: 321\nUid: 10123 10123 10123 10123\n"),
            (("cat", "/proc/321/stat"), self.stat()),
            (("pm", "path", self.package), f"package:{self.apk}\n"),
            (("pidof", self.package), "321\n"),
            (("cat", "/proc/sys/kernel/random/boot_id"), self.boot + "\n"),
        ]
        self.enterContext(
            patch.object(adb, "adb_executable", return_value=Path("/adb"))
        )
        self.monotonic = self.enterContext(
            patch.object(android_sdk.time, "monotonic", side_effect=lambda: self.clock)
        )
        self.command = self.enterContext(
            patch.object(adb, "run_command", side_effect=self.subprocess_read)
        )

    def subprocess_read(self, args: list[str], **kwargs: object):
        index = len(self.calls)
        command, output = self.snapshot[index]
        self.assertEqual(args, ["/adb", "-s", self.serial, "shell", *command])
        self.assertEqual(set(kwargs), {"cwd", "check", "timeout"})
        self.assertEqual(kwargs["cwd"], Path("/work"))
        self.assertEqual(kwargs["timeout"], 105.0 - self.clock)
        self.calls.append(command)
        self.events.append(command)
        self.clock += 0.01
        value = self.overrides.get(index + 1, output)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, subprocess.CompletedProcess):
            return value
        return subprocess.CompletedProcess(args, 0, value)

    def record(self, receipt: dict) -> None:
        self.events.append("record")
        self.receipts.append(receipt)

    def test_full_pinned_snapshot_records_exact_commands_inside_owner_brackets(self):
        expected = android_sdk.AndroidAppIdentity(
            serial=self.serial,
            package=self.package,
            pid=321,
            uid=10123,
            cmdline=self.package,
            start_ticks=700,
            boot_id=self.boot,
            base_apk_path=self.apk,
            apk_sha256=self.digest,
        )
        self.assertEqual(
            self.read(expected_identity=expected, record_command=self.record), expected
        )
        self.assertEqual(self.calls, [command for command, _ in self.snapshot])
        self.assertEqual(len(self.receipts), 13)
        for index, ((command, stdout), receipt) in enumerate(
            zip(self.snapshot, self.receipts, strict=True)
        ):
            self.assertEqual(
                self.events[4 * index : 4 * index + 4],
                ["owner", command, "record", "owner"],
            )
            self.assertEqual(
                set(receipt),
                {
                    "argv",
                    "returncode",
                    "stdout",
                    "error",
                    "started_monotonic",
                    "finished_monotonic",
                },
            )
            self.assertEqual(
                receipt["argv"], ["/adb", "-s", self.serial, "shell", *command]
            )
            self.assertEqual(receipt["returncode"], 0)
            self.assertEqual(receipt["stdout"], stdout)
            self.assertIsNone(receipt["error"])
            self.assertIsInstance(receipt["started_monotonic"], float)
            self.assertAlmostEqual(receipt["started_monotonic"], 100.0 + index * 0.01)
            self.assertAlmostEqual(
                receipt["finished_monotonic"], 100.0 + (index + 1) * 0.01
            )

    def test_omitted_and_explicit_none_keep_original_cadence(self):
        original = self.read()
        cadence = self.monotonic.call_count
        events = self.events.copy()
        self.calls.clear()
        self.events.clear()
        self.clock = 100.0
        self.monotonic.reset_mock()
        self.assertEqual(self.read(record_command=None), original)
        self.assertEqual(self.monotonic.call_count, cadence)
        self.assertEqual(self.events, events)
        self.assertEqual(cadence, 4 * 13)

    def test_pinned_pid_mismatch_records_readback_without_reselection(self):
        expected = self.read()
        self.calls.clear()
        self.events.clear()
        self.clock = 100.0
        self.overrides = {2: "322\n"}
        with self.assertRaisesRegex(
            android_sdk.AndroidSdkError, "refusing to reselect"
        ):
            self.read(expected_identity=expected, record_command=self.record)
        self.assertEqual(len(self.receipts), 2)
        self.assertEqual(self.receipts[-1]["stdout"], "322\n")
        self.assertIsNone(self.receipts[-1]["error"])
        self.assertEqual(self.events[-2:], ["record", "owner"])

    def test_timeout_records_partial_bytes_and_preserves_sdk_cause(self):
        timeout = subprocess.TimeoutExpired(["/adb"], 5, output=b"partial\xff\n")
        self.overrides = {1: timeout}
        with self.assertRaises(android_sdk.AndroidSdkError) as raised:
            self.read(record_command=self.record)
        self.assertIs(raised.exception.__cause__, timeout)
        self.assertEqual(len(self.receipts), 1)
        receipt = self.receipts[0]
        self.assertIsNone(receipt["returncode"])
        self.assertEqual(receipt["stdout"], "partial\ufffd\n")
        self.assertIn(str(raised.exception), receipt["error"])
        self.assertIn("TimeoutExpired", receipt["error"])
        self.assertEqual(self.events[-2:], ["record", "owner"])

    def test_nonzero_and_start_failure_record_available_evidence(self):
        for value, returncode, stdout in (
            (
                subprocess.CompletedProcess([], 7, "procfs denied\n"),
                7,
                "procfs denied\n",
            ),
            (OSError("adb unavailable"), None, ""),
        ):
            with self.subTest(value=value):
                self.calls.clear()
                self.events.clear()
                self.receipts.clear()
                self.clock = 100.0
                self.overrides = {1: value}
                with self.assertRaises(android_sdk.AndroidSdkError) as raised:
                    self.read(record_command=self.record)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.receipts[0]["returncode"], returncode)
                self.assertEqual(self.receipts[0]["stdout"], stdout)
                self.assertIn(str(raised.exception), self.receipts[0]["error"])
                if returncode is not None:
                    self.assertEqual(
                        str(raised.exception),
                        "command failed (exit=7): /adb -s synthetic-phone shell "
                        "cat /proc/sys/kernel/random/boot_id; procfs denied\n",
                    )
                    self.assertIsNone(raised.exception.__cause__)
                else:
                    self.assertIs(raised.exception.__cause__, value)
                self.assertEqual(self.events[-2:], ["record", "owner"])

    def test_oversize_is_bounded_in_bytes_and_explicit_even_on_sdk_failure(self):
        output = "\u20ac" * (64 * 1024)
        for returncode in (0, 7):
            with self.subTest(returncode=returncode):
                self.calls.clear()
                self.receipts.clear()
                self.clock = 100.0
                self.overrides = {
                    1: subprocess.CompletedProcess([], returncode, output)
                }
                with self.assertRaises(android_sdk.AndroidSdkError) as raised:
                    self.read(record_command=self.record)
                self.assertEqual(len(self.calls), 1)
                receipt = self.receipts[0]
                self.assertLessEqual(len(receipt["stdout"].encode("utf-8")), 64 * 1024)
                self.assertTrue(output.startswith(receipt["stdout"]))
                self.assertIn("oversized", receipt["error"])
                self.assertIn("truncat", receipt["error"])
                self.assertIn(
                    "exit=7" if returncode else "oversized", str(raised.exception)
                )

    def test_recorder_failure_fails_closed_and_still_checks_owner(self):
        failure = RuntimeError("evidence disk full")

        def record(receipt):
            self.record(receipt)
            raise failure

        with self.assertRaises(android_sdk.AndroidSdkError) as raised:
            self.read(record_command=record)
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.events[-2:], ["record", "owner"])

    def test_sdk_primary_survives_publication_and_post_boundary_failures(self):
        for value in (
            subprocess.TimeoutExpired(["/adb"], 5, output=b"partial"),
            subprocess.CompletedProcess([], 7, "denied"),
        ):
            with self.subTest(value=value):
                self.calls.clear()
                self.events.clear()
                self.receipts.clear()
                self.clock = 100.0
                self.overrides = {1: value}

                def record(receipt):
                    self.record(receipt)
                    raise RuntimeError("publication failed")

                def owner():
                    self.owner()
                    if self.calls:
                        raise RuntimeError("owner lost")

                with self.assertRaises(android_sdk.AndroidSdkError) as raised:
                    self.read(record_command=record, require_owner=owner)
                self.assertIn(
                    "exit=7"
                    if isinstance(value, subprocess.CompletedProcess)
                    else "timed out",
                    str(raised.exception),
                )
                self.assertIs(
                    raised.exception.__cause__,
                    None if isinstance(value, subprocess.CompletedProcess) else value,
                )
                notes = "\n".join(raised.exception.__notes__)
                self.assertIn("publication failed", notes)
                self.assertIn("owner lost", notes)
                self.assertEqual(len(self.receipts), 1)
                self.assertEqual(self.events[-2:], ["record", "owner"])

    def test_publication_is_passive_and_cannot_bypass_post_owner_or_deadline(self):
        for loss in ("owner", "deadline", "none"):
            with self.subTest(loss=loss):
                self.calls.clear()
                self.events.clear()
                self.receipts.clear()
                self.clock = 100.0

                def record(receipt, loss=loss):
                    self.record(receipt)
                    receipt["argv"].clear()
                    receipt["stdout"] = "foreign"
                    if loss == "deadline":
                        self.clock = 106.0
                    return "replacement identity is ignored"

                def owner(loss=loss):
                    self.owner()
                    if self.receipts and loss == "owner":
                        raise RuntimeError("owner lost")

                if loss == "none":
                    self.assertEqual(
                        self.read(record_command=record, require_owner=owner).pid, 321
                    )
                    self.assertEqual(len(self.calls), 13)
                else:
                    with self.assertRaisesRegex(
                        TimeoutError if loss == "deadline" else RuntimeError,
                        "deadline" if loss == "deadline" else "owner lost",
                    ):
                        self.read(record_command=record, require_owner=owner)
                    self.assertEqual(len(self.calls), 1)

    def test_precommand_refusal_has_no_fabricated_receipt(self):
        with self.assertRaisesRegex(RuntimeError, "owner lost"):
            self.read(
                record_command=self.record,
                require_owner=Mock(side_effect=RuntimeError("owner lost")),
            )
        self.assertEqual(self.receipts, [])
        self.command.assert_not_called()
