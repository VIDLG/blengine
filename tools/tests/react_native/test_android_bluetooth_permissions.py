"""Offline tests for the formal Android Bluetooth permission scope."""

from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.android import permission_control as pc
from tools.react_native.android_bluetooth_permissions import (
    AndroidBluetoothPermissionScope,
)

RUN = "21d6a93c-fc94-47cd-9863-e76f654b3c67"
DEVICE = "formal-permission-phone"
PACKAGE = "com.blehubformalhost"
UID = 10421


def permission_dump(
    scan: bool, connect: bool, *, package: str = PACKAGE, uid: int = UID
) -> str:
    return f"""Packages:
  Package [{package}] (formal-abc):
    userId={uid}
    User 0: ceDataInode=123 installed=true hidden=false
      runtime permissions:
        {pc.SCAN}: granted={str(scan).lower()}, flags=[]
        {pc.CONNECT}: granted={str(connect).lower()}, flags=[]
"""


class FakeFormalPhone:
    def __init__(self, scan: bool, connect: bool) -> None:
        self.scan = scan
        self.connect = connect
        self.uid = UID
        self.current_user = 0
        self.calls: list[list[str]] = []
        self.guarded = False
        self.fail_guard = False
        self.malformed = False

    def guard(self) -> None:
        if self.fail_guard:
            raise RuntimeError("formal phone lease lost")
        self.guarded = True

    def run(
        self, command: list[str], *, timeout: float, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        if not self.guarded:
            raise AssertionError("SDK command was not preceded by the phone guard")
        self.guarded = False
        self.calls.append(command)
        args = command[4:]
        for verb in ("grant", "revoke"):
            for permission in pc.PERMISSIONS:
                if tuple(args) == pc.permission_mutation_arguments(
                    verb, permission, use_root=True, package=PACKAGE
                ):
                    args = ["pm", verb, "--user", "0", PACKAGE, permission]
        if args[:2] == ["su", "-c"]:
            args = shlex.split(args[2])
        if args == ["id"]:
            return subprocess.CompletedProcess(command, 0, "uid=0(root) gid=0(root)\n")
        if args == ["am", "get-current-user"]:
            return subprocess.CompletedProcess(command, 0, f"{self.current_user}\n")
        if args == ["dumpsys", "package", PACKAGE]:
            output = (
                "unknown format\n"
                if self.malformed
                else permission_dump(self.scan, self.connect)
            )
            return subprocess.CompletedProcess(command, 0, output)
        if len(args) == 6 and args[0:4] == ["pm", "grant", "--user", "0"]:
            if args[4:] != [PACKAGE, args[5]]:
                raise AssertionError("mutation package was not exact")
            if args[5] == pc.SCAN:
                self.scan = True
            elif args[5] == pc.CONNECT:
                self.connect = True
            else:
                raise AssertionError("unexpected permission")
            return subprocess.CompletedProcess(command, 0, "")
        if len(args) == 6 and args[0:4] == ["pm", "revoke", "--user", "0"]:
            if args[4:] != [PACKAGE, args[5]]:
                raise AssertionError("mutation package was not exact")
            if args[5] == pc.SCAN:
                self.scan = False
            elif args[5] == pc.CONNECT:
                self.connect = False
            else:
                raise AssertionError("unexpected permission")
            return subprocess.CompletedProcess(command, 0, "")
        raise AssertionError(f"unexpected command: {command}")

    def mutation_commands(self) -> list[list[str]]:
        return [
            command
            for command in self.calls
            if command[4] == "su" and "pm" in command[6]
        ]


class AndroidBluetoothPermissionScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "formal-permissions.json"

    def scope(self, phone: FakeFormalPhone) -> AndroidBluetoothPermissionScope:
        return AndroidBluetoothPermissionScope(
            DEVICE,
            RUN,
            self.path,
            PACKAGE,
            phone.guard,
        )

    def patch_sdk(self, phone: FakeFormalPhone):
        return patch("tools.android.adb.run_command", side_effect=phone.run)

    def test_denied_baseline_grants_then_restores_exact_denied_state(self) -> None:
        phone = FakeFormalPhone(False, False)
        scope = self.scope(phone)
        with self.patch_sdk(phone):
            self.assertEqual(scope.prep(), {pc.SCAN: False, pc.CONNECT: False})
            self.assertEqual(
                scope.document["prepared_grants"], {pc.SCAN: True, pc.CONNECT: True}
            )
            self.assertEqual(scope.finish(), [])
        self.assertEqual((phone.scan, phone.connect), (False, False))
        document = json.loads(self.path.read_text())
        self.assertEqual(document["package"], PACKAGE)
        self.assertEqual(
            document["restored_grants"], {pc.SCAN: False, pc.CONNECT: False}
        )
        self.assertEqual(document["status"], "passed")
        self.assertTrue(
            any(
                "pm grant" in receipt["stdout"] or receipt["command"][4] == "su"
                for action in document["actions"]
                for receipt in action["receipts"]
            )
        )

    def test_granted_baseline_has_no_mutation_and_is_kept(self) -> None:
        phone = FakeFormalPhone(True, True)
        scope = self.scope(phone)
        with self.patch_sdk(phone):
            self.assertEqual(scope.enter(), {pc.SCAN: True, pc.CONNECT: True})
            self.assertEqual(scope.close(), [])
        self.assertEqual((phone.scan, phone.connect), (True, True))
        self.assertEqual(
            [
                command[4:]
                for command in phone.calls
                if command[4] == "su" and len(command) > 6
            ],
            [["su", "-c", "id"]],
        )

    def test_unknown_dump_uid_mismatch_and_nonroot_are_fail_closed(self) -> None:
        for field in ("malformed", "current_user"):
            phone = FakeFormalPhone(True, True)
            if field == "malformed":
                phone.malformed = True
            else:
                phone.current_user = 10
            scope = self.scope(phone)
            with (
                self.subTest(field=field),
                self.patch_sdk(phone),
                self.assertRaises(RuntimeError),
            ):
                scope.prep()
            self.assertEqual(scope.document["status"], "failed")
            self.assertEqual(phone.mutation_commands(), [])
        with self.assertRaisesRegex(ValueError, "explicit root"):
            AndroidBluetoothPermissionScope(
                DEVICE, RUN, self.path, PACKAGE, lambda: None, use_root=False
            )

    def test_lease_loss_and_cleanup_failure_are_sticky_and_quarantined(self) -> None:
        phone = FakeFormalPhone(False, False)
        scope = self.scope(phone)
        with self.patch_sdk(phone):
            scope.prep()
            phone.fail_guard = True
            errors = scope.finish()
        self.assertTrue(errors)
        self.assertEqual(scope.document["status"], "quarantined")
        with self.assertRaisesRegex(RuntimeError, "finished"):
            scope.prep()

    def test_publication_failure_is_retained_and_never_passes(self) -> None:
        phone = FakeFormalPhone(True, True)
        scope = self.scope(phone)
        with self.patch_sdk(phone):
            scope.prep()
            with patch.object(
                scope.controller, "_publish", side_effect=OSError("disk full")
            ):
                errors = scope.finish()
        self.assertTrue(errors)
        self.assertEqual(scope.document["status"], "quarantined")
        self.assertTrue(any("publication" in error for error in errors))

    def test_scope_never_launches_kills_or_changes_radio(self) -> None:
        phone = FakeFormalPhone(True, True)
        scope = self.scope(phone)
        with self.patch_sdk(phone):
            scope.prep()
            scope.finish()
        flat = " ".join(" ".join(command) for command in phone.calls)
        for forbidden in ("svc", "bluetooth_manager", "am start", "force-stop", "kill"):
            self.assertNotIn(forbidden, flat)


if __name__ == "__main__":
    unittest.main()
