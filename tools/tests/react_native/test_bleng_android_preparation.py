"""Hardware-free helper preparation, exercising the real inventory/session owners."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.android.state_control import AndroidRadioController
from tools.react_native import android_sdk
from tools.react_native import bleng_android_preparation as preparation
from tools.react_native.bleng_android_session import AndroidAutomationOwner
from tools.react_native.bleng_automation import ScanAutomationOwner
from tools.react_native.ios_radio_lease import SCAN_OBSERVATION_SCOPE, IosRadioLease
from tools.tests.react_native.support.android_automation import (
    APPOPS,
    BOOT,
    FORWARDS,
    LIVE_SERVICES,
    OS_PROCESSES,
    PS,
    SERVICES,
    STORE,
    AndroidAutomationHarness,
    active_processes,
)

PATH = "/data/local/tmp/mock_apps.json"
RAW = '["io.appium.settings"]\n'
STAT = "2049:1234:8180:1:23:2000:2000\n"
READ = (
    "shell",
    (
        f"if [ -L {PATH} ]; then exit 41; elif [ -e {PATH} ]; then "
        f"stat -c '%d:%i:%f:%h:%s:%u:%g' {PATH} && cat {PATH}; "
        "else printf 'absent\\n'; fi"
    ),
)
CREATE = (
    "shell",
    (
        f"(set -C; umask 077; printf '%s\\n' '[\"io.appium.settings\"]' > {PATH}) "
        f"&& stat -c '%d:%i:%f:%h:%s:%u:%g' {PATH} && cat {PATH}"
    ),
)
ALLOW = (
    "shell",
    "su -c 'cmd appops set --user 0 io.appium.settings android:mock_location allow'",
)
DENY = (
    "shell",
    "su -c 'cmd appops set --user 0 io.appium.settings android:mock_location deny'",
)
DELETE = (
    "shell",
    (
        f"[ ! -L {PATH} ] && [ -f {PATH} ] "
        f"&& [ \"$(stat -c '%d:%i:%f:%h:%s:%u:%g' {PATH})\" = "
        "'2049:1234:8180:1:23:2000:2000' ] "
        f"&& printf '%s\\n' '[\"io.appium.settings\"]' | cmp -s - {PATH} "
        f"&& rm {PATH}"
    ),
)


class PreparationHarness(AndroidAutomationHarness):
    # This is still entirely synthetic: all SDK subprocess and adb lookup are
    # isolated BEFORE the actual phone controller acquires its temporary lease.

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.replies[APPOPS] = "No operations.\nDefault mode: deny\n"
        self.replies[READ] = "absent\n"
        self.timeout_after: tuple[str, ...] | None = None
        self.replies.update(dict.fromkeys((ALLOW, DENY, CREATE, DELETE), ""))

    def sdk_command(self, command: list[str], **kwargs: object):
        args = tuple(command[3:])
        if args == ALLOW:
            self.replies[APPOPS] = "MOCK_LOCATION: allow\n"
        elif args == DENY:
            self.replies[APPOPS] = "MOCK_LOCATION: deny\n"
        elif args == CREATE:
            assert self.replies[READ] == "absent\n", "noclobber must not overwrite"
            self.replies[READ] = STAT + RAW
            self.replies[STORE] = RAW
            self.replies[CREATE] = STAT + RAW
        elif args == DELETE:
            assert self.replies[READ] == STAT + RAW, "delete only the owned file"
            self.replies[READ] = "absent\n"
        result = super().sdk_command(command, **kwargs)
        if self.timeout_after == args:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return result

    def scope(self, **overrides):
        values = {
            "phone": self.phone,
            "run_id": self.run_id,
            "authorization": True,
            "appium_home": self.appium_home,
            "expected_apk_sha256": self.expected,
            "system_port": self.system_port,
            "output": self.root / "preparation",
            "cwd": self.root,
            "deadline": self.deadline,
        }
        return preparation.AndroidHelperPreparation(**(values | overrides))

    def owner(self, **overrides):
        values = {
            "phone": self.phone,
            "run_id": self.run_id,
            "app_id": self.app_id,
            "appium_home": self.appium_home,
            "expected_apk_sha256": self.expected,
            "system_port": self.system_port,
            "output": self.output,
            "cwd": self.root,
            "deadline": self.deadline,
        }
        return AndroidAutomationOwner(**(values | overrides))

    def prepared_owner(self):
        owner = self.owner()
        owner.prepare()  # Never replaced by an always-success mock.
        return owner

    def closed_owner(self):
        owner = self.prepared_owner()
        owner.finalize(self.clock + 30)
        owner.require_closed()
        return owner


class AndroidHelperPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.h = self.enterContext(
            PreparationHarness(Path(self.temporary.name) / "harness")
        )

    def guard(self, scope):
        scope.require_prepared(
            phone=self.h.phone, run_id=self.h.run_id, deadline=self.h.deadline
        )

    def parent(self, helper, owner, **overrides):
        h = self.h
        values = {
            "phone": h.phone,
            "run_id": h.run_id,
            "app_id": h.app_id,
            "node": "synthetic-node",
            "appium_entry": h.root / "nonexistent-appium.js",
            "port": h.host_port,
            "output": h.root / "scan-automation",
            "cwd": h.root,
            "deadline": h.deadline,
            "activity": ".MainActivity",
            "android_owner": owner,
            "android_preparation": helper,
        }
        return ScanAutomationOwner(**(values | overrides))

    def managed_parent(self):
        h = self.h
        helper = h.scope()
        helper.prepare()
        owner = h.prepared_owner()
        parent = self.parent(helper, owner)
        original_remote = h.sdk.remote.side_effect

        def remote(url, **kwargs):
            h.active()
            driver = original_remote(url, **kwargs)
            original_quit = driver.quit.side_effect

            def quit_owned():
                original_quit()
                h.idle()

            driver.quit.side_effect = quit_owned
            return driver

        h.sdk.remote.side_effect = remote
        parent.start()
        return helper, owner, parent

    def test_parent_close_restores_helpers_before_phone_release(self):
        h = self.h
        helper, owner, parent = self.managed_parent()
        parent.require_running(deadline=h.deadline)
        stages = []

        def observe(command):
            if command in (READ, DELETE, DENY):
                self.assertIn("server-reaped", h.sdk.events)
                self.assertTrue((h.output / "finalized.json").exists())
                stages.append(command)

        h.after_command = observe
        h.clock = h.deadline + 1  # Cleanup does not extend the scenario.
        parent.close(deadline=h.clock + 30)
        owner.require_closed()
        helper.require_restored()
        parent.require_closed()
        self.assertIn(DELETE, stages)
        self.assertIn(DENY, stages)
        self.assertLess(
            h.sdk.events.index("session-quit"), h.sdk.events.index("server-reaped")
        )
        h.assert_phone_retained()
        result = json.loads((h.root / "scan-automation/close.json").read_text())
        self.assertEqual(result["helper_restoration"], "passed")
        self.assertEqual(
            h.phone.finish_app_observation(None, finalize_owned=parent.require_closed),
            [],
        )
        self.assertFalse(h.phone.lock.exists())

    def test_parent_wrong_helper_scope_rejected_before_sdk_or_popen(self):
        h = self.h
        helper = h.scope()
        helper.prepare()
        before = len(h.calls)
        wrong_hashes = h.expected | {"io.appium.settings": "0" * 64}
        for index, changes in enumerate(
            (
                {"appium_home": h.root / "other-home"},
                {"expected_apk_sha256": wrong_hashes},
                {"system_port": h.system_port + 1},
                {"deadline": h.deadline - 1},
            )
        ):
            owner = h.owner(output=h.root / f"wrong-owner-{index}", **changes)
            with (
                self.subTest(changes=changes),
                self.assertRaises((RuntimeError, ValueError)),
            ):
                self.parent(helper, owner)
        self.assertEqual(len(h.calls), before)
        h.sdk.spawn.assert_not_called()
        h.sdk.remote.assert_not_called()

    def test_parent_wrong_phone_or_run_rejected_before_sdk_or_popen(self):
        h = self.h
        helper = h.scope()
        helper.prepare()
        owner = h.owner()
        before = len(h.calls)
        with self.assertRaises(RuntimeError):
            self.parent(helper, owner, run_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        other = AndroidRadioController(
            "synthetic-other-phone",
            h.run_id,
            h.root / "other-phone.json",
            lock_root=h.root / "other-locks",
        )
        other.acquire()
        with self.assertRaises((RuntimeError, ValueError)):
            self.parent(helper, owner, phone=other)
        self.assertEqual(len(h.calls), before)
        h.sdk.spawn.assert_not_called()
        h.sdk.remote.assert_not_called()

    def test_parent_rechecks_helper_before_first_popen(self):
        h = self.h
        helper = h.scope()
        helper.prepare()
        parent = self.parent(helper, h.prepared_owner())
        before = len(h.calls)
        receipt = h.root / "preparation/prepared.json"
        receipt.write_bytes(receipt.read_bytes() + b" ")
        with self.assertRaises(RuntimeError):
            parent.start()
        self.assertEqual(len(h.calls), before)
        h.sdk.spawn.assert_not_called()
        h.sdk.remote.assert_not_called()
        with self.assertRaises(RuntimeError):
            parent.close(deadline=h.clock + 30)
        h.assert_phone_retained()

    def test_temporary_android_helper_is_rejected_by_ios_parent(self):
        h = self.h
        helper = h.scope()
        helper.prepare()
        ios = IosRadioLease(
            "00008110-001234567890ABCD",
            h.run_id,
            h.root / "ios-phone.json",
            lock_root=h.root / "ios-locks",
            scope=SCAN_OBSERVATION_SCOPE,
        )
        ios.acquire()
        before = len(h.calls)
        with self.assertRaises(TypeError):
            self.parent(
                helper,
                None,
                phone=ios,
                activity=None,
                wda_url="http://127.0.0.1:18101",
                require_platform_owner=self.fail,
                finalize_platform_owner=self.fail,
            )
        self.assertEqual(len(h.calls), before)
        h.sdk.spawn.assert_not_called()
        h.sdk.remote.assert_not_called()

    def test_temporary_helper_requires_concrete_android_parent(self):
        h = self.h
        helper = h.scope()
        helper.prepare()
        with self.assertRaises(TypeError):
            self.parent(
                helper,
                None,
                require_platform_owner=lambda deadline: None,
                finalize_platform_owner=lambda deadline: None,
            )
        h.sdk.spawn.assert_not_called()
        h.sdk.remote.assert_not_called()

    def test_parent_pre_session_spawn_failure_still_restores_helpers(self):
        h = self.h
        helper = h.scope()
        helper.prepare()
        owner = h.prepared_owner()
        parent = self.parent(helper, owner)
        h.sdk.spawn.side_effect = OSError("owned Appium spawn failed")
        with self.assertRaisesRegex(OSError, "owned Appium spawn failed"):
            parent.start()
        parent.close(deadline=h.clock + 30)
        parent.require_closed()
        helper.require_restored()
        h.sdk.remote.assert_not_called()
        h.driver.quit.assert_not_called()
        self.assertIn(DENY, h.calls)
        self.assertIn(DELETE, h.calls)

    def test_parent_failed_platform_prepare_can_restore_without_delete(self):
        h = self.h
        helper = h.scope()
        helper.prepare()
        owner = h.owner()
        h.returncodes[OS_PROCESSES] = 1
        with self.assertRaises(android_sdk.AndroidSdkError):
            owner.prepare()
        del h.returncodes[OS_PROCESSES]
        parent = self.parent(helper, owner)
        with self.assertRaises(RuntimeError):
            parent.start()
        parent.close(deadline=h.clock + 30)
        parent.require_closed()
        helper.require_restored()
        h.sdk.remote.assert_not_called()
        h.driver.quit.assert_not_called()
        self.assertFalse((h.output / "finalized.json").exists())

    def test_parent_unknown_remote_cannot_restore_even_when_idle(self):
        h = self.h
        helper = h.scope()
        helper.prepare()
        owner = h.prepared_owner()
        parent = self.parent(helper, owner)
        h.sdk.remote.side_effect = TimeoutError("Remote outcome unknown")
        with self.assertRaisesRegex(TimeoutError, "Remote outcome unknown"):
            parent.start()
        h.idle()  # Idle is not a DELETE receipt.
        cleanup_deadline = h.clock + 30
        with (
            patch.object(helper, "restore", wraps=helper.restore) as restoration,
            self.assertRaisesRegex(RuntimeError, "unknown"),
        ):
            parent.close(deadline=cleanup_deadline)
        restoration.assert_called_once_with(automation=owner, deadline=cleanup_deadline)
        self.assertIn("server-reaped", h.sdk.events)
        self.assertNotIn(DENY, h.calls)
        self.assertNotIn(DELETE, h.calls)
        with self.assertRaises(RuntimeError):
            parent.require_closed()
        h.assert_phone_retained()

    def test_parent_helper_file_drift_fails_and_quarantines_phone(self):
        h = self.h
        helper, _, parent = self.managed_parent()
        h.replies[READ] = STAT.replace(":1234:", ":5678:") + RAW
        with self.assertRaisesRegex(RuntimeError, "store"):
            parent.close(deadline=h.clock + 30)
        self.assertNotIn(DELETE, h.calls)
        self.assertIn(DENY, h.calls)
        with self.assertRaises(RuntimeError):
            helper.require_restored()
        errors = h.phone.finish_app_observation(
            None, finalize_owned=parent.require_closed
        )
        self.assertTrue(errors)
        self.assertEqual(h.phone.document["status"], "quarantined")
        self.assertTrue(h.phone.lock.exists())

    def test_parent_uid_appops_drift_still_attempts_helper_restore(self):
        h = self.h
        helper, owner, parent = self.managed_parent()
        original_quit = h.driver.quit.side_effect

        def quit_with_uid_drift():
            original_quit()
            h.replies[APPOPS] = "Uid mode: MOCK_LOCATION: allow\n"

        h.driver.quit.side_effect = quit_with_uid_drift
        with (
            patch.object(helper, "restore", wraps=helper.restore) as restoration,
            self.assertRaises(RuntimeError),
        ):
            parent.close(deadline=h.clock + 30)
        self.assertIs(restoration.call_args.kwargs["automation"], owner)
        restoration.assert_called_once()
        self.assertNotIn(DENY, h.calls)
        with self.assertRaises(RuntimeError):
            parent.require_closed()
        h.assert_phone_retained()

    def test_parent_running_and_cleanup_refuse_changed_helper_evidence(self):
        h = self.h
        _, _, parent = self.managed_parent()
        receipt = h.root / "preparation/prepared.json"
        receipt.write_bytes(receipt.read_bytes() + b" ")
        with self.assertRaises(RuntimeError):
            parent.require_running(deadline=h.deadline)
        with self.assertRaises(RuntimeError):
            parent.close(deadline=h.clock + 30)
        with self.assertRaises(RuntimeError):
            parent.require_closed()
        self.assertNotIn(DELETE, h.calls)
        h.assert_phone_retained()

    def test_parent_closed_guard_rechecks_restored_helper_evidence(self):
        h = self.h
        _, _, parent = self.managed_parent()
        parent.close(deadline=h.clock + 30)
        parent.require_closed()
        receipt = h.root / "preparation/restored.json"
        receipt.write_bytes(receipt.read_bytes() + b" ")
        errors = h.phone.finish_app_observation(
            None, finalize_owned=parent.require_closed
        )
        self.assertTrue(errors)
        self.assertEqual(h.phone.document["status"], "quarantined")
        self.assertTrue(h.phone.lock.exists())

    def test_parent_reap_failure_remains_primary_but_helper_rollback_runs(self):
        h = self.h
        helper, _, parent = self.managed_parent()
        h.sdk.kill.side_effect = OSError("owned server reap failure")
        with self.assertRaisesRegex(RuntimeError, "owned server reap failure"):
            parent.close(deadline=h.clock + 30)
        helper.require_restored()
        self.assertIn(DENY, h.calls)
        self.assertIn(DELETE, h.calls)
        with self.assertRaises(RuntimeError):
            parent.require_closed()
        h.assert_phone_retained()

    def test_real_inventory_owner_and_semantic_restore(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        self.guard(scope)
        owner = h.closed_owner()
        scope.restore(automation=owner, deadline=h.clock + 30)
        scope.require_restored()
        self.assertEqual([c for c in h.calls if c in (ALLOW, DENY)], [ALLOW, DENY])
        self.assertEqual(h.calls.count(CREATE), 1)
        self.assertEqual(h.calls.count(DELETE), 1)
        self.assertGreater(h.calls.index(ALLOW), h.calls.index(FORWARDS))
        result = json.loads((h.root / "preparation/restored.json").read_text())
        self.assertTrue(result["record_shape_changed"])
        self.assertEqual(result["before"], "No operations.\nDefault mode: deny\n")
        self.assertEqual(result["after"], "MOCK_LOCATION: deny\n")
        h.assert_phone_retained()
        with self.assertRaises(RuntimeError):
            self.guard(scope)
        with self.assertRaises(RuntimeError):
            scope.prepare()
        with self.assertRaises(RuntimeError):
            scope.restore(automation=owner, deadline=h.clock + 30)

    def test_authorization_same_lease_and_fresh_output_before_io(self):
        h = self.h
        for overrides in (
            {"authorization": False},
            {"authorization": 1},
            {"run_id": "other"},
            {"phone": object()},
        ):
            with (
                self.subTest(overrides=overrides),
                self.assertRaises((ValueError, TypeError)),
            ):
                h.scope(**overrides)
        self.assertEqual(h.calls, [])
        h.scope()
        with self.assertRaises(FileExistsError):
            h.scope()
        self.assertEqual(h.calls, [])

    def test_rejects_emulator_and_noncanonical_lease_device_before_io(self):
        h = self.h
        for device in ("emulator-5554", "", " synthetic-phone", "phone;cmd"):
            with (
                self.subTest(device=device),
                patch.object(h.phone, "device", device),
                self.assertRaises(ValueError),
            ):
                h.scope()
        self.assertEqual(h.calls, [])
        h.assert_phone_retained()

    def test_lease_device_drift_permanently_revokes_prepared_guard(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        before = len(h.calls)
        with (
            patch.object(h.phone, "device", "synthetic-other-phone"),
            self.assertRaisesRegex(RuntimeError, "ownership changed"),
        ):
            self.guard(scope)
        self.assertEqual(len(h.calls), before)
        with self.assertRaises(RuntimeError):
            self.guard(scope)
        h.assert_phone_retained()

    def test_live_inventory_rejects_hash_process_forward_and_uid(self):
        h = self.h
        cases = [
            (PS, "PID UID ARGS\n123 10102 io.appium.uiautomator2.server\n"),
            (FORWARDS, f"{h.serial} tcp:{h.system_port} tcp:6790\n"),
            (("shell", "sha256sum", h.paths["io.appium.settings"]), "wrong\n"),
            (
                ("shell", "pm", "list", "packages", "-U", "--user", "0"),
                "package:io.appium.settings uid:10101\n",
            ),
        ]
        for index, (command, reply) in enumerate(cases):
            original = h.replies[command]
            h.replies[command] = reply
            with self.subTest(command=command):
                scope = h.scope(output=h.root / f"rejected-{index}")
                with self.assertRaises(RuntimeError):
                    scope.prepare()
                self.assertNotIn(ALLOW, h.calls)
            h.replies[command] = original

    def test_unsupported_baseline_never_mutates(self):
        h = self.h
        for index, raw in enumerate(
            (
                "Uid mode: MOCK_LOCATION: deny\n",
                "MOCK_LOCATION: allow\n",
                "No operations.\n",
            )
        ):
            h.replies[APPOPS] = raw
            scope = h.scope(output=h.root / f"bad-mode-{index}")
            with self.assertRaises(RuntimeError):
                scope.prepare()
        h.replies[APPOPS] = "MOCK_LOCATION: deny\n"
        h.replies[READ] = STAT + RAW
        scope = h.scope(output=h.root / "existing-store")
        with self.assertRaises(RuntimeError):
            scope.prepare()
        self.assertNotIn(ALLOW, h.calls)
        self.assertNotIn(CREATE, h.calls)

    def test_file_identity_or_bytes_drift_quarantines_but_revokes_allow(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        owner = h.closed_owner()
        h.replies[READ] = STAT.replace(":1234:", ":5678:") + RAW
        with self.assertRaisesRegex(RuntimeError, "store"):
            scope.restore(automation=owner, deadline=h.clock + 30)
        self.assertNotIn(DELETE, h.calls)
        self.assertIn(DENY, h.calls)
        with self.assertRaises(RuntimeError):
            scope.require_restored()
        h.assert_phone_retained()

    def test_boot_drift_refuses_all_cleanup_mutations(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        owner = h.closed_owner()
        h.replies[BOOT] = "99999999-2222-4333-8444-555555555555\n"
        with self.assertRaisesRegex(RuntimeError, "boot_id"):
            scope.restore(automation=owner, deadline=h.clock + 30)
        self.assertNotIn(DENY, h.calls)
        self.assertNotIn(DELETE, h.calls)

    def test_install_drift_refuses_cleanup_even_with_old_closed_owner(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        owner = h.closed_owner()
        h.replies[("shell", "sha256sum", h.paths["io.appium.settings"])] = (
            "reinstalled\n"
        )
        with self.assertRaisesRegex(RuntimeError, "SHA256"):
            scope.restore(automation=owner, deadline=h.clock + 30)
        self.assertNotIn(DENY, h.calls)
        self.assertNotIn(DELETE, h.calls)

    def test_store_bytes_drift_never_deleted(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        owner = h.closed_owner()
        h.replies[READ] = STAT + RAW.replace("settings", "unknowns")
        with self.assertRaisesRegex(RuntimeError, "store"):
            scope.restore(automation=owner, deadline=h.clock + 30)
        self.assertNotIn(DELETE, h.calls)
        self.assertIn(DENY, h.calls)

    def test_no_unknown_session_cleanup_success(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        with self.assertRaisesRegex(RuntimeError, "automation"):
            scope.restore(automation=None, deadline=h.clock + 30)
        self.assertNotIn(DELETE, h.calls)
        self.assertNotIn(DENY, h.calls)
        with self.assertRaises(RuntimeError):
            scope.require_restored()

    def test_mutation_timeout_attempt_is_retired_and_still_revokes(self):
        h = self.h
        scope = h.scope()
        h.timeout_after = ALLOW
        with self.assertRaises(android_sdk.AndroidSdkError):
            scope.prepare()
        h.timeout_after = None
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            scope.restore(automation=None, deadline=h.clock + 30)
        self.assertIn(DENY, h.calls)
        self.assertNotIn(CREATE, h.calls)
        with self.assertRaises(RuntimeError):
            scope.prepare()
        with self.assertRaises(RuntimeError):
            scope.require_restored()

    def test_create_lost_receipt_never_adopts_same_bytes(self):
        h = self.h
        scope = h.scope()
        h.timeout_after = CREATE
        with self.assertRaises(android_sdk.AndroidSdkError):
            scope.prepare()
        h.timeout_after = None
        with self.assertRaises(RuntimeError):
            scope.restore(automation=None, deadline=h.clock + 30)
        self.assertIn(DENY, h.calls)
        self.assertNotIn(DELETE, h.calls)
        self.assertEqual(h.replies[READ], STAT + RAW)

    def test_publication_failure_after_create_retains_pin_for_rollback(self):
        h = self.h
        scope = h.scope()
        publish = preparation.publish_new_json

        def fail_created(path, value, **kwargs):
            if value.get("argv", [])[-1:] == [CREATE[-1]]:
                raise OSError("receipt disk failure")
            return publish(path, value, **kwargs)

        with (
            patch.object(preparation, "publish_new_json", side_effect=fail_created),
            self.assertRaisesRegex(OSError, "receipt disk failure"),
        ):
            scope.prepare()
        scope.restore(automation=None, deadline=h.clock + 30)
        scope.require_restored()
        self.assertIn(DELETE, h.calls)
        result = json.loads((h.root / "preparation/restored.json").read_text())
        self.assertIn("receipt disk failure", result["preparation_error"])

    def test_commit_receipt_loss_rolls_back_without_adopting_unknown_evidence(self):
        h = self.h
        scope = h.scope()
        publish = preparation.publish_new_json

        def lose_commit(path, value, **kwargs):
            digest = publish(path, value, **kwargs)
            if value.get("argv", [])[-1:] == [CREATE[-1]]:
                raise OSError("commit receipt lost")
            return digest

        with (
            patch.object(preparation, "publish_new_json", side_effect=lose_commit),
            self.assertRaisesRegex(OSError, "commit receipt lost"),
        ):
            scope.prepare()
        with self.assertRaisesRegex(
            RuntimeError, "unknown helper evidence publication"
        ):
            scope.restore(automation=None, deadline=h.clock + 30)
        self.assertIn(DELETE, h.calls)
        self.assertIn(DENY, h.calls)
        self.assertFalse((h.root / "preparation/restored.json").exists())
        with self.assertRaises(RuntimeError):
            scope.require_restored()

    def test_primary_not_swallowed_by_publication_and_cleanup_error(self):
        h = self.h
        scope = h.scope()
        h.timeout_after = ALLOW
        publish = preparation.publish_new_json

        def fail_mutation_receipt(path, value, **kwargs):
            if (
                value.get("argv", [])[-1:] == [ALLOW[-1]]
                or path.name == "prepare-failed.json"
            ):
                raise OSError("disk")
            return publish(path, value, **kwargs)

        with (
            patch.object(
                preparation, "publish_new_json", side_effect=fail_mutation_receipt
            ),
            self.assertRaises(android_sdk.AndroidSdkError) as caught,
        ):
            scope.prepare()
        self.assertTrue(any("disk" in note for note in caught.exception.__notes__))
        h.timeout_after = None
        h.replies[APPOPS] = "Uid mode: MOCK_LOCATION: allow\n"
        with self.assertRaises(RuntimeError):
            scope.restore(automation=None, deadline=h.clock + 30)
        self.assertNotIn(DENY, h.calls)
        self.assertNotIn(DELETE, h.calls)

    def test_restore_publication_failure_is_not_cleanup_success(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        owner = h.closed_owner()
        publish = preparation.publish_new_json

        def fail_terminal(path, value, **kwargs):
            if path.name in {"restored.json", "restore-failed.json"}:
                raise OSError("terminal disk failure")
            return publish(path, value, **kwargs)

        with (
            patch.object(preparation, "publish_new_json", side_effect=fail_terminal),
            self.assertRaisesRegex(OSError, "terminal disk failure") as caught,
        ):
            scope.restore(automation=owner, deadline=h.clock + 30)
        self.assertTrue(any("publication" in n for n in caught.exception.__notes__))
        self.assertEqual(h.replies[READ], "absent\n")
        self.assertEqual(h.replies[APPOPS], "MOCK_LOCATION: deny\n")
        with self.assertRaises(RuntimeError):
            scope.require_restored()
        h.assert_phone_retained()

    def test_already_denied_needs_no_root_restore_and_no_shape_change(self):
        h = self.h
        h.replies[APPOPS] = "MOCK_LOCATION: deny; time=old\n"
        scope = h.scope()
        scope.prepare()
        owner = h.closed_owner()
        h.replies[APPOPS] = "MOCK_LOCATION: deny; time=new\n"
        scope.restore(automation=owner, deadline=h.clock + 30)
        scope.require_restored()
        self.assertNotIn(DENY, h.calls)
        result = json.loads((h.root / "preparation/restored.json").read_text())
        self.assertFalse(result["record_shape_changed"])
        self.assertNotEqual(result["before"], result["after"])

    def test_restore_timeout_is_one_shot_and_unknown_not_success(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        owner = h.closed_owner()
        h.timeout_after = DENY
        with self.assertRaises(android_sdk.AndroidSdkError):
            scope.restore(automation=owner, deadline=h.clock + 30)
        self.assertEqual(h.replies[APPOPS], "MOCK_LOCATION: deny\n")
        h.timeout_after = None
        with self.assertRaises(RuntimeError):
            scope.restore(automation=owner, deadline=h.clock + 30)
        with self.assertRaises(RuntimeError):
            scope.require_restored()

    def test_modified_owner_receipt_refuses_restoration(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        owner = h.closed_owner()
        # A stale closed receipt is not sufficient authority.
        (h.output / "finalized.json").write_text("{}\n")
        with self.assertRaises(RuntimeError):
            scope.restore(automation=owner, deadline=h.clock + 30)
        self.assertNotIn(DENY, h.calls)
        self.assertNotIn(DELETE, h.calls)

    def test_prepared_owner_without_session_can_authorize_restore(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        scope.restore(automation=h.prepared_owner(), deadline=h.clock + 30)
        scope.require_restored()
        self.assertIn(DENY, h.calls)
        self.assertIn(DELETE, h.calls)
        h.assert_phone_retained()

    def test_owner_prepare_failure_before_session_can_authorize_restore(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        owner = h.owner()
        h.returncodes[OS_PROCESSES] = 1
        with self.assertRaises(android_sdk.AndroidSdkError):
            owner.prepare()
        del h.returncodes[OS_PROCESSES]
        scope.restore(automation=owner, deadline=h.clock + 30)
        scope.require_restored()
        self.assertIn(DENY, h.calls)
        self.assertIn(DELETE, h.calls)
        h.assert_phone_retained()

    def test_requesting_owner_cannot_authorize_restore(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        owner = h.prepared_owner()
        owner.begin_session(
            server_url=f"http://127.0.0.1:{h.host_port}", deadline=h.deadline
        )
        with self.assertRaises(RuntimeError):
            scope.restore(automation=owner, deadline=h.clock + 30)
        self.assertNotIn(DENY, h.calls)
        self.assertNotIn(DELETE, h.calls)
        with self.assertRaises(RuntimeError):
            scope.require_restored()
        with self.assertRaises(RuntimeError):
            scope.restore(automation=owner, deadline=h.clock + 30)
        h.assert_phone_retained()

    def test_inventory_alone_does_not_admit_unknown_instrumentation_or_service(self):
        h = self.h
        for index, (command, raw) in enumerate(
            ((OS_PROCESSES, active_processes()), (SERVICES, LIVE_SERVICES))
        ):
            original = h.replies[command]
            h.replies[command] = raw
            scope = h.scope(output=h.root / f"unknown-activity-{index}")
            with (
                self.subTest(command=command),
                self.assertRaises((RuntimeError, ValueError)),
            ):
                scope.prepare()
            h.replies[command] = original
        self.assertNotIn(ALLOW, h.calls)
        self.assertNotIn(CREATE, h.calls)

    def test_failed_inventory_does_not_turn_unknown_session_into_restored(self):
        h = self.h
        h.replies[PS] = "PID UID ARGS\n123 10102 io.appium.uiautomator2.server\n"
        scope = h.scope()
        with self.assertRaises(RuntimeError):
            scope.prepare()
        with self.assertRaisesRegex(RuntimeError, "unknown helper"):
            scope.restore(automation=None, deadline=h.clock + 30)
        with self.assertRaises(RuntimeError):
            scope.require_restored()
        self.assertNotIn(ALLOW, h.calls)
        self.assertNotIn(DENY, h.calls)
        h.assert_phone_retained()

    def test_delete_timeout_still_revokes_but_stays_quarantined(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        owner = h.closed_owner()
        h.timeout_after = DELETE
        with self.assertRaises(android_sdk.AndroidSdkError):
            scope.restore(automation=owner, deadline=h.clock + 30)
        self.assertIn(DENY, h.calls)
        self.assertEqual(h.replies[READ], "absent\n")
        with self.assertRaises(RuntimeError):
            scope.require_restored()

    def test_terminal_deadline_loss_retracts_own_success_receipt(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        owner = h.closed_owner()
        publish = preparation.publish_new_json
        deadline = h.clock + 30

        def expire_after_publish(path, value, **kwargs):
            digest = publish(path, value, **kwargs)
            if path.name == "restored.json":
                h.clock = deadline + 1
            return digest

        with (
            patch.object(
                preparation, "publish_new_json", side_effect=expire_after_publish
            ),
            self.assertRaises(TimeoutError),
        ):
            scope.restore(automation=owner, deadline=deadline)
        self.assertFalse((h.root / "preparation/restored.json").exists())
        self.assertTrue((h.root / "preparation/restore-failed.json").exists())
        with self.assertRaises(RuntimeError):
            scope.require_restored()

    def test_restored_evidence_guard_permanently_detects_replacement(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        scope.restore(automation=h.closed_owner(), deadline=h.clock + 30)
        receipt = h.root / "preparation/restored.json"
        original = receipt.read_bytes()
        replacement = receipt.with_suffix(".replacement")
        replacement.write_bytes(original)
        replacement.replace(receipt)
        with self.assertRaises(RuntimeError):
            scope.require_restored()
        with self.assertRaises(RuntimeError):
            scope.require_restored()

    def test_evidence_change_revokes_local_guard_permanently(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        owner = h.closed_owner()
        receipt = h.root / "preparation/prepared.json"
        original = receipt.read_bytes()
        receipt.write_bytes(original + b" ")
        with self.assertRaises(RuntimeError):
            self.guard(scope)
        receipt.write_bytes(original)
        with self.assertRaises(RuntimeError):
            self.guard(scope)
        with self.assertRaisesRegex(RuntimeError, "permanently invalidated"):
            scope.restore(automation=owner, deadline=h.clock + 30)
        self.assertNotIn(DELETE, h.calls)
        self.assertNotIn(DENY, h.calls)

    def test_independent_cleanup_budget_does_not_extend_scenario(self):
        h = self.h
        scope = h.scope()
        scope.prepare()
        owner = h.closed_owner()
        h.clock = h.deadline + 1
        with self.assertRaises(TimeoutError):
            self.guard(scope)
        scope.restore(automation=owner, deadline=h.clock + 20)
        scope.require_restored()
        self.assertTrue(
            all(0 < call.kwargs["timeout"] <= 5 for call in h.command.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
