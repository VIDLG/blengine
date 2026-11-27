"""Host-only radio-control boundary tests; mocked ADB is never RF evidence."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.android.state_control import (
    AndroidRadioController,
    radio_state,
    verify_state_control,
)

RUN = "11111111-1111-4111-8111-111111111111"
DEVICE = "test-physical-serial"


class RadioControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.controller = AndroidRadioController(
            DEVICE, RUN, self.root / "control.json", lock_root=self.root / "locks"
        )
        self.adb = patch(
            "tools.android.adb.adb_executable", return_value=Path("/sdk/adb")
        )
        self.adb.start()
        self.addCleanup(self.adb.stop)

    def replies(self, *states: str):
        samples = iter(states)

        def command(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv[:4], ["/sdk/adb", "-s", DEVICE, "shell"])
            self.assertGreater(kwargs["timeout"], 0)
            self.assertLessEqual(kwargs["timeout"], 5)
            output = "state: " + next(samples) + "\n" if "dumpsys" in argv else ""
            return subprocess.CompletedProcess(argv, 0, output)

        return patch("tools.android.adb.run_command", side_effect=command)

    def test_complete_readback_journal_is_accepted_by_independent_checker(self) -> None:
        with self.replies("ON", "TURNING_OFF", "OFF", "TURNING_ON", "ON", "ON"):
            self.controller.acquire()
            self.controller.initial()
            self.controller.off()
            self.controller.on()
            self.assertEqual(self.controller.finish(None), [])
        value = json.loads((self.root / "control.json").read_text())
        verify_state_control(value, RUN, DEVICE)
        self.assertFalse(self.controller.lock.exists())

    def test_observation_finish_is_read_only_on_success_and_failure(self) -> None:
        for primary in (None, RuntimeError("permission gate failed")):
            with self.subTest(primary=primary), tempfile.TemporaryDirectory() as temp:
                controller = AndroidRadioController(
                    DEVICE,
                    RUN,
                    Path(temp) / "control.json",
                    lock_root=Path(temp) / "locks",
                )
                with self.replies("ON", "ON") as commands:
                    controller.acquire()
                    controller.initial()
                    self.assertEqual(controller.finish_observation(primary), [])
                self.assertEqual(commands.call_count, 2)
                self.assertTrue(
                    all("dumpsys" in c.args[0] for c in commands.call_args_list)
                )
                self.assertEqual(
                    controller.document["status"],
                    "passed" if primary is None else "failed",
                )
                self.assertEqual(
                    [a["label"] for a in controller.document["actions"]],
                    ["initial", "final"],
                )
                self.assertFalse(controller.lock.exists())
                with patch("tools.android.adb.run_command") as late:
                    self.assertEqual(
                        controller.finish_observation(primary),
                        ["radio controller was already finished"],
                    )
                late.assert_not_called()

    def test_observation_final_failure_cleans_app_before_release_without_radio_change(
        self,
    ) -> None:
        self.controller.acquire()
        with self.replies("ON"):
            self.controller.initial()
        original_release = self.controller._release
        order = []

        def release() -> None:
            order.append("release")
            original_release()

        def stop_app() -> None:
            self.controller.require_owner()
            order.append("stop-owned-app")
            raise RuntimeError("app cleanup failed")

        with (
            self.replies("OFF") as commands,
            patch.object(self.controller, "_release", side_effect=release),
        ):
            errors = self.controller.finish_observation(None, on_failure=stop_app)
        self.assertEqual(order, ["stop-owned-app", "release"])
        self.assertEqual(commands.call_count, 1)
        self.assertIn("dumpsys", commands.call_args.args[0])
        self.assertTrue(any("final radio ON" in e for e in errors))
        self.assertTrue(any("app cleanup failed" in e for e in errors))
        self.assertEqual(self.controller.document["status"], "failed")
        self.assertFalse(self.controller.lock.exists())

    def test_short_off_budget_reaches_each_subprocess_with_decreasing_remaining_time(
        self,
    ) -> None:
        now = [100.0]
        timeouts: list[float] = []
        spent = iter((1.25, 0.75, 0.25))
        states = iter(("TURNING_OFF", "OFF"))

        def command(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            timeouts.append(kwargs["timeout"])
            now[0] += next(spent)
            output = "state: " + next(states) + "\n" if "dumpsys" in argv else ""
            return subprocess.CompletedProcess(argv, 0, output)

        def sleep(seconds: float) -> None:
            now[0] += seconds

        with (
            patch(
                "tools.android.state_control.time.monotonic", side_effect=lambda: now[0]
            ),
            patch("tools.android.state_control.time.sleep", side_effect=sleep),
        ):
            self.controller.acquire()
            with self.replies("ON"):
                self.controller.initial()
            with patch(
                "tools.android.state_control.subprocess.run", side_effect=command
            ):
                self.controller.off(timeout=4.0)
            self.assertEqual(len(timeouts), 3)
            for actual, expected in zip(timeouts, (4.0, 2.75, 1.9), strict=True):
                self.assertAlmostEqual(actual, expected)
            with self.replies("ON", "ON"):
                self.controller.on()
                self.assertEqual(self.controller.finish(None), [])
        verify_state_control(self.controller.document, RUN, DEVICE)

    def test_timed_out_off_has_no_retry_and_restore_gets_an_independent_budget(
        self,
    ) -> None:
        now = [100.0]
        restore_timeouts: list[float] = []

        def hung(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(kwargs["timeout"], 4.0)
            now[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        def restore(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            restore_timeouts.append(kwargs["timeout"])
            # Restoration takes nine simulated seconds: it must not inherit OFF's 4s.
            now[0] += 4.5
            return subprocess.CompletedProcess(
                argv, 0, "state: ON\n" if "dumpsys" in argv else ""
            )

        with patch(
            "tools.android.state_control.time.monotonic", side_effect=lambda: now[0]
        ):
            self.controller.acquire()
            with self.replies("ON"):
                self.controller.initial()
            with (
                patch(
                    "tools.android.state_control.subprocess.run", side_effect=hung
                ) as off_command,
                self.assertRaises(subprocess.TimeoutExpired) as raised,
            ):
                self.controller.off(timeout=4.0)
            off_command.assert_called_once()
            self.assertIsNone(
                self.controller.document["actions"][1]["confirmed_monotonic"]
            )
            with patch(
                "tools.android.state_control.subprocess.run", side_effect=restore
            ):
                self.assertEqual(self.controller.finish(raised.exception), [])
        self.assertEqual(restore_timeouts, [5.0, 5.0, 5.0])
        self.assertEqual(self.controller.document["status"], "failed")
        self.assertEqual(self.controller.document["final_state"], "ON")
        self.assertEqual(
            [a["label"] for a in self.controller.document["actions"]],
            ["initial", "off", "restore", "final"],
        )
        self.assertFalse(self.controller.lock.exists())

    def test_late_successful_readback_is_not_accepted_as_off_confirmation(self) -> None:
        now = [100.0]
        received: list[float] = []

        def command(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            received.append(kwargs["timeout"])
            now[0] += kwargs["timeout"] + 0.01 if "dumpsys" in argv else 0.25
            return subprocess.CompletedProcess(
                argv, 0, "state: OFF\n" if "dumpsys" in argv else ""
            )

        with patch(
            "tools.android.state_control.time.monotonic", side_effect=lambda: now[0]
        ):
            self.controller.acquire()
            with self.replies("ON"):
                self.controller.initial()
            with (
                patch(
                    "tools.android.state_control.subprocess.run", side_effect=command
                ),
                self.assertRaisesRegex(TimeoutError, "returned after") as raised,
            ):
                self.controller.off(timeout=4.0)
            self.assertEqual(received, [4.0, 3.75])
            self.assertIsNone(
                self.controller.document["actions"][1]["confirmed_monotonic"]
            )
            self.assertIn(
                "error", self.controller.document["actions"][1]["receipts"][-1]
            )
            with self.replies("ON", "ON"):
                self.assertEqual(self.controller.finish(raised.exception), [])

    def test_lookup_that_exhausts_the_off_budget_cannot_start_a_child(self) -> None:
        now = [100.0]

        def late_lookup() -> Path:
            now[0] += 4.0
            return Path("/sdk/adb")

        with patch(
            "tools.android.state_control.time.monotonic", side_effect=lambda: now[0]
        ):
            self.controller.acquire()
            with self.replies("ON"):
                self.controller.initial()
            with (
                patch(
                    "tools.android.adb.adb_executable",
                    side_effect=late_lookup,
                ),
                patch("tools.android.adb.run_command") as child,
                self.assertRaisesRegex(TimeoutError, "before command") as raised,
            ):
                self.controller.off(timeout=4.0)
            child.assert_not_called()
            with self.replies("ON", "ON"):
                self.assertEqual(self.controller.finish(raised.exception), [])

    def test_refuses_preexisting_lock_without_stealing_or_writing_evidence(
        self,
    ) -> None:
        self.controller.acquire()
        owner = (self.controller.lock / "owner.json").read_bytes()
        other = AndroidRadioController(
            DEVICE, RUN, self.root / "other.json", lock_root=self.root / "locks"
        )
        with self.assertRaises(FileExistsError):
            other.acquire()
        self.assertEqual((self.controller.lock / "owner.json").read_bytes(), owner)
        self.assertFalse(other.path.exists())

        # No initial state was established; cleanup may release only its directory.
        self.assertEqual(self.controller.finish(RuntimeError("not started")), [])

    def test_initial_off_is_not_mutated_and_is_not_passed(self) -> None:
        with self.replies("OFF") as commands:
            self.controller.acquire()
            with self.assertRaisesRegex(RuntimeError, "precondition") as raised:
                self.controller.initial()
            self.assertEqual(self.controller.finish(raised.exception), [])
        self.assertEqual(commands.call_count, 1)
        self.assertEqual(self.controller.document["status"], "failed")
        self.assertEqual(self.controller.document["initial_state"], None)

    def test_failed_partial_run_restores_on_and_preserves_failure(self) -> None:
        with self.replies("ON", "OFF", "ON", "ON"):
            self.controller.acquire()
            self.controller.initial()
            self.controller.off()
            primary = RuntimeError("RN process died")
            self.assertEqual(self.controller.finish(primary), [])
        value = self.controller.document
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["primary_error"], str(primary))
        self.assertEqual(value["final_state"], "ON")
        self.assertEqual(
            [a["label"] for a in value["actions"]],
            ["initial", "off", "restore", "final"],
        )

    def test_new_final_failure_is_restored_but_never_relabelled_passed(self) -> None:
        with self.replies("ON", "OFF", "ON", "OFF", "ON", "ON"):
            self.controller.acquire()
            self.controller.initial()
            self.controller.off()
            self.controller.on()
            errors = self.controller.finish(None)
        self.assertTrue(any("final radio readback" in error for error in errors))
        self.assertEqual(self.controller.document["status"], "failed")
        self.assertEqual(self.controller.document["final_state"], "ON")
        self.assertFalse(self.controller.lock.exists())

    def test_foreign_owner_blocks_radio_and_app_mutations_and_lock_removal(
        self,
    ) -> None:
        self.controller.acquire()
        owner_path = self.controller.lock / "owner.json"
        owner_path.write_text("foreign")
        with patch("tools.android.adb.run_command") as commands:
            with self.assertRaisesRegex(RuntimeError, "ownership"):
                self.controller.require_owner()
            with self.assertRaisesRegex(RuntimeError, "ownership"):
                self.controller.initial()
            errors = self.controller.finish(RuntimeError("owner lost"))
        commands.assert_not_called()
        self.assertTrue(errors)
        self.assertEqual(owner_path.read_text(), "foreign")

    def test_command_failure_is_recorded_not_retried_or_used_as_state_proof(
        self,
    ) -> None:
        self.controller.acquire()
        with self.replies("ON"):
            self.controller.initial()
        with (
            patch(
                "tools.android.state_control.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "denied"),
            ) as command,
            self.assertRaisesRegex(RuntimeError, "command failed"),
        ):
            self.controller.off()
        self.assertEqual(command.call_count, 1)
        with self.replies("ON", "ON"):
            self.controller.finish(RuntimeError("OFF rejected"))
        self.assertEqual(
            self.controller.document["actions"][1]["receipts"][0]["returncode"], 1
        )
        self.assertEqual(self.controller.document["status"], "failed")

    def test_hung_adb_and_ambiguous_readback_fail_closed(self) -> None:
        for sample in ("", "state: ON\nstate: OFF\n", "state: INVALID\n"):
            with self.subTest(sample=sample), self.assertRaises(RuntimeError):
                radio_state(sample)
        self.controller.acquire()
        with (
            patch(
                "tools.android.state_control.subprocess.run",
                side_effect=subprocess.TimeoutExpired("adb", 5),
            ),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            self.controller.initial()
        self.assertEqual(self.controller.finish(RuntimeError("timeout")), [])
        self.assertIn("error", self.controller.document["actions"][0]["receipts"][0])


if __name__ == "__main__":
    unittest.main()
