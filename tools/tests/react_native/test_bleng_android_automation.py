"""Prepared-helper inventory contract; no Appium session or physical device I/O.

Only the real Android SDK subprocess boundary and adb lookup are mocked. APKs,
phone leases, drift and publication conflicts use real temporary filesystem state.

"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from collections import Counter
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

from tools.android.android_appium import uiautomator2_support_apks
from tools.android.state_control import AndroidRadioController
from tools.react_native import android_sdk
from tools.react_native.bleng_android_automation import (
    inspect_prepared_android_automation,
    require_mock_location_restored,
)
from tools.react_native.capture_io import publish_new_json

SETTINGS = "io.appium.settings"
SERVER = "io.appium.uiautomator2.server"
TEST = SERVER + ".test"
PACKAGES = (SETTINGS, SERVER, TEST)
BOOT = ("shell", "cat", "/proc/sys/kernel/random/boot_id")
PACKAGE_UIDS = ("shell", "pm", "list", "packages", "-U", "--user", "0")
PROCESSES = ("shell", "ps", "-A", "-o", "PID,UID,ARGS")
FORWARDS = ("forward", "--list")
INSTRUMENTATION = ("shell", "pm", "list", "instrumentation")
POLICIES = (
    "hidden_api_policy_pre_p_apps",
    "hidden_api_policy_p_apps",
    "hidden_api_policy",
)
COMPONENT = TEST + "/androidx.test.runner.AndroidJUnitRunner"


class PreparedAndroidAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sequence = 0
        # Install isolation before creating any synthetic controller/serial.
        self.sdk = self.enterContext(
            patch.object(android_sdk.subprocess, "run", side_effect=self.sdk_command)
        )
        self.enterContext(
            patch.object(android_sdk, "adb_executable", side_effect=self.adb_lookup)
        )
        self.prepare()

    def prepare(self) -> None:
        """Give every subcase its own real lease, APK tree and fresh output path."""
        self.sequence += 1
        self.case = self.root / str(self.sequence)
        self.case.mkdir()
        self.appium_home = self.case / "appium-home"
        driver = self.appium_home / "node_modules/appium-uiautomator2-driver"
        self.apks = dict(
            zip(
                PACKAGES,
                (
                    driver
                    / "node_modules/io.appium.settings/apks/settings_apk-debug.apk",
                    driver
                    / "node_modules/appium-uiautomator2-server/apks/appium-uiautomator2-server-v10.7.6.apk",
                    driver
                    / "node_modules/appium-uiautomator2-server/apks/appium-uiautomator2-server-debug-androidTest.apk",
                ),
                strict=True,
            )
        )
        for package, path in self.apks.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"synthetic APK for {package}; never install".encode())
        self.expected = {
            package: hashlib.sha256(path.read_bytes()).hexdigest()
            for package, path in self.apks.items()
        }
        self.paths = {
            package: f"/data/app/~~inventory/{package}-candidate/base.apk"
            for package in PACKAGES
        }
        self.uids = dict(zip(PACKAGES, (10101, 10102, 10103), strict=True))
        self.serial = f"synthetic-inventory-{uuid.uuid4().hex}"
        self.phone = AndroidRadioController(
            self.serial,
            str(uuid.uuid4()),
            self.case / "phone.json",
            lock_root=self.case / "leases",
        )
        self.phone.acquire()
        self.output = self.case / "inventory"
        self.adb = self.case / "nonexistent-sdk/adb"
        self.deadline = time.monotonic() + 60
        self.calls: list[tuple[str, ...]] = []
        self.counts: Counter[tuple[str, ...]] = Counter()
        self.after_command: Callable[[tuple[str, ...]], None] | None = None
        self.replies = {
            ("get-state",): "device\n",
            BOOT: "12345678-1234-4234-8234-123456789abc\n",
            ("shell", "am", "get-current-user"): "0\n",
            ("shell", "pm", "list", "users"): (
                "Users:\n\tUserInfo{0:Owner:c13} running\n"
            ),
            PACKAGE_UIDS: "".join(
                f"package:{package} uid:{uid}\n" for package, uid in self.uids.items()
            )
            + "package:com.example.unrelated uid:10200\n",
            INSTRUMENTATION: (f"instrumentation:{COMPONENT} (target={SERVER})\n"),
            PROCESSES: "PID   UID   ARGS\n1 0 /system/bin/init\n",
            FORWARDS: "",
            ("shell", "cmd", "appops", "get", SETTINGS, "android:mock_location"): (
                "android:mock_location: default\n"
            ),
        }
        for policy in POLICIES:
            self.replies[("shell", "settings", "get", "global", policy)] = "null\n"
        for package in PACKAGES:
            self.replies[("shell", "pm", "path", package)] = (
                f"package:{self.paths[package]}\n"
            )
            self.replies[("shell", "sha256sum", self.paths[package])] = (
                f"{self.expected[package]}  {self.paths[package]}\n"
            )
        self.later: dict[tuple[str, ...], str] = {}
        self.returncodes: dict[tuple[str, ...], int] = {}
        self.sdk.reset_mock()

    def adb_lookup(self) -> Path:
        self.lookup_time = time.monotonic()
        return self.adb

    def sdk_command(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        # An exact allowlist rejects root/install/grant/radio/repair/forward removal,
        # shell strings and accidental commands against any real or foreign serial.
        self.assertEqual(command[:3], [str(self.adb), "-s", self.serial])
        args = tuple(command[3:])
        self.assertIn(args, self.replies, f"non-read-only SDK command: {command}")
        self.assertEqual(kwargs["cwd"], self.case)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(kwargs.get("shell", False))
        budget = kwargs["timeout"]
        self.assertTrue(math.isfinite(budget))
        self.assertGreater(budget, 0)
        self.assertLessEqual(budget, 5)
        self.assertLessEqual(budget, self.deadline - self.lookup_time + 0.001)
        self.calls.append(args)
        self.counts[args] += 1
        reply = (
            self.later.get(args, self.replies[args])
            if self.counts[args] > 1
            else self.replies[args]
        )
        if self.after_command is not None:
            self.after_command(args)
        return subprocess.CompletedProcess(
            command, self.returncodes.get(args, 0), reply
        )

    def inspect(self, **changes) -> Path:
        arguments = {
            "phone": self.phone,
            "appium_home": self.appium_home,
            "expected_apk_sha256": self.expected,
            "system_port": 18200,
            "output": self.output,
            "cwd": self.case,
            "deadline": self.deadline,
        }
        arguments.update(changes)
        return inspect_prepared_android_automation(**arguments)

    def failure(self, *, result: bool = True, **changes) -> Exception:
        with self.assertRaises(Exception) as raised:
            self.inspect(**changes)
        error = raised.exception
        # SDK allowlist/budget assertions must never masquerade as expected failures.
        self.assertNotIsInstance(error, AssertionError)
        if result:
            document = json.loads((self.output / "result.json").read_bytes())
            self.assertEqual(document["status"], "failed")
            self.assertTrue(document["primary_error"])
            self.assertIn(str(error), str(document["primary_error"]))
        return error

    def lose_lease(self) -> None:
        (self.phone.lock / "owner.json").write_bytes(b"foreign lease owner")

    def test_valid_observations_are_inventory_only_and_preserve_device_state(self):
        for mode, policy in (
            ("default", "null"),
            ("allow", "1"),
            ("deny", "2"),
            ("ignore", "0"),
        ):
            with self.subTest(mode=mode, policy=policy):
                self.prepare()
                self.assertEqual(
                    uiautomator2_support_apks(self.appium_home),
                    tuple(self.apks.values()),
                )
                self.replies[PROCESSES] += f"421 {self.uids[SETTINGS]} {SETTINGS}\n"
                self.replies[PROCESSES] += "423 10200 com.example.unrelated\n"
                self.replies[FORWARDS] = (
                    f"{self.serial} tcp:19000 tcp:9000\n"
                    "synthetic-foreign-phone tcp:19001 tcp:6790\n"
                )
                self.replies[
                    ("shell", "cmd", "appops", "get", SETTINGS, "android:mock_location")
                ] = f"android:mock_location: {mode}\n"
                for key in POLICIES:
                    self.replies[("shell", "settings", "get", "global", key)] = (
                        policy + "\n"
                    )
                journal = self.phone.path.read_bytes()
                path = self.inspect()
                self.assertEqual(path, self.output / "result.json")
                document = json.loads(path.read_bytes())
                self.assertEqual(document["status"], "inventory-verified")
                self.assertEqual(document["automation_admission"], "pending")
                self.assertEqual(document["run_id"], self.phone.run_id)
                self.assertEqual(document["device"], self.serial)
                self.assertEqual(set(document["helpers"]), set(PACKAGES))
                for package, helper in document["helpers"].items():
                    self.assertEqual(helper["installed_path"], self.paths[package])
                    self.assertEqual(helper["sha256"], self.expected[package])
                    self.assertEqual(helper["uid"], self.uids[package])
                self.assertEqual(
                    set(document["observed_hidden_api_policy"]), set(POLICIES)
                )
                for value in document["observed_hidden_api_policy"].values():
                    self.assertIn(
                        value,
                        (None, "null") if policy == "null" else (int(policy), policy),
                    )
                self.assertEqual(document["observed_mock_location_mode"], mode)
                self.assertEqual(
                    document["observed_mock_location_scopes"], {"package": mode}
                )
                self.assertTrue(document["settings_processes"])
                requirements = json.dumps(document["remaining_requirements"]).lower()
                self.assertTrue(document["remaining_requirements"])
                for term in ("session", "owner", "settings", "finaliz"):
                    self.assertIn(term, requirements)
                self.assertEqual(self.phone.path.read_bytes(), journal)
                self.phone.require_owner()
                self.assertFalse(self.phone.finished)
                self.assertEqual(self.phone.document["actions"], [])
                self.assertEqual(set(self.calls), set(self.replies))
                self.assertEqual(self.calls[0], ("get-state",))
                self.assertEqual(self.calls[-1], BOOT)
                last = {
                    args: len(self.calls) - 1 - self.calls[::-1].index(args)
                    for args in self.calls
                }
                for package in PACKAGES:
                    path_read = ("shell", "pm", "path", package)
                    hash_read = ("shell", "sha256sum", self.paths[package])
                    for args in (
                        PACKAGE_UIDS,
                        path_read,
                        hash_read,
                        PROCESSES,
                        FORWARDS,
                        BOOT,
                    ):
                        self.assertGreaterEqual(self.counts[args], 2)
                    self.assertLess(last[PACKAGE_UIDS], last[path_read])
                    self.assertLess(last[path_read], last[hash_read])
                    self.assertLess(
                        last[hash_read], min(last[PROCESSES], last[FORWARDS])
                    )
                self.assertLess(max(last[PROCESSES], last[FORWARDS]), last[BOOT])

    def test_no_operations_preserves_explicit_default_mode_without_package_scope(self):
        command = ("shell", "cmd", "appops", "get", SETTINGS, "android:mock_location")
        for mode in ("allow", "deny", "ignore", "default"):
            with self.subTest(mode=mode):
                self.prepare()
                self.replies[command] = f"No operations.\nDefault mode: {mode}\n"
                document = json.loads(self.inspect().read_bytes())
                self.assertEqual(document["observed_mock_location_mode"], mode)
                self.assertEqual(
                    document["observed_mock_location_scopes"], {"default": mode}
                )
        for value in (
            "No operations.\nDefault mode: errored\n",
            "No operations.\nDefault mode: deny\nDefault mode: allow\n",
            "No operations.\nDefault mode: deny\nMOCK_LOCATION: allow\n",
        ):
            with self.subTest(value=value):
                self.prepare()
                self.replies[command] = value
                self.failure()

    def test_restoration_accepts_retained_deny_record_but_keeps_scope_safety(self):
        for original, restored in (
            ("No operations.\nDefault mode: deny\n", "MOCK_LOCATION: deny\n"),
            ("MOCK_LOCATION: deny\n", "MOCK_LOCATION: deny; time=+1s ago\n"),
            (
                "Uid mode: MOCK_LOCATION: allow\nMOCK_LOCATION: allow\n",
                "MOCK_LOCATION: allow\nUid mode: MOCK_LOCATION: allow\n",
            ),
        ):
            with self.subTest(original=original, restored=restored):
                require_mock_location_restored(original, restored)

        original = "No operations.\nDefault mode: deny\n"
        for restored in (
            "MOCK_LOCATION: allow\n",
            "MOCK_LOCATION: default\n",
            "MOCK_LOCATION: ignore\n",
            "Uid mode: MOCK_LOCATION: deny\n",
            "Uid mode: MOCK_LOCATION: deny\nMOCK_LOCATION: deny\n",
            "Uid mode: MOCK_LOCATION: allow\nMOCK_LOCATION: deny\n",
            "No operations.\nDefault mode: allow\n",
            "MOCK_LOCATION: deny\nMOCK_LOCATION: deny\n",
            "unrecognized output\n",
        ):
            with self.subTest(restored=restored), self.assertRaises(RuntimeError):
                require_mock_location_restored(original, restored)

        for original, restored in (
            ("MOCK_LOCATION: allow\n", "Uid mode: MOCK_LOCATION: allow\n"),
            ("MOCK_LOCATION: deny\n", "No operations.\nDefault mode: deny\n"),
            ("No operations.\nDefault mode: allow\n", "MOCK_LOCATION: allow\n"),
        ):
            with self.subTest(original=original), self.assertRaises(RuntimeError):
                require_mock_location_restored(original, restored)

    def test_uid_scope_and_matching_package_appops_modes_are_observations(self):
        command = ("shell", "cmd", "appops", "get", SETTINGS, "android:mock_location")
        for mode in ("allow", "deny", "ignore", "default"):
            for include_package in (False, True):
                with self.subTest(mode=mode, include_package=include_package):
                    self.prepare()
                    self.replies[command] = f"Uid mode: MOCK_LOCATION: {mode}\n"
                    if include_package:
                        self.replies[command] += (
                            f"MOCK_LOCATION: {mode}; time=+1s ago\n"
                        )
                    path = self.inspect()
                    document = json.loads(path.read_bytes())
                    self.assertEqual(document["observed_mock_location_mode"], mode)
                    expected_scopes = {"uid": mode}
                    if include_package:
                        expected_scopes["package"] = mode
                    self.assertEqual(
                        document["observed_mock_location_scopes"], expected_scopes
                    )
                    self.assertEqual(document["status"], "inventory-verified")
                    self.assertEqual(document["automation_admission"], "pending")
                    self.assertTrue(document["remaining_requirements"])

    def test_conflicting_uid_and_package_appops_modes_are_not_collapsed(self):
        command = ("shell", "cmd", "appops", "get", SETTINGS, "android:mock_location")
        for uid_mode, package_mode in (("allow", "deny"), ("deny", "allow")):
            for uid_first in (True, False):
                with self.subTest(uid_mode=uid_mode, uid_first=uid_first):
                    self.prepare()
                    lines = [
                        f"Uid mode: MOCK_LOCATION: {uid_mode}\n",
                        f"MOCK_LOCATION: {package_mode}\n",
                    ]
                    self.replies[command] = "".join(
                        lines if uid_first else reversed(lines)
                    )
                    self.failure()

    def test_local_candidate_validation_finishes_before_first_sdk_command(self):
        for fault in (
            "missing-expected",
            "extra-expected",
            "uppercase",
            "short",
            "wrong-hash",
            "changed-local",
            "missing-apk",
            "multiple-server-apks",
        ):
            with self.subTest(fault=fault):
                self.prepare()
                if fault == "missing-expected":
                    del self.expected[TEST]
                elif fault == "extra-expected":
                    self.expected["com.example.foreign"] = "a" * 64
                elif fault in ("uppercase", "short", "wrong-hash"):
                    self.expected[SETTINGS] = {
                        "uppercase": "A" * 64,
                        "short": "a" * 63,
                        "wrong-hash": "0" * 64,
                    }[fault]
                elif fault == "changed-local":
                    self.apks[TEST].write_bytes(b"replacement APK")
                elif fault == "missing-apk":
                    self.apks[SETTINGS].unlink()
                else:
                    self.apks[SERVER].with_name(
                        "appium-uiautomator2-server-v0.apk"
                    ).write_bytes(b"stale APK")
                self.failure(result=False)
                self.sdk.assert_not_called()
                if self.output.exists():
                    self.assertEqual(
                        json.loads((self.output / "result.json").read_bytes())[
                            "status"
                        ],
                        "failed",
                    )

    def test_absent_split_unsafe_or_wrong_installed_helper_is_not_repaired(self):
        for package in PACKAGES:
            for fault in ("absent", "split", "unsafe-path", "hash", "hash-wrong-path"):
                with self.subTest(package=package, fault=fault):
                    self.prepare()
                    path_read = ("shell", "pm", "path", package)
                    path = self.paths[package]
                    if fault == "absent":
                        self.replies[path_read] = ""
                    elif fault == "split":
                        self.replies[path_read] += (
                            f"package:{path.removesuffix('base.apk')}split_config.apk\n"
                        )
                    elif fault == "unsafe-path":
                        self.replies[path_read] = "package:/data/app/x/../x/base.apk\n"
                    else:
                        self.replies[("shell", "sha256sum", path)] = (
                            f"{'0' * 64}  {path}\n"
                            if fault == "hash"
                            else f"{self.expected[package]}  /data/app/foreign/base.apk\n"
                        )
                    self.failure()

    def test_user_uid_instrumentation_and_policy_readbacks_fail_closed(self):
        cases = (
            (("shell", "am", "get-current-user"), "10\n"),
            (
                ("shell", "pm", "list", "users"),
                "Users:\n UserInfo{0:Owner:c13} running\n UserInfo{10:Work:30} running\n",
            ),
            (PACKAGE_UIDS, "package:io.appium.settings uid:not-a-uid\n"),
            (INSTRUMENTATION, ""),
            (
                INSTRUMENTATION,
                f"instrumentation:{TEST}/foreign.Runner (target={SERVER})\n",
            ),
            (
                INSTRUMENTATION,
                f"instrumentation:{COMPONENT} (target=com.example.foreign)\n",
            ),
            (BOOT, "not-a-boot-id\n"),
            (("shell", "settings", "get", "global", POLICIES[0]), "garbage\n"),
            (
                ("shell", "cmd", "appops", "get", SETTINGS, "android:mock_location"),
                "android:mock_location: errored\n",
            ),
        )
        for args, reply in cases:
            with self.subTest(args=args, reply=reply):
                self.prepare()
                self.replies[args] = reply
                self.failure()

    def test_shared_helper_or_foreign_package_uid_is_refused(self):
        for package in PACKAGES:
            with self.subTest(package=package):
                self.prepare()
                self.replies[PACKAGE_UIDS] += (
                    f"package:com.example.foreign uid:{self.uids[package]}\n"
                )
                self.failure()
        self.prepare()
        self.replies[PACKAGE_UIDS] = self.replies[PACKAGE_UIDS].replace(
            "uid:10103", "uid:10102"
        )
        self.failure()

    def test_unknown_server_settings_suffix_foreign_uid_and_uiautomator_processes(self):
        for uid, args in (
            (10102, SERVER),
            (10103, TEST),
            (10200, SERVER + ":foreign"),
            (10102, "com.example.foreign"),
            (10103, "com.example.foreign"),
            (10101, SETTINGS + ":unknown"),
            (10200, SETTINGS + ":unknown"),
            (10101, "com.example.foreign"),
            (10200, SETTINGS),
            (2000, "uiautomator"),
            (2000, "/system/bin/uiautomator"),
            (
                2000,
                "app_process /system/bin com.android.commands.uiautomator.Launcher runtest",
            ),
        ):
            with self.subTest(uid=uid, args=args):
                self.prepare()
                self.replies[PROCESSES] += f"422 {uid} {args}\n"
                self.failure()

    def test_adb_cli_empty_and_trailing_line_terminators_are_not_forward_rows(self):
        for value in ("\n", "\n\n", "{serial} tcp:19000 tcp:9000\n\n"):
            with self.subTest(value=value):
                self.prepare()
                self.replies[FORWARDS] = value.format(serial=self.serial)
                document = json.loads(self.inspect().read_bytes())
                self.assertEqual(document["status"], "inventory-verified")
                self.assertEqual(document["automation_admission"], "pending")
        for value in (
            "\n{serial} tcp:19000 tcp:9000\n",
            "{serial} tcp:19000 tcp:9000\n\n{serial} tcp:19001 tcp:9001\n",
        ):
            with self.subTest(malformed=value):
                self.prepare()
                self.replies[FORWARDS] = value.format(serial=self.serial)
                self.failure()

    def test_forward_collision_is_refused_without_deleting_any_forward(self):
        for device, local, remote in (
            ("same", 19001, 6790),
            ("same", 19002, 7810),
            ("same", 18200, 9000),
            ("foreign", 18200, 9000),
            ("same", 19003, "06790"),
            ("same", 19004, "07810"),
            ("foreign", "018200", 9000),
            ("same", "", 9000),
            ("foreign", "18200junk", 9000),
            ("same", 19005, "not-a-port"),
            ("same", 19006, 65536),
            ("foreign", -1, 9000),
        ):
            with self.subTest(device=device, local=local, remote=remote):
                self.prepare()
                serial = self.serial if device == "same" else "synthetic-foreign-phone"
                self.replies[FORWARDS] = f"{serial} tcp:{local} tcp:{remote}\n"
                self.failure()

    def test_reverse_sweep_rejects_same_mode_moving_from_package_to_uid_scope(self):
        command = ("shell", "cmd", "appops", "get", SETTINGS, "android:mock_location")
        self.replies[command] = "MOCK_LOCATION: allow\n"
        self.later[command] = "Uid mode: MOCK_LOCATION: allow\n"
        error = self.failure()
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("changed", str(error))
        self.assertGreaterEqual(self.counts[command], 2)
        document = json.loads((self.output / "result.json").read_bytes())
        self.assertEqual(document["automation_admission"], "pending")

    def test_reverse_sweep_detects_boot_uid_path_hash_process_and_forward_drift(self):
        for fault in ("boot", "uid", "path", "hash", "process", "forward"):
            with self.subTest(fault=fault):
                self.prepare()
                if fault == "boot":
                    self.later[BOOT] = "87654321-4321-4324-8324-cba987654321\n"
                elif fault == "uid":
                    self.later[PACKAGE_UIDS] = self.replies[PACKAGE_UIDS].replace(
                        "uid:10101", "uid:10199"
                    )
                elif fault == "path":
                    self.later[("shell", "pm", "path", SETTINGS)] = (
                        "package:/data/app/reinstalled/base.apk\n"
                    )
                elif fault == "hash":
                    self.later[("shell", "sha256sum", self.paths[TEST])] = (
                        f"{'0' * 64}  {self.paths[TEST]}\n"
                    )
                elif fault == "process":
                    self.later[PROCESSES] = (
                        self.replies[PROCESSES] + f"422 10102 {SERVER}\n"
                    )
                else:
                    self.later[FORWARDS] = f"{self.serial} tcp:19000 tcp:6790\n"
                self.failure()

    def test_local_hash_is_rechecked_after_every_command_including_last(self):
        self.inspect()
        command_count = len(self.calls)
        for position in range(1, command_count + 1):
            with self.subTest(position=position):
                self.prepare()

                def replace_apk(_args, position=position):
                    if len(self.calls) == position:
                        package = PACKAGES[(position - 1) % len(PACKAGES)]
                        self.apks[package].write_bytes(b"changed during SDK read")

                self.after_command = replace_apk
                self.failure()
                self.assertEqual(len(self.calls), position)

    def test_real_lease_is_checked_before_io_and_after_every_command(self):
        owner_checks = []
        checks_at_sdk = []
        previous = sys.getprofile()

        def observe(frame, event, _arg):
            if (
                event == "call"
                and frame.f_code is AndroidRadioController._require_owner.__code__
                and frame.f_locals["self"] is self.phone
            ):
                owner_checks.append(len(self.calls))

        self.after_command = lambda _args: checks_at_sdk.append(len(owner_checks))
        try:
            sys.setprofile(observe)
            self.inspect()
        finally:
            sys.setprofile(previous)
        self.assertGreaterEqual(checks_at_sdk[0], 1)
        for before, after in pairwise(checks_at_sdk):
            self.assertGreaterEqual(after - before, 2)  # Previous post + next pre.
        self.assertGreater(len(owner_checks), checks_at_sdk[-1])
        command_count = len(self.calls)
        self.prepare()
        self.lose_lease()
        self.failure(result=False)
        self.sdk.assert_not_called()
        for position in range(1, command_count + 1):
            with self.subTest(position=position):
                self.prepare()

                def replace_owner(_args, position=position):
                    if len(self.calls) == position:
                        self.lose_lease()

                self.after_command = replace_owner
                self.failure()
                self.assertEqual(len(self.calls), position)

    def test_sdk_failure_stays_primary_with_post_command_owner_failure_notes(self):
        self.replies[("get-state",)] = "synthetic SDK denial\n"
        self.returncodes[("get-state",)] = 37
        self.after_command = lambda _args: self.lose_lease()
        error = self.failure()
        self.assertIsInstance(error, android_sdk.AndroidSdkError)
        self.assertIn("37", str(error))
        self.assertIn("synthetic SDK denial", str(error))
        self.assertRegex(" ".join(getattr(error, "__notes__", ())), "(?i)(owner|lease)")
        self.assertEqual(len(self.calls), 1)

    def test_publications_use_real_exclusive_publisher_for_result_and_receipts(self):
        published: list[Path] = []
        previous = sys.getprofile()

        def observe(frame, event, _arg):
            if event == "call" and frame.f_code is publish_new_json.__code__:
                published.append(frame.f_locals["path"])

        try:
            sys.setprofile(observe)
            self.inspect()
        finally:
            sys.setprofile(previous)
        self.assertIn(self.output / "result.json", published)
        self.assertEqual(len(published), len(set(published)))
        receipts = [path for path in published if path != self.output / "result.json"]
        self.assertGreaterEqual(len(receipts), len(self.calls))
        for path in published:
            self.assertTrue(path.is_relative_to(self.output))
            self.assertIsInstance(json.loads(path.read_bytes()), dict)
        # Inject a competing real inode at the publisher entry, not a mock writer.
        for collision in ("result", "receipt"):
            with self.subTest(collision=collision):
                self.prepare()
                conflicts: list[Path] = []

                def compete(
                    frame, event, _arg, conflicts=conflicts, collision=collision
                ):
                    if event != "call" or frame.f_code is not publish_new_json.__code__:
                        return
                    path = frame.f_locals["path"]
                    is_result = path == self.output / "result.json"
                    if not conflicts and is_result == (collision == "result"):
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with path.open("xb") as stream:
                            stream.write(b"foreign publication; preserve exactly")
                        conflicts.append(path)

                try:
                    sys.setprofile(compete)
                    self.failure(result=collision != "result")
                finally:
                    sys.setprofile(previous)
                self.assertTrue(conflicts)
                self.assertEqual(
                    conflicts[0].read_bytes(), b"foreign publication; preserve exactly"
                )

    def test_result_publication_return_revalidates_budget_owner_and_artifacts(self):
        for fault in ("deadline", "owner", "artifact"):
            for external_replacement in (False, True):
                with self.subTest(
                    fault=fault, external_replacement=external_replacement
                ):
                    self.prepare()
                    if fault == "deadline":
                        self.deadline = time.monotonic() + 1
                    result_path = self.output / "result.json"
                    original_identities = []
                    replacement_identities = []
                    failed_publications = []
                    calls_at_publication = []
                    foreign_bytes = b'{"status":"foreign","evidence":"preserve"}\n'
                    previous = sys.getprofile()

                    def on_return(
                        frame,
                        event,
                        value,
                        *,
                        fault=fault,
                        external_replacement=external_replacement,
                        result_path=result_path,
                        original_identities=original_identities,
                        replacement_identities=replacement_identities,
                        failed_publications=failed_publications,
                        calls_at_publication=calls_at_publication,
                        foreign_bytes=foreign_bytes,
                    ):
                        if (
                            event != "return"
                            or frame.f_code is not publish_new_json.__code__
                            or frame.f_locals["path"] != result_path
                            or not isinstance(value, str)
                        ):
                            return
                        status = frame.f_locals["value"].get("status")
                        info = result_path.lstat()
                        identity = (info.st_dev, info.st_ino)
                        if status == "failed":
                            failed_publications.append(identity)
                            return
                        if status != "inventory-verified" or original_identities:
                            return
                        # The real publisher has closed and linked the result inode.
                        original_identities.append(identity)
                        calls_at_publication.append(len(self.calls))
                        if external_replacement:
                            replacement = self.case / "foreign-result.json"
                            with replacement.open("xb") as stream:
                                stream.write(foreign_bytes)
                            info = replacement.lstat()
                            replacement_identities.append((info.st_dev, info.st_ino))
                            replacement.replace(result_path)
                        if fault == "deadline":
                            time.sleep(max(0, self.deadline - time.monotonic()) + 0.03)
                        elif fault == "owner":
                            self.lose_lease()
                        else:
                            self.apks[SETTINGS].write_bytes(
                                b"changed after result publication"
                            )

                    try:
                        sys.setprofile(on_return)
                        error = self.failure(result=not external_replacement)
                    finally:
                        sys.setprofile(previous)
                    self.assertEqual(len(original_identities), 1)
                    self.assertIsInstance(
                        error, TimeoutError if fault == "deadline" else RuntimeError
                    )
                    self.assertEqual(self.calls[-1], BOOT)
                    self.assertEqual(calls_at_publication, [len(self.calls)])
                    info = result_path.lstat()
                    final_identity = (info.st_dev, info.st_ino)
                    if external_replacement:
                        self.assertEqual(result_path.read_bytes(), foreign_bytes)
                        self.assertEqual(replacement_identities, [final_identity])
                        self.assertNotEqual(final_identity, original_identities[0])
                        self.assertEqual(failed_publications, [])
                    else:
                        self.assertEqual(failed_publications, [final_identity])
                        document = json.loads(result_path.read_bytes())
                        self.assertEqual(document["status"], "failed")
                        self.assertEqual(document["automation_admission"], "pending")

    def test_sdk_primary_survives_failure_result_publication_conflict(self):
        def fail_with_conflict(_args):
            (self.output / "result.json").write_bytes(b"foreign result")

        self.replies[("get-state",)] = "SDK failed first\n"
        self.returncodes[("get-state",)] = 41
        self.after_command = fail_with_conflict
        error = self.failure(result=False)
        self.assertIsInstance(error, android_sdk.AndroidSdkError)
        self.assertIn("41", str(error))
        self.assertIn("SDK failed first", str(error))
        self.assertRegex(
            " ".join(getattr(error, "__notes__", ())),
            "(?i)(publish|publication|result|evidence)",
        )
        self.assertEqual((self.output / "result.json").read_bytes(), b"foreign result")
        self.assertEqual(len(self.calls), 1)

    def test_invalid_deadlines_ports_and_sdk_timeout_do_not_retry_or_repair(self):
        for deadline in (
            float("inf"),
            float("nan"),
            True,
            "tomorrow",
            time.monotonic() - 1,
        ):
            with self.subTest(deadline=deadline):
                self.prepare()
                self.failure(result=False, deadline=deadline)
                self.sdk.assert_not_called()
        for port in (True, 0, -1, 65536, "18200"):
            with self.subTest(port=port):
                self.prepare()
                self.failure(result=False, system_port=port)
                self.sdk.assert_not_called()
        self.prepare()
        self.deadline = time.monotonic() + 1
        self.inspect()  # Exercise min(5, remaining), not just the five-second cap.
        self.prepare()

        def timeout(_args):
            raise subprocess.TimeoutExpired([str(self.adb)], 0.01)

        self.after_command = timeout
        error = self.failure()
        self.assertIsInstance(error.__cause__, subprocess.TimeoutExpired)
        self.assertEqual(len(self.calls), 1)
        self.prepare()
        self.deadline = time.monotonic() + 0.05
        self.after_command = lambda _args: time.sleep(0.06)
        self.failure()  # A late successful SDK reply cannot refresh the total budget.
        self.assertEqual(len(self.calls), 1)

    def test_oversized_command_output_is_rejected_without_further_sdk_reads(self):
        self.replies[PROCESSES] = "PID UID ARGS\n" + "x" * (1024 * 1024)
        self.failure()
        self.assertEqual(self.calls[-1], PROCESSES)
        self.assertEqual(self.counts[PROCESSES], 1)

    def test_existing_output_directory_file_or_symlink_is_never_overwritten(self):
        for kind in ("empty-directory", "directory", "file", "symlink"):
            with self.subTest(kind=kind):
                self.prepare()
                target = self.case / "foreign-evidence"
                target.mkdir()
                sentinel = target / "result.json"
                sentinel.write_bytes(b"foreign result must survive")
                if kind == "empty-directory":
                    self.output.mkdir()
                elif kind == "directory":
                    self.output.mkdir()
                    (self.output / "result.json").write_bytes(b"original result")
                elif kind == "file":
                    self.output.write_bytes(b"original file")
                else:
                    self.output.symlink_to(target, target_is_directory=True)
                self.failure(result=False)
                self.sdk.assert_not_called()
                self.assertEqual(sentinel.read_bytes(), b"foreign result must survive")
                if kind == "directory":
                    self.assertEqual(
                        (self.output / "result.json").read_bytes(), b"original result"
                    )
                elif kind == "empty-directory":
                    self.assertEqual(list(self.output.iterdir()), [])
                elif kind == "file":
                    self.assertEqual(self.output.read_bytes(), b"original file")
                else:
                    self.assertTrue(self.output.is_symlink())


if __name__ == "__main__":
    unittest.main()
