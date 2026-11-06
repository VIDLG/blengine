"""Offline host wire/SDK regressions; no device, permission or RF operations."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock

from tests.peripherals.nrf52840 import permission_transition as wire
from tools.react_native import permission_transition_capture as api
from tools.tests.react_native.support.permission_host_samples import Harness, bootstrap


class PermissionCaptureTests(unittest.TestCase):
    def harness(self, directory: str, stack: ExitStack) -> Harness:
        return Harness(Path(directory), stack)

    def test_three_fresh_phases_one_stream_and_original_provider_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = self.harness(directory, stack)
            text = h.capture.run()
            runtime = h.verify()
            self.assertEqual(runtime["counts"]["alive_probes"], 3)
            self.assertEqual(runtime["counts"]["closed_zero_snapshots"], 3)
            self.assertIsNone(runtime["active_closed_resources"])
            self.assertEqual(
                h.events,
                [
                    "launch-denied",
                    "retire-denied",
                    "launch-active",
                    "arm",
                    "launch-restored",
                ],
            )
            self.assertEqual(h.arm_calls, 1)
            self.assertEqual(sum("logcat" in command for command in h.commands), 1)
            self.assertEqual(sum("force-stop" in command for command in h.commands), 1)
            self.assertFalse(
                any(
                    "svc" in command or "kill" in command or "install" in command
                    for command in h.commands
                )
            )
            self.assertEqual(text, h.log.read_text())
            self.assertEqual(text, (h.root / wire.RUNTIME_NAME).read_text())
            raw = (h.log.parent / api.RAW_NAME).read_text()
            self.assertEqual(h.verify(raw)["runtime_sha256"], runtime["runtime_sha256"])
            self.assertTrue(all(child.poll() is not None for child in h.children))
            result = json.loads((h.log.parent / api.RESULT_NAME).read_bytes())
            self.assertEqual(
                result["phases"]["active"]["native_bootstrap"], bootstrap("active")
            )
            self.assertTrue(result["phases"]["active"]["dead"])
            self.assertFalse(result["phases"]["restored"]["retired"])
            self.assertEqual(len({e["pid"] for e in result["phases"].values()}), 3)
            self.assertEqual(
                len(list(h.log.parent.glob("permission-transition-*.identity.json"))), 3
            )

    def test_silence_is_minimum_observation_not_identity_roundtrip_deadline(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = self.harness(directory, stack)
            pin = h.capture._pin
            silence = h.capture._silence
            observations = []

            def slow_pin(boundary: str) -> dict:
                if boundary == "phase silence":
                    h.sleep(0.5)
                return pin(boundary)

            def observe() -> None:
                h.capture.next_identity = h.phone.now
                began = h.phone.now
                silence()
                observations.append(h.phone.now - began)

            h.capture._pin = slow_pin
            h.capture._silence = observe
            h.capture.run()
            h.verify()
            self.assertEqual(len(observations), 2)
            self.assertTrue(all(elapsed >= 0.5 for elapsed in observations))
            self.assertTrue(all(child.poll() is not None for child in h.children))

    def test_silence_cannot_shorten_observation_or_escape_absolute_deadline(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = self.harness(directory, stack)
            h.capture.deadline = h.phone.now + api.SILENCE_SECONDS / 2
            with self.assertRaises(TimeoutError):
                h.capture._silence()
            self.assertGreaterEqual(h.phone.now, h.capture.deadline)
            self.assertEqual(h.commands, [])

    def test_wrong_pid_uid_js_before_native_and_live_terminal_fail_closed(self) -> None:
        for fault in ("pid", "uid", "js-first", "late-terminal"):
            with (
                self.subTest(fault=fault),
                tempfile.TemporaryDirectory() as directory,
                ExitStack() as stack,
            ):
                h = self.harness(directory, stack)
                h.fault = fault
                if fault == "pid":
                    h.marker_pid = 9999
                with self.assertRaises(RuntimeError):
                    h.capture.run()
                self.assertFalse(h.permission.death_confirmed)
                self.assertNotIn("launch-restored", h.events)
                self.assertTrue(all(child.poll() is not None for child in h.children))
                self.assertTrue((h.log.parent / api.RAW_NAME).exists())

    def test_rom_survival_unknown_reason_and_proof_publication_preserve_primary(
        self,
    ) -> None:
        for fault in ("survival", "reason10", "proof-write"):
            with (
                self.subTest(fault=fault),
                tempfile.TemporaryDirectory() as directory,
                ExitStack() as stack,
            ):
                h = self.harness(directory, stack)
                if fault == "survival":
                    h.phone.kill_on_revoke = False
                elif fault == "reason10":
                    h.phone.bad_record = {"reason": 10}
                else:
                    h.phone.bad_record = {"reason": 10}
                    h.capture._persist_os_proof = Mock(
                        side_effect=OSError("proof disk failure")
                    )
                with self.assertRaises((RuntimeError, TimeoutError)) as caught:
                    h.capture.run()
                self.assertFalse(h.permission.death_confirmed)
                self.assertFalse((h.root / wire.LOSS_CONFIRMED_NAME).exists())
                self.assertEqual(sum("force-stop" in c for c in h.commands), 1)
                self.assertNotIn("launch-restored", h.events)
                self.assertFalse(h.capture.death_window)
                if fault == "proof-write":
                    self.assertIn("reason8", str(caught.exception))
                    self.assertIn(
                        "proof disk failure", " ".join(caught.exception.__notes__)
                    )
                else:
                    self.assertTrue((h.log.parent / api.PROOF_NAME).exists())

    def test_authorization_budget_anchors_first_file_observation_not_later_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = self.harness(directory, stack)
            revoke = h.capture._revoke

            def delayed() -> None:
                h.sleep(4.01)
                revoke()

            h.capture._revoke = delayed
            with self.assertRaisesRegex(TimeoutError, "authorization-to-revoke"):
                h.capture.run()
            self.assertFalse(
                any(
                    a["label"] == "revoke-connected"
                    for a in h.permission.document["actions"]
                )
            )
            self.assertFalse(h.permission.death_confirmed)
            self.assertFalse((h.root / wire.LOSS_CONFIRMED_NAME).exists())
            self.assertEqual(sum("force-stop" in c for c in h.commands), 1)

    def test_irreversible_window_blocks_force_stop_relaunch_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = self.harness(directory, stack)
            h.capture.death_window = True
            for operation in (
                lambda: h.capture._adb(
                    "shell", "am", "force-stop", "com.blehubnitrohost"
                ),
                lambda: h.capture._adb("shell", "am", "kill", "com.blehubnitrohost"),
                lambda: h.capture._launch_phase("restored"),
                h.capture.stop_owned_app,
            ):
                with self.assertRaises(RuntimeError):
                    operation()
            self.assertEqual(h.commands, [])

    def test_capture_exit_and_old_pid_late_tail_are_not_adopted(self) -> None:
        for fault in ("exit", "late-tail"):
            with (
                self.subTest(fault=fault),
                tempfile.TemporaryDirectory() as directory,
                ExitStack() as stack,
            ):
                h = self.harness(directory, stack)
                original = h.provider_step

                def provider(h=h, original=original, fault=fault) -> None:
                    original()
                    if h.capture.facts.phase == "active":
                        if fault == "exit":
                            h.stream_child.returncode = 0
                        else:
                            h.append(
                                [f"passed:phase=denied:runId={h.capture.run_id}"],
                                pid=bootstrap("denied")["pid"],
                            )
                        h.provider_step = lambda: None

                h.provider_step = provider
                with self.assertRaises(RuntimeError):
                    h.capture.run()
                self.assertNotIn("arm", h.events)
                self.assertFalse(h.permission.death_confirmed)

    def test_stale_evidence_is_not_overwritten_or_used_for_a_new_attempt(self) -> None:
        for name in (
            api.PROOF_NAME,
            api.RAW_NAME,
            api.RESULT_NAME,
            wire.RUNTIME_NAME,
            "permission-transition-active.identity.json",
        ):
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory() as directory,
                ExitStack() as stack,
            ):
                h = self.harness(directory, stack)
                stale = h.log.parent / name
                stale.write_bytes(b"stale evidence must survive\n")
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    h.capture.run()
                self.assertEqual(stale.read_bytes(), b"stale evidence must survive\n")
                self.assertEqual(len(h.permission.document["actions"]), 1)
                self.assertEqual(h.children, [])

    def test_invalid_budget_rejected_without_capture(self) -> None:
        for timeout in (True, 0, -1, 301, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                api.validate_inputs("synthetic-device", timeout)

    def test_cleanup_requires_native_receipt_exact_instance_and_preserves_final_app(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            h = self.harness(directory, stack)
            h.capture.stop_owned_app()
            self.assertEqual(h.commands, [])
            h.capture.run()
            h.permission.finish(None)
            owned_pid = h.pid
            h.pid = 9999
            with self.assertRaisesRegex(RuntimeError, "pinned PID"):
                h.capture.stop_owned_app()
            self.assertEqual(sum("force-stop" in c for c in h.commands), 1)
            h.pid = owned_pid
            h.capture.stop_owned_app()
            self.assertEqual(h.events[-1], "retire-restored")
            self.assertTrue(h.capture.result["phases"]["restored"]["retired"])
