"""Offline OS-evidence and deadline regressions; all ADB execution is mocked."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from tools.android import adb
from tools.android import permission_control as pc
from tools.tests.react_native.support import permission_os_samples as samples

RUN = "21d6a93c-fc94-47cd-9863-e76f654b3c67"
DEVICE = "permission-test-phone"
FORMAL_PACKAGE = "com.blehubformalhost"


class FakePhone:
    """Deterministic transport/clock, with optional ROM coupling and survival."""

    def __init__(self) -> None:
        self.now = 100.0
        self.scan = self.connect = True
        self.uid = samples.UID
        self.current_user = 0
        self.sdk = "36"
        self.listing: str | None = None
        self.permission_output: str | None = None
        self.history: str | None = None
        self.records: list[str] = []
        self.events: list[object] = []
        self.calls: list[tuple[list[str], float]] = []
        self.cost = 0.02
        self.kill_on_revoke = True
        self.coupled = True
        self.alive = True
        self.reused = False
        self.stat_error: str | None = None
        self.fail_grants: set[str] = set()
        self.fail_revokes: set[str] = set()
        self.bad_record: dict[str, object] = {}
        self.status_uid = samples.UID
        self.cmdline = pc.PACKAGE + "\0"
        self.root_identity = "uid=0(root) gid=0(root)\n"

    def guard(self) -> None:
        self.events.append("guard")

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def unix_ms(self) -> int:
        return samples.NOW_MS + round((self.now - 100) * 1000)

    def run(
        self, command: list[str], *, timeout: float, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        if not 0 < timeout <= pc.COMMAND_SECONDS or check:
            raise AssertionError("unbounded command or non-journaled failure")
        if self.events[-1] != "guard":
            raise AssertionError("command did not immediately follow phone_guard")
        self.events.append(list(command))
        self.calls.append((command, timeout))
        self.now += self.cost
        args = command[4:]
        for verb in ("grant", "revoke"):
            for permission in pc.PERMISSIONS:
                if tuple(args) == pc.permission_mutation_arguments(
                    verb, permission, use_root=True
                ):
                    args = ["pm", verb, "--user", "0", pc.PACKAGE, permission]
        code = 0
        if tuple(args) == pc.ROOT_ID_ARGS:
            output = self.root_identity
        elif args == ["getprop", "ro.build.version.sdk"]:
            output = self.sdk + "\n"
        elif args == ["am", "get-current-user"]:
            output = f"{self.current_user}\n"
        elif args == ["dumpsys", "package", pc.PACKAGE]:
            output = self.permission_output or samples.permission_dump(
                self.scan, self.connect, uid=self.uid
            )
        elif args == [
            "cmd",
            "package",
            "list",
            "packages",
            "-U",
            "--user",
            "0",
            pc.PACKAGE,
        ]:
            output = self.listing or f"package:{pc.PACKAGE} uid:{self.uid}\n"
        elif args == ["date", "+%s%3N,%z"]:
            output = f"{self.unix_ms()},+0000\n"
        elif args == ["dumpsys", "activity", "exit-info", pc.PACKAGE]:
            output = self.history or samples.exit_dump(*self.records)
        elif args == ["cat", f"/proc/{samples.PID}/stat"]:
            if self.stat_error:
                code, output = 1, self.stat_error
            elif self.reused:
                output = samples.proc_stat(ticks=samples.START_TICKS + 1)
            elif self.alive:
                output = samples.proc_stat()
            else:
                code, output = (
                    1,
                    f"cat: /proc/{samples.PID}/stat: No such file or directory\n",
                )
        elif args == ["cat", f"/proc/{samples.PID}/status"]:
            output = f"Name:\thost\nUid:\t{self.status_uid}\t{self.status_uid}\t{self.status_uid}\t{self.status_uid}\n"
        elif args == ["cat", f"/proc/{samples.PID}/cmdline"]:
            output = self.cmdline
        elif len(args) == 6 and args[:1] == ["pm"]:
            verb, user_flag, user, package, permission = args[1:]
            if (user_flag, user, package) != ("--user", "0", pc.PACKAGE):
                raise AssertionError("wrong mutation scope")
            if permission not in pc.PERMISSIONS or verb not in ("revoke", "grant"):
                raise AssertionError("unsupported mutation")
            if (verb == "grant" and permission in self.fail_grants) or (
                verb == "revoke" and permission in self.fail_revokes
            ):
                code, output = 1, f"SecurityException: {verb} failed\n"
            else:
                value = verb == "grant"
                if self.coupled or permission == pc.SCAN:
                    self.scan = value
                if self.coupled or permission == pc.CONNECT:
                    self.connect = value
                if verb == "revoke" and self.alive and self.kill_on_revoke:
                    self.alive = False
                    timestamp = datetime.fromtimestamp(self.unix_ms() / 1000, UTC)
                    fields = {
                        "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    }
                    fields.update(self.bad_record)
                    self.records.insert(0, samples.exit_record(**fields))
                output = ""
        else:
            raise AssertionError(
                f"unexpected command (never fall through to real ADB): {command}"
            )
        return subprocess.CompletedProcess(command, code, output)

    def mutations(self) -> list[list[str]]:
        return [command[4:] for command, _ in self.calls if command[4] == "pm"]


class PermissionParsingTests(unittest.TestCase):
    def test_default_api_and_mutation_still_use_legacy_package(self) -> None:
        self.assertEqual(pc.PACKAGE, "com.blehubnitrohost")
        self.assertEqual(
            pc.permission_mutation_arguments("grant", pc.SCAN, use_root=False),
            ("pm", "grant", "--user", "0", pc.PACKAGE, pc.SCAN),
        )
        self.assertEqual(
            pc.parse_permissions(samples.permission_dump(False, True)),
            (samples.UID, {pc.SCAN: False, pc.CONNECT: True}),
        )

    def test_formal_package_is_exact_in_parser_and_mutation(self) -> None:
        dump = samples.permission_dump(False, True).replace(pc.PACKAGE, FORMAL_PACKAGE)
        self.assertEqual(
            pc.parse_permissions(dump, package=FORMAL_PACKAGE),
            (samples.UID, {pc.SCAN: False, pc.CONNECT: True}),
        )
        self.assertEqual(
            pc.permission_mutation_arguments(
                "grant", pc.SCAN, use_root=True, package=FORMAL_PACKAGE
            ),
            (
                "su",
                "-c",
                f"'pm grant --user 0 {FORMAL_PACKAGE} {pc.SCAN}'",
            ),
        )
        with self.assertRaisesRegex(ValueError, "canonical"):
            pc.permission_mutation_arguments(
                "grant", pc.SCAN, use_root=True, package="com.bad;pm"
            )

    def test_controller_documents_formal_package_and_rejects_invalid_package(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "permission.json"
            controller = pc.AndroidPermissionController(
                "formal-phone",
                RUN,
                path,
                phone_guard=lambda: None,
                package=FORMAL_PACKAGE,
            )
            self.assertEqual(controller.package, FORMAL_PACKAGE)
            self.assertEqual(controller.document["package"], FORMAL_PACKAGE)
            with self.assertRaisesRegex(ValueError, "canonical"):
                pc.AndroidPermissionController(
                    "formal-phone",
                    RUN,
                    path,
                    phone_guard=lambda: None,
                    package="formal package",
                )

    def test_uid_listing_requires_exact_app_among_substring_matches(self) -> None:
        app = f"package:{pc.PACKAGE} uid:{samples.UID}\n"
        instrumentation = f"package:{pc.PACKAGE}.test uid:{samples.UID + 1}\n"
        for valid in (app, instrumentation + app, app + instrumentation):
            pc.verify_package_uid_listing(valid, samples.UID)
        for invalid in (
            "",
            instrumentation,
            app + app,
            app + instrumentation + instrumentation,
            app.replace(str(samples.UID), str(samples.UID + 1)),
            app + "package:other.app uid:10200\n",
            app + "unexpected output\n",
            app + instrumentation.replace("uid:", "uid="),
            app + instrumentation.replace(str(samples.UID + 1), "110234"),
        ):
            with self.subTest(output=invalid), self.assertRaises(RuntimeError):
                pc.verify_package_uid_listing(invalid, samples.UID)

    def test_only_user0_runtime_grants_are_evidence(self) -> None:
        uid, grants = pc.parse_permissions(samples.permission_dump(False, True))
        self.assertEqual(uid, samples.UID)
        self.assertEqual(grants, {pc.SCAN: False, pc.CONNECT: True})
        dump = samples.permission_dump().replace("userId=", "appId=")
        self.assertEqual(pc.parse_permissions(dump)[0], samples.UID)

    def test_missing_duplicate_and_wrong_scope_fail_closed(self) -> None:
        valid = samples.permission_dump()
        cases = [
            samples.permission_dump(user=1),
            samples.permission_dump(uid=110234),
            valid.replace(pc.PACKAGE, pc.PACKAGE + ".other"),
            valid.replace("runtime permissions:", "install permissions:", 1),
            valid.replace(
                "      runtime permissions:",
                "      runtime permissions:\n      runtime permissions:",
                1,
            ),
            valid.replace("    User 0:", "    User 0: installed=true\n    User 0:", 1),
            valid.replace("    userId=10234", "    userId=10234\n    userId=10234"),
            valid.replace("    User 0:", "    User 0: installed=true "),
            valid.replace(
                "        android.permission.BLUETOOTH_SCAN:",
                "        android.permission.BLUETOOTH_SCAN: granted=false\n        android.permission.BLUETOOTH_SCAN:",
                1,
            ),
            valid.replace("granted=true, flags=", "granted=trueish, flags=", 1),
        ]
        for output in cases:
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                pc.parse_permissions(output)

    def test_exit_dump_full_format_timezone_and_identity_not_sequence(self) -> None:
        raw = samples.exit_dump(samples.exit_record())
        record = pc.parse_exit_history(raw, "+0000")[0]
        self.assertEqual(record["timestamp_unix_ms"], samples.NOW_MS + 100)
        changed = samples.exit_dump(samples.exit_record(index=9)).replace(
            "pss=12MB", "pss=13MB"
        )
        self.assertEqual(
            pc.parse_exit_history(changed, "+0000")[0]["fingerprint"],
            record["fingerprint"],
        )
        local = raw.replace("12:00:00.100", "20:00:00.100")
        self.assertEqual(
            pc.parse_exit_history(local, "+0800")[0]["timestamp_unix_ms"],
            samples.NOW_MS + 100,
        )
        for bad in (
            raw.replace("realUid=", "wrongUid="),
            raw.replace("state=empty trace=null", ""),
            raw + "... truncated ...\n",
            raw.replace("package: com.blehubnitrohost", "package: other"),
            samples.exit_dump(samples.exit_record(), samples.exit_record(index=1)),
            raw.replace("PROCESS EXIT INFO", "PROCESS EXIT"),
        ):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                pc.parse_exit_history(bad, "+0000")

    def test_exit_dump_device_banner_and_uid_group_preserve_records(self) -> None:
        raw = samples.exit_dump(
            samples.exit_record(),
            samples.exit_record(
                index=1,
                pid=samples.PID + 1,
                reason=10,
                timestamp="2026-10-17 12:00:01.000",
            ),
        )
        expected = pc.parse_exit_history(raw, "+0800")
        for group in ("userId=0", f"uid={samples.UID}"):
            for colon in ("", ":"):
                for banner in (
                    "",
                    "---- TIME 2026/10/17 20:28:42 ----\n",
                    "---- TIME 2024/02/29 00:00:00 ----\n",
                ):
                    with self.subTest(group=group, colon=colon, banner=banner):
                        output = banner + raw.replace("userId=0", group + colon)
                        self.assertEqual(
                            pc.parse_exit_history(output, "+0800"), expected
                        )
        empty = samples.exit_dump().replace("userId=0", f"uid={samples.UID}")
        self.assertEqual(pc.parse_exit_history(empty, "+0800"), [])

    def test_exit_dump_time_banner_requires_exact_valid_datetime(self) -> None:
        raw = samples.exit_dump(samples.exit_record()).replace(
            "userId=0", f"uid={samples.UID}"
        )
        for timestamp in (
            "0000/10/17 20:28:42",
            "2026/00/17 20:28:42",
            "2026/13/17 20:28:42",
            "2026/10/00 20:28:42",
            "2026/04/31 20:28:42",
            "2026/02/29 20:28:42",
            "1900/02/29 20:28:42",
            "2026/10/17 24:28:42",
            "2026/10/17 20:60:42",
            "2026/10/17 20:28:60",
            "2026/1/17 20:28:42",
            "2026/10/7 20:28:42",
            "2026/10/17 2:28:42",
            "2026/10/17 20:8:42",
            "2026/10/17 20:28:2",
            "2026-10-17 20:28:42",
            "2026/10/17T20:28:42",
            "2026/10/17 20:28:42.000",
            "2026/10/17 20:28:42+0800",
            "２０２６/10/17 20:28:42",
        ):
            with self.subTest(timestamp=timestamp), self.assertRaises(RuntimeError):
                pc.parse_exit_history(f"---- TIME {timestamp} ----\n" + raw, "+0800")

    def test_exit_dump_banner_position_noise_and_bad_records_fail_closed(self) -> None:
        banner = "---- TIME 2026/10/17 20:28:42 ----\n"
        raw = samples.exit_dump(samples.exit_record()).replace(
            "userId=0", f"uid={samples.UID}"
        )
        duplicate = samples.exit_dump(
            samples.exit_record(), samples.exit_record(index=1)
        ).replace("userId=0", f"uid={samples.UID}")
        for output in (
            banner,
            banner + banner + raw,
            banner + banner.replace("20:28:42", "20:28:43") + raw,
            raw + banner,
            raw.replace("  package:", banner + "  package:", 1),
            raw.replace("          process=", banner + "          process=", 1),
            "garbage\n" + banner + raw,
            banner + "garbage\n" + raw,
            banner + raw + "garbage\n",
            "\n" + banner + raw,
            " " + banner + raw,
            banner.replace(" ----\n", " ---- garbage\n") + raw,
            banner.replace(" ----\n", " ---- \n") + raw,
            banner + raw + samples.HEADER,
            banner + raw.replace(f"uid={samples.UID}", f"uid={samples.UID} junk"),
            banner + raw.replace("for uid=", "for appId="),
            banner + duplicate,
            banner + raw.replace("realUid=", "wrongUid="),
            banner + raw.replace("state=empty trace=null", ""),
            banner + raw.replace("package: " + pc.PACKAGE, "package: other"),
            banner + raw.replace("#0:", "#0:\n        ApplicationExitInfo #1:"),
        ):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                pc.parse_exit_history(output, "+0800")


class PermissionControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "permission-control.json"
        self.phone = FakePhone()
        self.guard = Mock(side_effect=self.phone.guard)
        self.stack.enter_context(
            patch.object(adb, "adb_executable", return_value=Path("/sdk/adb"))
        )
        self.transport = self.stack.enter_context(
            patch.object(adb, "run_command", side_effect=self.phone.run)
        )
        self.stack.enter_context(
            patch.object(pc.time, "monotonic", side_effect=lambda: self.phone.now)
        )
        self.stack.enter_context(
            patch.object(pc.time, "sleep", side_effect=self.phone.sleep)
        )
        self.controller = pc.AndroidPermissionController(
            DEVICE, RUN, self.path, phone_guard=self.guard
        )

    def identity(self) -> pc.AndroidProcessIdentity:
        return pc.AndroidProcessIdentity(**samples.process_identity())

    def initialized(self) -> None:
        self.controller.initial()

    def revoked(self) -> None:
        self.initialized()
        self.controller.revoke_connected(self.identity())

    def test_baseline_raw_journal_hash_atomic_json_and_initial_preconditions(
        self,
    ) -> None:
        self.phone.records = [
            samples.exit_record(pid=1234, timestamp="2026-10-17 11:59:00.000")
        ]
        self.initialized()
        document = json.loads(self.path.read_text())
        self.assertEqual(document["uid"], samples.UID)
        self.assertEqual(len(document["baseline_exit_records"]), 1)
        receipts = document["actions"][0]["receipts"]
        for receipt in receipts:
            self.assertTrue(receipt["stdout_complete"])
            self.assertEqual(
                receipt["stdout_sha256"],
                hashlib.sha256(receipt["stdout"].encode()).hexdigest(),
            )
        self.assertEqual(receipts[-1]["stdout"], samples.exit_dump(*self.phone.records))
        self.assertEqual(self.phone.mutations(), [])
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_root_mutations_keep_one_transport_and_original_identity_proof(
        self,
    ) -> None:
        self.controller = pc.AndroidPermissionController(
            DEVICE, RUN, self.path, phone_guard=self.guard, use_root=True
        )
        self.initialized()
        self.controller.deny_startup()
        self.controller.grant(label="active")
        self.phone.fail_grants = set(pc.PERMISSIONS)
        with self.assertRaises(RuntimeError):
            self.controller.grant(label="failed")
        self.controller.finish(RuntimeError("preserve failure"))
        mutations = [
            c[4:]
            for c, _ in self.phone.calls
            if c[4] in ("pm", "su") and tuple(c[4:]) != pc.ROOT_ID_ARGS
        ]
        self.assertTrue(mutations)
        self.assertTrue(all(c[:2] == ["su", "-c"] for c in mutations))
        self.assertEqual(self.controller.document["permission_command_mode"], "root")
        proof = self.controller.document["actions"][0]["receipts"][0]
        self.assertEqual(proof["command"][4:], list(pc.ROOT_ID_ARGS))
        self.assertEqual(proof["stdout"], self.phone.root_identity)
        self.assertEqual(self.controller.document["status"], "failed")

    def test_failed_root_revoke_is_not_retried_or_fallen_back_to_shell(self) -> None:
        self.controller = pc.AndroidPermissionController(
            DEVICE, RUN, self.path, phone_guard=self.guard, use_root=True
        )
        self.initialized()
        self.phone.fail_revokes.add(pc.SCAN)
        with self.assertRaisesRegex(RuntimeError, "SecurityException: revoke failed"):
            self.controller.deny_startup()
        expected = list(
            pc.permission_mutation_arguments("revoke", pc.SCAN, use_root=True)
        )
        attempted = [
            c[4:]
            for c, _ in self.phone.calls
            if c[4] in ("su", "pm") and tuple(c[4:]) != pc.ROOT_ID_ARGS
        ]
        self.assertEqual(attempted, [expected])
        self.assertFalse(self.phone.mutations())

    def test_nonroot_identity_cannot_authorize_selected_root_transport(self) -> None:
        self.phone.root_identity = "uid=2000(shell) gid=2000(shell)\n"
        self.controller = pc.AndroidPermissionController(
            DEVICE, RUN, self.path, phone_guard=self.guard, use_root=True
        )
        with self.assertRaisesRegex(RuntimeError, "root identity"):
            self.controller.initial()
        self.assertEqual(len(self.phone.calls), 1)
        self.assertFalse(self.controller.initialized)

    def test_wrong_uid_user_sdk_or_initial_deny_never_mutates(self) -> None:
        cases = (
            {"uid": 110234},
            {"current_user": 10},
            {"sdk": "30"},
            {"scan": False},
            {"listing": f"package:{pc.PACKAGE} uid:10444\n"},
            {"permission_output": samples.permission_dump(user=1)},
        )
        for index, fields in enumerate(cases):
            with self.subTest(fields=fields):
                phone = FakePhone()
                for key, value in fields.items():
                    setattr(phone, key, value)
                controller = pc.AndroidPermissionController(
                    DEVICE,
                    RUN,
                    self.path.with_name(f"bad-{index}.json"),
                    phone_guard=phone.guard,
                )
                with patch.object(adb, "run_command", side_effect=phone.run):
                    with self.assertRaises(RuntimeError) as error:
                        controller.initial()
                    controller.finish(error.exception)
                self.assertEqual(phone.mutations(), [])

    def test_startup_deny_and_grant_both_with_strict_readback(self) -> None:
        self.initialized()
        denied = self.controller.deny_startup()
        self.assertEqual(denied, {pc.SCAN: False, pc.CONNECT: False})
        self.assertEqual([c[-1] for c in self.phone.mutations()], list(pc.PERMISSIONS))
        self.assertEqual(
            self.controller.grant(label="startup-recovery"),
            {pc.SCAN: True, pc.CONNECT: True},
        )
        with self.assertRaisesRegex(RuntimeError, "repeated"):
            self.controller.deny_startup()
        self.assertEqual(self.controller.finalize(None), [])
        self.assertEqual(json.loads(self.path.read_text())["status"], "passed")

    def test_connected_group_coupling_real_readback_and_exact_reason8_death(
        self,
    ) -> None:
        self.initialized()
        denied = self.controller.revoke_connected(self.identity())
        self.assertEqual(denied, {pc.SCAN: False, pc.CONNECT: False})
        self.assertEqual(
            self.phone.mutations(),
            [["pm", "revoke", "--user", "0", pc.PACKAGE, pc.CONNECT]],
        )
        record = self.controller.await_permission_death(self.identity())
        self.assertEqual(record["reason"], 8)
        self.assertEqual(record["pid"], samples.PID)
        self.assertTrue(self.controller.death_confirmed)
        self.assertEqual(
            self.controller.grant(label="connected-recovery"),
            {pc.SCAN: True, pc.CONNECT: True},
        )
        self.assertEqual(self.controller.finish(None), [])
        self.assertEqual(self.controller.document["status"], "passed")
        for command, _ in self.phone.calls:
            self.assertNotIn("pidof", command)
            self.assertNotIn("force-stop", command)
            self.assertNotIn("kill", command)
            self.assertNotIn("bluetooth", command)
        with self.assertRaises(RuntimeError):
            self.controller.revoke_connected(self.identity())

    def test_scan_can_remain_granted_on_non_coupled_rom(self) -> None:
        self.initialized()
        self.phone.coupled = False
        self.assertEqual(
            self.controller.revoke_connected(self.identity()),
            {pc.SCAN: True, pc.CONNECT: False},
        )
        self.controller.await_permission_death(self.identity())

    def test_wrong_process_identity_rejected_before_mutation(self) -> None:
        self.initialized()
        for fields in (
            {"uid": 10444},
            {"user_id": 10},
            {"process_name": pc.PACKAGE + ":remote"},
            {"pid": "2345"},
            {"start_ticks": True},
        ):
            identity = self.identity() | fields
            with self.subTest(fields=fields), self.assertRaises(RuntimeError):
                self.controller.revoke_connected(identity)
        self.assertEqual(self.phone.mutations(), [])

    def test_proc_uid_cmdline_or_instance_mismatch_refuses_revoke(self) -> None:
        self.initialized()
        self.phone.status_uid += 1
        with self.assertRaisesRegex(RuntimeError, "UID mismatch"):
            self.controller.revoke_connected(self.identity())
        self.assertEqual(self.phone.mutations(), [])
        with self.assertRaisesRegex(RuntimeError, "retried"):
            self.controller.revoke_connected(self.identity())

    def test_new_exit_must_match_pid_uid_user_process_and_window(self) -> None:
        bad_fields = (
            {"real_uid": 10444},
            {"package_uid": 10444},
            {"user": 10},
            {"process": pc.PACKAGE + ":remote"},
            {"reason": 10},
            {"timestamp": "2026-10-17 11:59:59.999"},
            {"timestamp": "2026-10-17 12:00:05.000"},
        )
        for index, fields in enumerate(bad_fields):
            with self.subTest(fields=fields):
                phone = FakePhone()
                phone.bad_record = fields
                controller = pc.AndroidPermissionController(
                    DEVICE,
                    RUN,
                    self.path.with_name(f"exit-{index}.json"),
                    phone_guard=phone.guard,
                )
                with (
                    patch.object(adb, "run_command", side_effect=phone.run),
                    patch.object(
                        pc.time, "monotonic", side_effect=lambda phone=phone: phone.now
                    ),
                ):
                    controller.initial()
                    controller.revoke_connected(self.identity())
                    with self.assertRaises(RuntimeError):
                        controller.await_permission_death(self.identity())
                self.assertFalse(controller.death_confirmed)

    def test_baseline_record_cannot_be_reused_even_after_pid_disappears(self) -> None:
        self.phone.history = samples.exit_dump(
            samples.exit_record(timestamp="2026-10-17 11:59:00.000")
        )
        self.revoked()
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            self.controller.await_permission_death(self.identity(), timeout=0.4)
        self.assertFalse(self.controller.death_confirmed)

    def test_absent_pid_without_exit_record_or_rom_survival_fails_closed(self) -> None:
        self.phone.kill_on_revoke = False
        self.revoked()
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            self.controller.await_permission_death(self.identity(), timeout=0.4)
        self.phone.alive = False
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            self.controller.await_permission_death(self.identity(), timeout=0.4)
        self.assertFalse(self.controller.death_confirmed)
        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            self.controller.grant(label="not-a-kill-workaround")
        self.assertEqual(len(self.phone.mutations()), 1)

    def test_duplicate_exit_records_and_access_denied_are_not_death(self) -> None:
        self.revoked()
        self.phone.stat_error = f"cat: /proc/{samples.PID}/stat: Permission denied\n"
        with self.assertRaisesRegex(RuntimeError, "ambiguous /proc"):
            self.controller.await_permission_death(self.identity())
        self.phone.stat_error = None
        self.phone.records.append(self.phone.records[0].replace("#0:", "#1:"))
        with self.assertRaisesRegex(RuntimeError, "duplicate exit record"):
            self.controller.await_permission_death(self.identity())

    def test_pid_reuse_requires_reason8_for_original_instance(self) -> None:
        self.revoked()
        self.phone.reused = True
        self.assertEqual(
            self.controller.await_permission_death(self.identity())["pid"], samples.PID
        )
        action = self.controller.document["actions"][-1]
        self.assertTrue(action["old_instance_gone"])
        self.assertEqual(action["observed_start_ticks"], samples.START_TICKS + 1)

    def test_guard_loss_blocks_mutations_and_cleanup_preserves_primary(self) -> None:
        self.initialized()
        primary = RuntimeError("original RF failure")
        self.guard.side_effect = RuntimeError("foreign phone owner")
        with self.assertRaisesRegex(RuntimeError, "foreign phone owner"):
            self.controller.deny_startup()
        errors = self.controller.finish(primary)
        self.assertIn("foreign phone owner", errors[0])
        self.assertEqual(self.phone.mutations(), [])
        self.assertEqual(
            self.controller.document["primary_error"]["message"], "original RF failure"
        )
        self.assertIn("restore permissions", primary.__notes__[0])

    def test_restore_attempts_both_and_retains_grant_error_even_if_readback_true(
        self,
    ) -> None:
        self.initialized()
        self.controller.deny_startup()
        self.phone.fail_grants.add(pc.SCAN)
        primary = RuntimeError("reason10 must remain primary")
        errors = self.controller.finish(primary)
        # CONNECT grant couples SCAN to true; the earlier grant error still fails cleanup.
        self.assertTrue(self.phone.scan and self.phone.connect)
        self.assertTrue(errors)
        self.assertEqual(
            [c[-1] for c in self.phone.mutations()[-2:]], list(pc.PERMISSIONS)
        )
        self.assertEqual(self.controller.document["status"], "failed")
        self.assertEqual(
            self.controller.document["primary_error"]["message"], str(primary)
        )
        self.assertTrue(self.controller.finish(primary))

    def test_incomplete_death_cannot_be_passed_by_restoration(self) -> None:
        self.revoked()
        errors = self.controller.finish(None)
        self.assertIn("death gate incomplete", errors[0])
        self.assertEqual(
            self.controller.document["final_grants"], {pc.SCAN: True, pc.CONNECT: True}
        )
        self.assertEqual(self.controller.document["status"], "failed")

    def test_failed_death_gate_stays_failed_after_successful_cleanup(self) -> None:
        self.phone.bad_record = {"reason": 10}
        self.revoked()
        with self.assertRaisesRegex(RuntimeError, "reason8") as error:
            self.controller.await_permission_death(self.identity())
        self.assertEqual(self.controller.finish(error.exception), [])
        self.assertEqual(self.controller.document["status"], "failed")
        self.assertEqual(
            self.controller.document["final_grants"], {pc.SCAN: True, pc.CONNECT: True}
        )

    def test_existing_evidence_is_never_overwritten_even_by_finish(self) -> None:
        self.path.write_text('{"owned_by":"someone-else"}\n')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "existing evidence") as error:
            self.controller.initial()
        self.assertTrue(self.controller.finish(error.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.transport.assert_not_called()

    def test_revoke_deadline_propagates_decreasing_budget_to_every_command(
        self,
    ) -> None:
        self.initialized()
        self.phone.cost = 0.2
        start = len(self.phone.calls)
        before = self.phone.now
        self.controller.revoke_connected(self.identity(), timeout=3.0)
        calls = self.phone.calls[start:]
        self.assertEqual(len(calls), 10)
        for index, (_, timeout) in enumerate(calls):
            self.assertAlmostEqual(timeout, 3.0 - index * 0.2)
        self.assertLess(self.phone.now - before, 3.0)

    def test_late_readback_is_not_confirmed_and_revoke_is_not_retried(self) -> None:
        self.initialized()
        original = self.phone.run

        def slow(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            result = original(command, **kwargs)
            if command[4:6] == ["pm", "revoke"]:
                self.phone.now += 4.0
            return result

        with (
            patch.object(adb, "run_command", side_effect=slow),
            self.assertRaisesRegex(TimeoutError, "returned after"),
        ):
            self.controller.revoke_connected(self.identity())
        self.assertEqual(len(self.phone.mutations()), 1)
        self.assertFalse(self.controller.revoke_confirmed)
        with self.assertRaisesRegex(RuntimeError, "retried"):
            self.controller.revoke_connected(self.identity())
        self.phone.cost = 0.02
        self.assertEqual(self.controller.finish(RuntimeError("late revoke")), [])

    def test_expensive_adb_lookup_cannot_start_child_after_deadline(self) -> None:
        self.initialized()
        count = len(self.phone.calls)

        def lookup() -> Path:
            self.phone.now += 4.0
            return Path("/sdk/adb")

        with (
            patch.object(adb, "adb_executable", side_effect=lookup),
            self.assertRaisesRegex(TimeoutError, "before command"),
        ):
            self.controller.revoke_connected(self.identity())
        self.assertEqual(len(self.phone.calls), count)

    def test_command_timeout_partial_stdout_is_preserved_not_success(self) -> None:
        self.initialized()
        partial = "raw partial stdout\n"
        command = ["/sdk/adb", "-s", DEVICE, "shell", "am", "get-current-user"]
        with (
            patch.object(
                adb,
                "run_command",
                side_effect=subprocess.TimeoutExpired(
                    command, 1, output=partial.encode()
                ),
            ),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            self.controller.deny_startup()
        receipt = json.loads(self.path.read_text())["actions"][-1]["receipts"][-1]
        self.assertEqual(receipt["stdout"], partial)
        self.assertFalse(receipt["stdout_complete"])
        self.assertEqual(self.phone.mutations(), [])

    def test_overflow_has_hash_size_and_explicit_missing_raw_not_truncation(
        self,
    ) -> None:
        self.initialized()
        large = "x" * (pc.MAX_OUTPUT_BYTES + 1)
        with (
            patch.object(
                adb,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, large),
            ),
            self.assertRaisesRegex(RuntimeError, "no truncated text"),
        ):
            self.controller.deny_startup()
        receipt = json.loads(self.path.read_text())["actions"][-1]["receipts"][-1]
        self.assertTrue(receipt["output_overflow"])
        self.assertNotIn("stdout", receipt)
        self.assertEqual(
            receipt["stdout_sha256"], hashlib.sha256(large.encode()).hexdigest()
        )
        self.assertEqual(receipt["stdout_bytes"], len(large))

    def test_await_budget_includes_prior_revoke_time_and_delayed_call(self) -> None:
        self.revoked()
        self.phone.now += 6.0
        count = len(self.phone.calls)
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            self.controller.await_permission_death(self.identity(), timeout=6.0)
        self.assertEqual(len(self.phone.calls), count)
        self.assertFalse(self.controller.death_confirmed)

    def test_unsafe_timeout_inputs_rejected_without_revoke(self) -> None:
        self.initialized()
        for timeout in (0, -1, True, float("inf"), float("nan"), 4.01):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.controller.revoke_connected(self.identity(), timeout=timeout)
        self.assertEqual(self.phone.mutations(), [])
        self.assertIsNone(self.controller.old_identity)
        self.controller.revoke_connected(self.identity())
        count = len(self.phone.calls)
        for timeout in (0, -1, True, float("inf"), float("nan"), 6.01):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.controller.await_permission_death(self.identity(), timeout=timeout)
        self.assertEqual(len(self.phone.calls), count)
        self.assertFalse(self.controller.death_confirmed)

    def test_reason8_record_without_old_instance_death_is_insufficient(self) -> None:
        self.revoked()
        self.phone.alive = True
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            self.controller.await_permission_death(self.identity(), timeout=0.4)
        self.assertFalse(self.controller.death_confirmed)
        self.assertEqual(len(self.phone.mutations()), 1)

    def test_multiple_fresh_oldpid_records_are_ambiguous(self) -> None:
        self.revoked()
        self.phone.records.append(
            samples.exit_record(timestamp="2026-10-17 12:00:00.001", index=1)
        )
        with self.assertRaisesRegex(RuntimeError, "ambiguous new"):
            self.controller.await_permission_death(self.identity())
        self.assertFalse(self.controller.death_confirmed)

    def test_wrong_pid_exit_cannot_substitute_for_old_process_exit(self) -> None:
        self.phone.bad_record = {"pid": samples.PID + 1}
        self.revoked()
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            self.controller.await_permission_death(self.identity(), timeout=0.4)
        self.assertFalse(self.controller.death_confirmed)

    def test_await_commands_share_one_deadline_including_poll_sleeps(self) -> None:
        self.phone.kill_on_revoke = False
        self.revoked()
        started = self.phone.now
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            self.controller.await_permission_death(self.identity(), timeout=0.4)
        receipts = self.controller.document["actions"][-1]["receipts"]
        self.assertGreater(len(receipts), 3)
        for receipt in receipts:
            self.assertAlmostEqual(
                receipt["timeout_seconds"], started + 0.4 - receipt["started_monotonic"]
            )
        self.assertAlmostEqual(receipts[3]["timeout_seconds"], 0.24)

    def test_clock_jump_is_not_a_permission_death_window(self) -> None:
        self.revoked()
        original = self.phone.unix_ms
        with (
            patch.object(self.phone, "unix_ms", side_effect=lambda: original() + 10000),
            self.assertRaisesRegex(RuntimeError, "clock outside"),
        ):
            self.controller.await_permission_death(self.identity())
        self.assertFalse(self.controller.death_confirmed)

    def test_new_uid_between_initial_and_revoke_or_restore_refuses_mutation(
        self,
    ) -> None:
        self.initialized()
        self.phone.uid += 1
        with self.assertRaisesRegex(RuntimeError, "UID changed") as error:
            self.controller.revoke_connected(self.identity())
        errors = self.controller.finish(error.exception)
        self.assertIn("UID changed", errors[0])
        self.assertEqual(self.phone.mutations(), [])

    def test_guard_is_rechecked_at_each_mutation_not_only_action_entry(self) -> None:
        self.initialized()

        def guarded() -> None:
            receipts = self.controller.document["actions"][-1]["receipts"]
            if receipts and receipts[-1]["command"][4] == "pm":
                raise RuntimeError("lease lost at mutation boundary")
            self.phone.guard()

        self.guard.side_effect = guarded
        with self.assertRaisesRegex(RuntimeError, "mutation boundary"):
            self.controller.deny_startup()
        self.assertEqual(self.phone.mutations(), [])
        self.assertEqual(self.controller.document["actions"][-1]["status"], "failed")

    def test_restore_readback_denied_is_cleanup_failure_not_pass(self) -> None:
        self.initialized()
        self.controller.deny_startup()
        original = self.phone.run

        def grant_does_not_stick(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            result = original(command, **kwargs)
            if command[4:6] == ["pm", "grant"]:
                self.phone.scan = self.phone.connect = False
            return result

        primary = RuntimeError("original permission failure")
        with patch.object(adb, "run_command", side_effect=grant_does_not_stick):
            errors = self.controller.finish(primary)
        self.assertIn("grant/readback failed", errors[0])
        self.assertEqual(
            self.controller.document["primary_error"]["message"], str(primary)
        )
        self.assertEqual(self.controller.document["status"], "failed")
        self.assertEqual(
            self.controller.document["actions"][-1]["observed_grants"],
            {pc.SCAN: False, pc.CONNECT: False},
        )

    def test_journal_publication_errors_cannot_mask_primary_action_or_cleanup(
        self,
    ) -> None:
        self.initialized()
        primary = RuntimeError("lost lease is primary")
        self.guard.side_effect = primary
        with patch.object(
            pc, "write_document", side_effect=OSError("journal disk full")
        ):
            with self.assertRaises(RuntimeError) as raised:
                self.controller.deny_startup()
            self.assertIs(raised.exception, primary)
            self.assertIn("journal disk full", primary.__notes__[0])
            errors = self.controller.finish(primary)
        self.assertGreaterEqual(len(errors), 2)
        self.assertEqual(self.controller.document["status"], "failed")
        self.assertIn("publish permission cleanup", errors[-1])
        self.assertEqual(self.phone.mutations(), [])


if __name__ == "__main__":
    unittest.main()
