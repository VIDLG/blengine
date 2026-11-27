"""Hardware-free native HIL ownership tests; synthetic output is never RF evidence."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests.peripherals.nrf52840 import direct_fixture, fixture_session
from tests.peripherals.nrf52840.fixture_attestation import parse_attestation
from tests.peripherals.nrf52840.profile import load_profile
from tests.peripherals.nrf52840.support.direct_fixture_resources import resources
from tests.peripherals.nrf52840.support.session_receipts import write_v2_inputs
from tools.android import adb
from tools.android import native_hil as hil
from tools.android.state_control import AndroidRadioController
from tools.android_toolchain import AndroidDevice


class AndroidNativeHilTests(unittest.TestCase):
    def test_native_hil_selects_the_blehub_lane_not_the_combined_bleng_image(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[3]
        self.assertEqual(
            hil.APK_ROOT,
            root / "tests/consumers/android/blehub/app/build/outputs/apk",
        )
        self.assertEqual(
            hil.COMPONENT, hil.APP + ".test/androidx.test.runner.AndroidJUnitRunner"
        )
        self.assertEqual(hil.TEST_CLASS, hil.APP + ".NativeHilSmokeTest")

    @contextmanager
    def attempt(
        self,
        *,
        wrong_image: bool = False,
        tamper: bool = False,
        lease_loss: bool = False,
        incomplete: bool = False,
        cleanup_failure: bool = False,
    ):
        """Keep receipt validation, DirectFixture lifecycle and streaming parser real.

        Only physical lease/serial, doctor provisioning and actual ADB process
        boundaries are synthetic. No placeholder can reach the real Android SDK.
        """
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            contract, profile = write_v2_inputs(root)
            work = root / "attempts"
            apk_root = root / "apks"
            for name in (
                "debug/app-debug.apk",
                "androidTest/debug/app-debug-androidTest.apk",
            ):
                path = apk_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic APK - never install")
            commands: list[list[str]] = []
            events: list[str] = []
            device = "synthetic-phone"
            fixture = stack.enter_context(resources(events))
            fixture.session.pop_event.return_value = None
            fixture.session.poll_event.return_value = None
            stack.enter_context(
                patch.object(direct_fixture, "load_profile", side_effect=load_profile)
            )
            doctor = stack.enter_context(patch.object(direct_fixture, "NrfDoctor"))
            doctor.return_value.run.return_value.role_handles = {"updates": 909}
            installed = json.loads(contract.read_text())
            installed.pop("fixture_role")
            if wrong_image:
                installed["image_sha256"] = "cd" * 32
            attest = stack.enter_context(
                patch.object(
                    fixture_session.VendorCommands,
                    "read_image_attestation",
                    return_value=parse_attestation(installed),
                )
            )
            stack.enter_context(
                patch.object(
                    fixture_session, "identity_for_port", return_value="jlink:123456"
                )
            )
            stack.enter_context(patch.object(hil, "WORK", work))
            stack.enter_context(patch.object(hil, "APK_ROOT", apk_root))
            stack.enter_context(patch.object(hil, "adb_environment", return_value={}))
            stack.enter_context(
                patch.object(
                    hil,
                    "select_test_device",
                    return_value=AndroidDevice(device, "device", ""),
                )
            )
            stack.enter_context(
                patch.object(adb, "adb_executable", return_value=Path("synthetic-adb"))
            )
            stack.enter_context(
                patch.object(
                    hil,
                    "AndroidRadioController",
                    side_effect=lambda *args: AndroidRadioController(
                        *args,
                        lock_root=root / "phone-locks",
                    ),
                )
            )

            def current_plan():
                return fixture_session.load_session(
                    next(work.glob("*/fixture-session/fixture-request.json"))
                )

            def adb_run(command: list[str], **kwargs: Any):
                commands.append(command)
                self.assertEqual(command[:3], ["synthetic-adb", "-s", device])
                output, code = "", 0
                if "dumpsys" in command and "bluetooth_manager" in command:
                    if (
                        cleanup_failure
                        and len([c for c in commands if "bluetooth_manager" in c]) > 1
                    ):
                        raise OSError("synthetic final observation failure")
                    output = "state: ON\n"
                elif "pidof" in command:
                    code = 1
                elif "getprop" in command:
                    output = "36\n"
                elif "dumpsys" in command:
                    output = "\n".join(
                        f" android.permission.{name}: granted=true"
                        for name in ("BLUETOOTH_SCAN", "BLUETOOTH_CONNECT")
                    )
                elif "push" in command:
                    plan = current_plan()
                    fixture_session.verify_receipt(
                        plan, fixture_identity=(hil.os.getpid(), hil.os.getpgid(0))
                    )
                    self.assertEqual(attest.call_count, 1)
                    fixture.owner.require.assert_called()
                elif "install" in command:
                    output = "Success\n"
                    if tamper:
                        receipt = current_plan().receipt_path
                        receipt.chmod(0o600)
                        receipt.write_text("{}", encoding="utf-8")
                    if lease_loss:
                        fixture.owner.require.side_effect = RuntimeError(
                            "synthetic lease lost"
                        )
                return subprocess.CompletedProcess(command, code, output)

            real_popen = subprocess.Popen

            def launch(command: list[str], **kwargs: Any):
                commands.append(command)
                self.assertEqual(
                    command[:7],
                    ["synthetic-adb", "-s", device, "shell", "am", "instrument", "-w"],
                )
                plan = current_plan()
                fixture_session.verify_receipt(
                    plan, fixture_identity=(hil.os.getpid(), hil.os.getpgid(0))
                )
                self.assertIn(hil.TEST_CLASS, command)
                self.assertEqual(command[command.index("nativeHil") + 1], "writes-v2")
                self.assertIn(plan.session_id, command)
                self.assertEqual(
                    command[command.index("namePrefix") + 1],
                    load_profile(plan.profile_path).local_name,
                )
                initial = bytes.fromhex(command[command.index("initialHex") + 1])
                written = bytes.fromhex(command[command.index("writeHex") + 1])
                self.assertEqual(len(written), len(initial))
                self.assertNotEqual(written, initial)
                without_response = bytes.fromhex(
                    command[command.index("writeWithoutResponseHex") + 1]
                )
                self.assertEqual(len(without_response), len(initial))
                self.assertNotIn(without_response, (initial, written))
                self.assertEqual(
                    initial,
                    next(
                        item.initial_value
                        for item in load_profile(plan.profile_path).characteristics
                        if item.role == "read-write"
                    ),
                )
                rows = ["INSTRUMENTATION_STATUS: numtests=1\n"]
                for phase in hil.PHASES:
                    if incomplete and phase == "closed":
                        break
                    suffix = ":123:456" if phase == "started" else ""
                    rows.append(
                        f"INSTRUMENTATION_STATUS: native_hil={plan.session_id}:{phase}{suffix}\n"
                    )
                    rows.append("INSTRUMENTATION_STATUS_CODE: 2\n")
                rows.extend(
                    (
                        "INSTRUMENTATION_STATUS_CODE: 0\n",
                        "INSTRUMENTATION_RESULT: stream=\n",
                        "OK (1 test)\n",
                        "INSTRUMENTATION_CODE: -1\n",
                    )
                )
                return real_popen(
                    [
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.write(" + repr("".join(rows)) + ")",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )

            # Both actual device boundaries are intercepted for the whole attempt: the
            # bounded command seam and the streamed instrumentation launch.
            stack.enter_context(patch.object(adb, "run_command", side_effect=adb_run))
            stack.enter_context(patch.object(adb, "start_command", side_effect=launch))
            stack.enter_context(redirect_stdout(io.StringIO()))
            yield root, work, contract, profile, commands, fixture, doctor

    def test_smoke_uses_live_receipt_ordered_notification_and_read_only_cleanup(
        self,
    ) -> None:
        with self.attempt() as (root, work, contract, profile, commands, fixture, _):
            path = hil.run("synthetic-phone", "test-port", contract, profile)
            report = json.loads((path / "result.json").read_text())
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["phases"], list(hil.PHASES))
            self.assertTrue(report["peripheral_disconnected"])
            fixture.gatt.set_value.assert_called_once_with(909, bytes.fromhex("ef"))
            fixture.gatt.reset_server.assert_not_called()
            fixture.session.close.assert_called_once()
            fixture.serial.close.assert_called_once()
            self.assertEqual(len([c for c in commands if "install" in c]), 2)
            self.assertFalse(
                any("svc" in c or "grant" in c or "revoke" in c for c in commands)
            )
            self.assertEqual(list((root / "phone-locks").iterdir()), [])
            self.assertEqual(path.parent, work)

    def test_wrong_image_blocks_install_launch_and_fixture_mutation(self) -> None:
        with self.attempt(wrong_image=True) as (
            _,
            work,
            contract,
            profile,
            commands,
            fixture,
            doctor,
        ):
            with self.assertRaisesRegex(RuntimeError, "image_sha256"):
                hil.run("synthetic-phone", "test-port", contract, profile)
            self.assertFalse(any("push" in c or "instrument" in c for c in commands))
            doctor.assert_not_called()
            self.assertEqual(fixture.gap.mock_calls, [])
            fixture.session.close.assert_called_once()
            report = json.loads(next(work.glob("*/result.json")).read_text())
            self.assertEqual(report["status"], "failed")

    def test_lost_receipt_or_lease_blocks_second_install_and_launch(self) -> None:
        for options, message in (
            ({"tamper": True}, "receipt keys"),
            ({"lease_loss": True}, "synthetic lease lost"),
        ):
            with self.subTest(options=options), self.attempt(**options) as case:
                _, work, contract, profile, commands, _, _ = case
                with self.assertRaisesRegex(RuntimeError, message):
                    hil.run("synthetic-phone", "test-port", contract, profile)
                self.assertEqual(len([c for c in commands if "install" in c]), 1)
                self.assertFalse(any("instrument" in c for c in commands))
                report = json.loads(next(work.glob("*/result.json")).read_text())
                self.assertEqual(report["status"], "failed")

    def test_junit_success_does_not_replace_close_and_cleanup_cannot_mask_primary(
        self,
    ) -> None:
        with self.attempt(incomplete=True, cleanup_failure=True) as (
            _,
            work,
            contract,
            profile,
            _,
            _,
            _,
        ):
            with self.assertRaisesRegex(RuntimeError, "incomplete native HIL ledger"):
                hil.run("synthetic-phone", "test-port", contract, profile)
            report = json.loads(next(work.glob("*/result.json")).read_text())
            self.assertIn("incomplete native HIL ledger", report["primary_error"])
            self.assertTrue(
                any(
                    "synthetic final observation failure" in item
                    for item in report["cleanup_errors"]
                )
            )
            self.assertEqual(report["status"], "failed")

    def test_stream_rejects_foreign_order_duplicate_and_unbounded_capture(self) -> None:
        run = "12345678-1234-4234-8234-123456789abc"
        cases = (
            "other:started:123:456",
            f"{run}:subscribed",
            f"{run}:started:123",
            f"{run}:started:123:456\nINSTRUMENTATION_STATUS: native_hil={run}:started:123:456",
        )
        for marker in cases:
            with self.subTest(marker=marker):
                notifications = []
                stream = hil.SmokeCapture(
                    io.StringIO(),
                    run,
                    lambda: None,
                    lambda pending=notifications: pending.append(1),
                )
                with self.assertRaises(RuntimeError):
                    stream.write("INSTRUMENTATION_STATUS: native_hil=" + marker + "\n")
                self.assertEqual(notifications, [])
        stream = hil.SmokeCapture(io.StringIO(), run, lambda: None, lambda: None)
        with self.assertRaisesRegex(RuntimeError, "exceeded"):
            stream.write("x" * (hil.MAX_CAPTURE_BYTES + 1))

    def test_cleanup_refuses_foreign_or_reused_pid(self) -> None:
        stream = hil.SmokeCapture(io.StringIO(), "unused", lambda: None, lambda: None)
        stream.pid, stream.start_ticks = 123, 456
        for pid, stat in (("999", ""), ("123", "123 (app) " + " ".join(["0"] * 20))):
            with (
                self.subTest(pid=pid),
                patch.object(hil, "_command", side_effect=[pid, stat]) as command,
            ):
                with self.assertRaisesRegex(RuntimeError, "foreign|reused"):
                    hil._cleanup_app("synthetic-phone", io.StringIO(), stream)
                self.assertFalse(
                    any("force-stop" in call.args for call in command.call_args_list)
                )


if __name__ == "__main__":
    unittest.main()
