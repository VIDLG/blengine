"""Synthetic Android controller protocol/ownership tests; no USB, ADB or RF.

The session parser, snapshot, receipt verifier, RuntimeFacts, ledger and bundle
remain real. Device/process boundaries are scripted. The independent physical
checker already has its own exhaustive synthetic suite.
"""

from __future__ import annotations

import io
import signal
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from tests.peripherals.nrf52840 import fixture_session
from tests.peripherals.nrf52840.cancellation_control import read_document
from tools.react_native import android_pending_reload_capture as capture_api
from tools.react_native import pending_read_reload_runner as runner
from tools.react_native import rf_evidence_bundle as evidence
from tools.tests.react_native.support.pending_reload_ledger import (
    ACTIONS,
    FOREIGN,
)
from tools.tests.react_native.support.pending_reload_runner import IDENTITY, mocked_run

RUN_ID = "12345678-1234-4123-8123-123456789abc"
PID = "4321"
DELIVERED_WARNING = (
    "Warning: Activity not started, intent has been delivered to currently running "
    "top-most instance."
)


class Clock:
    """Advance only on controller sleep, making every deadline deterministic."""

    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def raw_record(marker: str, *, pid: str = PID, newline: bytes = b"\n") -> bytes:
    return (
        f"10-14 12:00:00.001 {pid} 55 I ReactNativeJS: {capture_api.PREFIX}{marker}".encode()
        + newline
    )


@dataclass
class Frame:
    """One logcat poll; omitted documents retain the previous published state."""

    raw: bytes | None = None
    phase: str | None = None
    progress_status: str = "running"
    run_id: str = RUN_ID
    arm_url: str | None = None
    physical_status: str | None = None
    returncode: int | None = None


@contextmanager
def captured(
    root: Path, frames: list[Frame], *, control_stdout: str = "Starting: Intent"
) -> Iterator[SimpleNamespace]:
    """Run the real capture loop against files written by a synthetic logcat."""
    coordination = root / "coordination"
    coordination.mkdir()
    log = root / "runtime.log"
    clock = Clock()
    process = Mock(pid=7654)
    frame_index = 0
    commands: list[list[str]] = []
    action_polls: list[int] = []

    def poll() -> int | None:
        nonlocal frame_index
        if frame_index >= len(frames):
            return None
        frame = frames[frame_index]
        frame_index += 1
        if frame.raw is not None:
            log.with_suffix(".raw.log").write_bytes(frame.raw)
        if frame.phase is not None:
            capture_api.write_document(
                coordination / capture_api.PROGRESS_NAME,
                {
                    "schema_version": 1,
                    "run_id": frame.run_id,
                    "phase": frame.phase,
                    "status": frame.progress_status,
                    "arm_url": frame.arm_url
                    if frame.arm_url is not None
                    else f"blehub-rn-spike://arm/{frame.run_id}",
                },
            )
        if frame.physical_status is not None:
            capture_api.write_document(
                coordination / capture_api.PHYSICAL_NAME,
                {
                    "schema_version": 1,
                    "run_id": frame.run_id,
                    "status": frame.physical_status,
                },
            )
        return frame.returncode

    def command(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        action_polls.append(frame_index)
        return subprocess.CompletedProcess(argv, 0, stdout=control_stdout, stderr="")

    process.poll.side_effect = poll
    with ExitStack() as stack:
        stack.enter_context(redirect_stdout(io.StringIO()))
        stack.enter_context(
            patch.object(capture_api.time, "monotonic", clock.monotonic)
        )
        stack.enter_context(patch.object(capture_api.time, "sleep", clock.sleep))
        boot = stack.enter_context(
            patch.object(capture_api, "wait_for_initial_pid", return_value=PID)
        )
        pid = stack.enter_context(
            patch.object(capture_api, "android_pid", return_value=PID)
        )
        popen = stack.enter_context(
            patch.object(capture_api.subprocess, "Popen", return_value=process)
        )
        send = stack.enter_context(
            patch.object(capture_api.android_sdk, "run_command", side_effect=command)
        )
        guard = Mock()

        def capture() -> tuple[str, tuple[str, ...]]:
            return capture_api.capture(
                "synthetic-phone",
                log,
                coordination,
                run_id=RUN_ID,
                seconds=2,
                guard=guard,
            )

        yield SimpleNamespace(
            run=capture,
            log=log,
            coordination=coordination,
            process=process,
            popen=popen,
            pid=pid,
            boot=boot,
            guard=guard,
            send=send,
            commands=commands,
            action_polls=action_polls,
        )


class NormalizationAndControlTests(unittest.TestCase):
    def test_normalization_retains_duplicate_complete_records_and_partial_utf8(
        self,
    ) -> None:
        marker = "generation=1:runtime-start:runId=" + RUN_ID + ":nonce=synthetic-1"
        record = raw_record(marker, newline=b"\r\n")
        partial = raw_record("generation=1:scan:passed:note=测", newline=b"")[:-1]
        raw = b"--------- beginning of main\nnoise\n" + record * 2 + partial
        normalized = (capture_api.PREFIX + marker + "\n") * 2
        self.assertEqual(capture_api._normalized(raw, PID), normalized)
        complete = raw_record("generation=1:scan:passed:note=测")
        self.assertEqual(
            capture_api._normalized(record + complete, PID),
            normalized[: len(normalized) // 2]
            + capture_api.PREFIX
            + "generation=1:scan:passed:note=测\n",
        )

    def test_foreign_or_missing_pid_and_invalid_complete_utf8_are_not_filtered_away(
        self,
    ) -> None:
        for raw in (
            raw_record("passed:runId=" + RUN_ID, pid="9999"),
            (capture_api.PREFIX + "passed:runId=" + RUN_ID + "\n").encode(),
        ):
            with (
                self.subTest(raw=raw),
                self.assertRaisesRegex(RuntimeError, "foreign PID"),
            ):
                capture_api._normalized(raw, PID)
        with self.assertRaises(UnicodeDecodeError):
            capture_api._normalized(b"invalid complete UTF8: \xff\n", PID)

    def test_send_pins_pid_checks_both_boundaries_and_rejects_native_control_errors(
        self,
    ) -> None:
        cases = (
            ([PID, PID], "Starting: Intent", None, 1),
            ([PID, PID], DELIVERED_WARNING, None, 1),
            ([PID, PID], "Starting: Intent\n" + DELIVERED_WARNING + "\n", None, 1),
            ([PID, "9999"], DELIVERED_WARNING, "changed the App process", 1),
            (["9999"], "", "before control", 0),
            ([PID, "9999"], "", "changed the App process", 1),
            ([PID], "Error: permission denied", "control rejected", 1),
            ([PID], "java.lang.SecurityException", "control rejected", 1),
            ([PID], "Activity not started", "control rejected", 1),
            (
                [PID],
                "Warning: Activity not started, unable to resolve Intent",
                "control rejected",
                1,
            ),
            ([PID], DELIVERED_WARNING.removesuffix("."), "control rejected", 1),
            (
                [PID],
                DELIVERED_WARNING + "\nError: permission denied",
                "control rejected",
                1,
            ),
            (
                [PID],
                "Error: permission denied\n" + DELIVERED_WARNING,
                "control rejected",
                1,
            ),
            (
                [PID],
                DELIVERED_WARNING + "\njava.lang.SecurityException",
                "control rejected",
                1,
            ),
            (
                [PID],
                DELIVERED_WARNING + "\nActivity not started: unresolved component",
                "control rejected",
                1,
            ),
        )
        arguments = ["shell", "am", "start", "-n", "synthetic/component"]
        for pids, stdout, error, sends in cases:
            with (
                self.subTest(pids=pids, stdout=stdout),
                patch.object(capture_api, "android_pid", side_effect=pids) as pid,
                patch.object(
                    capture_api.android_sdk,
                    "run_command",
                    return_value=subprocess.CompletedProcess([], 0, stdout=stdout),
                ) as command,
            ):
                order = Mock()
                guard = Mock()
                order.attach_mock(guard, "guard")
                order.attach_mock(pid, "pid")
                order.attach_mock(command, "command")
                if error is None:
                    capture_api._send("phone", PID, arguments, guard)
                    self.assertEqual(
                        [item[0] for item in order.mock_calls],
                        ["guard", "pid", "command", "guard", "pid"],
                    )
                else:
                    with self.assertRaisesRegex(RuntimeError, error):
                        capture_api._send("phone", PID, arguments, guard)
                self.assertEqual(command.call_count, sends)
                if sends:
                    command.assert_called_once_with(
                        capture_api.android_sdk.adb_command("phone", *arguments),
                        timeout=3,
                        check=True,
                    )

    def test_send_owner_or_adb_failure_is_preserved_without_retry(self) -> None:
        for boundary in ("before", "after", "adb"):
            failure = RuntimeError("original " + boundary)
            with (
                self.subTest(boundary=boundary),
                patch.object(capture_api, "android_pid", return_value=PID),
                patch.object(capture_api.android_sdk, "run_command") as command,
            ):
                guard = Mock(
                    side_effect=[failure]
                    if boundary == "before"
                    else [None, failure]
                    if boundary == "after"
                    else None
                )
                if boundary == "adb":
                    command.side_effect = failure
                else:
                    command.return_value.stdout = ""
                with self.assertRaises(RuntimeError) as caught:
                    capture_api._send("phone", PID, ["shell", "am", "start"], guard)
                self.assertIs(caught.exception, failure)
                self.assertEqual(command.call_count, 0 if boundary == "before" else 1)


class CaptureProtocolTests(unittest.TestCase):
    def test_only_ordered_progress_drives_arm_reload_arm_with_exact_pinned_controls(
        self,
    ) -> None:
        passed = raw_record("passed:runId=" + RUN_ID)
        frames = [
            Frame(phase="armed-2"),
            Frame(phase="waiting-1"),
            Frame(phase="armed-1"),
            Frame(phase="armed-1"),
            Frame(phase="armed-2"),
            Frame(phase="waiting-1"),
            Frame(phase="waiting-1"),
            Frame(phase="armed-2"),
            Frame(raw=passed, phase="closed", physical_status="passed"),
        ]
        with (
            tempfile.TemporaryDirectory() as tmp,
            captured(Path(tmp), frames, control_stdout=DELIVERED_WARNING) as h,
        ):
            text, actions = h.run()
            self.assertEqual(actions, ACTIONS)
            self.assertEqual(h.action_polls, [3, 6, 8])
            self.assertEqual(text, capture_api.PREFIX + "passed:runId=" + RUN_ID + "\n")
            component = f"{capture_api.ANDROID_PACKAGE}/{capture_api.ANDROID_ACTIVITY}"
            arm = [
                "shell",
                "am",
                "start",
                "-n",
                component,
                "-a",
                "android.intent.action.VIEW",
                "-d",
                f"blehub-rn-spike://arm/{RUN_ID}",
            ]
            reload = [
                "shell",
                "am",
                "start",
                "-n",
                component,
                "--ez",
                "blehub.triggerReload",
                "true",
                "--es",
                "blehub.rfPendingReadRunId",
                RUN_ID,
            ]
            self.assertEqual(
                h.commands,
                [
                    capture_api.android_sdk.adb_command("synthetic-phone", *args)
                    for args in (arm, reload, arm)
                ],
            )
            document = read_document(h.log.with_suffix(".capture.json"))
            self.assertEqual(document["host_actions"], list(ACTIONS))
            self.assertEqual(document["status"], "passed")
            self.assertEqual((h.coordination / "runtime.log").read_text(), text)
            self.assertEqual(h.log.with_suffix(".raw.log").read_bytes(), passed)
            self.assertIn("--pid", h.popen.call_args.args[0])
            self.assertEqual(
                h.popen.call_args.args[0][h.popen.call_args.args[0].index("--pid") + 1],
                PID,
            )
            h.process.terminate.assert_called_once()
            h.process.wait.assert_called_once_with(timeout=3)

    def test_partial_record_is_not_published_until_newline_and_duplicates_fail_facts(
        self,
    ) -> None:
        marker = "generation=1:runtime-start:runId=" + RUN_ID + ":nonce=synthetic-1"
        raw = raw_record(marker)
        frames = [Frame(raw=raw[:-1]), Frame(raw=raw), Frame(raw=raw * 2)]
        with tempfile.TemporaryDirectory() as tmp, captured(Path(tmp), frames) as h:
            with self.assertRaisesRegex(RuntimeError, "reused runtime nonce"):
                h.run()
            self.assertEqual(
                h.log.read_text(), (capture_api.PREFIX + marker + "\n") * 2
            )
            self.assertEqual(h.log.with_suffix(".raw.log").read_bytes(), raw * 2)
            h.send.assert_not_called()
            self.assertEqual(
                read_document(h.log.with_suffix(".capture.json"))["status"],
                "failed",
            )

    def test_capture_fails_closed_on_raw_drift_overflow_exit_process_or_physical_loss(
        self,
    ) -> None:
        boot = raw_record(
            "generation=1:runtime-start:runId=" + RUN_ID + ":nonce=synthetic-1"
        )
        cases = (
            (
                "drift",
                [Frame(raw=boot), Frame(raw=boot.replace(b"12:00", b"12:01"))],
                "prefix drift",
            ),
            ("overflow", [Frame(raw=b"x" * (8 * 1024 * 1024 + 1))], "overflow"),
            (
                "exit",
                [
                    Frame(
                        raw=raw_record("passed:runId=" + RUN_ID),
                        physical_status="passed",
                        returncode=0,
                    )
                ],
                "capture exited",
            ),
            ("process", [Frame()], "replaced its process"),
            (
                "physical",
                [Frame(physical_status="failed")],
                "physical pending reload proof failed",
            ),
            (
                "progress",
                [Frame(phase="armed-1", progress_status="failed")],
                "coordinator failed",
            ),
            (
                "foreign-run",
                [Frame(phase="armed-1", run_id=FOREIGN)],
                "foreign physical progress",
            ),
            (
                "foreign-url",
                [Frame(phase="armed-1", arm_url=f"blehub-rn-spike://arm/{FOREIGN}")],
                "foreign arm URL",
            ),
            (
                "foreign-log",
                [Frame(raw=raw_record("passed:runId=" + RUN_ID, pid="9999"))],
                "foreign PID",
            ),
            (
                "native-failure",
                [Frame(raw=raw_record("generation=1:read:failed:native-error"))],
                "runtime failure",
            ),
        )
        for name, frames, message in cases:
            with (
                self.subTest(case=name),
                tempfile.TemporaryDirectory() as tmp,
                captured(Path(tmp), frames) as h,
            ):
                if name == "process":
                    h.pid.return_value = "9999"
                with self.assertRaisesRegex(RuntimeError, message):
                    h.run()
                document = read_document(h.log.with_suffix(".capture.json"))
                self.assertEqual(document["status"], "failed")
                self.assertIn(message, document["primary_error"])
                h.send.assert_not_called()
                h.process.terminate.assert_called_once()

    def test_passed_markers_cannot_substitute_for_missing_controls_or_physical_proof(
        self,
    ) -> None:
        cases = (
            (
                [
                    Frame(
                        raw=raw_record("passed:runId=" + RUN_ID),
                        physical_status="passed",
                    )
                ],
                RuntimeError,
                "host actions incomplete",
            ),
            (
                [
                    Frame(phase="armed-1"),
                    Frame(phase="waiting-1"),
                    Frame(
                        raw=raw_record("passed:runId=" + RUN_ID),
                        physical_status="passed",
                    ),
                ],
                RuntimeError,
                "host actions incomplete",
            ),
            (
                [
                    Frame(phase="armed-1"),
                    Frame(phase="waiting-1"),
                    Frame(phase="armed-2"),
                    Frame(raw=raw_record("passed:runId=" + RUN_ID)),
                ],
                TimeoutError,
                "deadline",
            ),
            (
                [
                    Frame(phase="armed-1"),
                    Frame(phase="waiting-1"),
                    Frame(phase="armed-2"),
                    Frame(physical_status="passed"),
                ],
                TimeoutError,
                "deadline",
            ),
        )
        for frames, exception, message in cases:
            with (
                self.subTest(message=message, frames=frames),
                tempfile.TemporaryDirectory() as tmp,
                captured(Path(tmp), frames, control_stdout=DELIVERED_WARNING) as h,
            ):
                with self.assertRaisesRegex(exception, message):
                    h.run()
                self.assertEqual(
                    read_document(h.log.with_suffix(".capture.json"))["status"],
                    "failed",
                )

    def test_rejected_control_is_not_recorded_as_confirmed_and_kill_is_bounded(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            captured(Path(tmp), [Frame(phase="armed-1")]) as h,
        ):
            h.send.side_effect = None
            h.send.return_value.stdout = "Error: intent denied"
            h.process.wait.side_effect = [subprocess.TimeoutExpired("logcat", 3), 0]
            with self.assertRaisesRegex(RuntimeError, "control rejected"):
                h.run()
            self.assertEqual(
                read_document(h.log.with_suffix(".capture.json"))["host_actions"],
                [],
            )
            h.process.kill.assert_called_once()
            self.assertEqual(
                h.process.wait.call_args_list, [call(timeout=3), call(timeout=3)]
            )

    def test_initial_capture_publication_failure_still_reaps_owned_logcat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, captured(Path(tmp), []) as h:
            with (
                patch.object(
                    capture_api,
                    "write_document",
                    side_effect=OSError("capture disk full"),
                ),
                self.assertRaisesRegex(OSError, "capture disk full"),
            ):
                h.run()
            h.process.terminate.assert_called_once()
            h.process.wait.assert_called_once_with(timeout=3)

    def test_capture_teardown_exception_does_not_replace_primary_protocol_error(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            captured(Path(tmp), [Frame(physical_status="failed")]) as h,
        ):
            h.process.wait.side_effect = OSError("owned capture wait failed")
            with self.assertRaisesRegex(
                RuntimeError, "physical pending reload proof failed"
            ) as caught:
                h.run()
            self.assertIn(
                "owned capture wait failed", " ".join(caught.exception.__notes__)
            )


class RunOwnershipTests(unittest.TestCase):
    def assert_restored(self, h: SimpleNamespace) -> None:
        calls = h.handlers.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertIs(calls[0].args[0], signal.SIGTERM)
        self.assertTrue(callable(calls[0].args[1]))
        self.assertEqual(calls[1], call(signal.SIGTERM, signal.SIG_IGN))
        self.assertEqual(calls[2], call(signal.SIGTERM, h.previous_handler))

    def test_real_snapshot_session_install_launch_ledger_and_manifest_follow_cleanup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp, mocked_run(Path(tmp)) as h:
            order = Mock()
            order.attach_mock(h.command, "command")
            order.attach_mock(h.receipt, "receipt")
            h.faults.permission_action = lambda: order.permissions()
            manifest = h.run()
            document = read_document(manifest)
            self.assertEqual(document["status"], "passed")
            self.assertEqual(document["scenario"], "pending-read-reload")
            snapshot = manifest.parent / document["artifact"]["path"]
            install = next(
                argv for argv in h.commands if "tools.android.install_apk" in argv
            )
            badging = next(argv for argv in h.commands if "badging" in argv)
            self.assertEqual(Path(install[install.index("--apk") + 1]), snapshot)
            self.assertEqual(Path(badging[-1]), snapshot)
            self.assertNotEqual(snapshot, h.apk)
            self.assertEqual(snapshot.read_bytes(), h.apk.read_bytes())
            plan = h.plans[-1]
            self.assertEqual(fixture_session.load_session(plan.request_path), plan)
            self.assertEqual(
                read_document(manifest.parent / "coordination" / runner.CONTROL_NAME),
                {"schema_version": 1, "run_id": plan.session_id},
            )
            profile = runner.load_session_profile(plan.contract, plan.profile_path)
            launch = next(argv for argv in h.commands if "start" in argv)
            for key, value in (
                ("blehub.rfServiceUuid", profile.service_uuid),
                ("blehub.rfExpectedLocalName", profile.local_name),
                ("blehub.rfPendingReadRunId", plan.session_id),
                (
                    "blehub.rfReloadNotificationName",
                    f"dev.blehub.rn.reload.{plan.session_id}",
                ),
            ):
                self.assertEqual(launch[launch.index(key) + 1], value)
            self.assertEqual(
                h.events,
                [
                    "badging",
                    "prepare",
                    "fixture-start",
                    "install",
                    "adb",
                    "adb",
                    "permissions",
                    "launch",
                    "capture",
                    "fixture-stop",
                    "finish",
                ],
            )
            self.assertEqual(h.receipt.call_count, 5)
            launch_index = next(
                index
                for index, interaction in enumerate(order.mock_calls)
                if interaction[0] == "command" and "start" in interaction.args[0]
            )
            self.assertEqual(order.mock_calls[launch_index - 2], call.permissions())
            self.assertEqual(
                order.mock_calls[launch_index - 1],
                call.receipt(plan, fixture_identity=IDENTITY),
            )
            self.assertEqual(
                sum(
                    interaction[0] == "receipt"
                    for interaction in order.mock_calls[:launch_index]
                ),
                3,
            )
            self.assertTrue(
                all(
                    c.kwargs["fixture_identity"] == IDENTITY
                    for c in h.receipt.call_args_list
                )
            )
            stop = next(argv for argv in h.commands if "stop-owned" in argv)
            self.assertEqual(stop[-4:], ["--pid", "1234", "--pgid", "1234"])
            self.assertFalse(h.pid_file.exists())
            self.assertFalse((manifest.parent / "coordination/abort").exists())
            self.assertEqual(
                read_document(manifest.parent / "pending-reload-ledger.json")["status"],
                "passed",
            )
            self.assert_restored(h)

    def test_failed_snapshot_identity_or_preparation_never_starts_fixture_or_app(
        self,
    ) -> None:
        for failure_point in ("snapshot", "install-identity", "session"):
            with (
                self.subTest(point=failure_point),
                tempfile.TemporaryDirectory() as tmp,
                mocked_run(Path(tmp)) as h,
                ExitStack() as stack,
            ):
                primary = RuntimeError("original " + failure_point)
                if failure_point == "snapshot":
                    stack.enter_context(
                        patch.object(
                            evidence.EvidenceBundle,
                            "prepare_artifact",
                            side_effect=primary,
                        )
                    )
                elif failure_point == "install-identity":
                    stack.enter_context(
                        patch.object(
                            runner, "verify_install_identity", side_effect=primary
                        )
                    )
                else:
                    h.preparation.side_effect = primary
                with self.assertRaises(RuntimeError) as caught:
                    h.run()
                self.assertIs(caught.exception, primary)
                self.assertFalse(
                    any(
                        "direct-start" in argv
                        or "stop-owned" in argv
                        or "tools.android.install_apk" in argv
                        or "shell" in argv
                        for argv in h.commands
                    )
                )
                bundle = h.finalizer.call_args.args[0]
                document = read_document(bundle.attempt / evidence.MANIFEST)
                self.assertEqual(document["status"], "failed")
                self.assertEqual(document["primary_error"]["message"], str(primary))
                self.assertEqual(
                    (bundle.attempt / "coordination/abort").read_text(),
                    "parent-failed\n",
                )
                self.assert_restored(h)

    def test_only_exact_official_startup_stdout_can_grant_cleanup_ownership(
        self,
    ) -> None:
        invalid = (
            "",
            "nrf52840_fixture_pid=1234\n",
            "nrf52840_fixture_pid=0\nnrf52840_fixture_pgid=1234\n",
            "nrf52840_fixture_pid=1234\nnrf52840_fixture_pid=1234\nnrf52840_fixture_pgid=1234\n",
            " nrf52840_fixture_pid=1234\nnrf52840_fixture_pgid=1234\n",
            "nrf52840_fixture_pid=1234 extra\nnrf52840_fixture_pgid=1234\n",
        )
        for stdout in invalid:
            with (
                self.subTest(stdout=stdout),
                tempfile.TemporaryDirectory() as tmp,
                mocked_run(Path(tmp)) as h,
            ):
                h.faults.start_stdout = stdout
                with self.assertRaisesRegex(RuntimeError, "direct-start"):
                    h.run()
                self.assertNotIn("fixture-stop", h.events)
                self.assertNotIn("install", h.events)
                self.assertNotIn("launch", h.events)
                h.identity.assert_not_called()
                self.assertTrue(h.pid_file.exists())
                self.assert_restored(h)

    def test_existing_failed_start_and_replaced_markers_never_adopt_or_stop_foreign(
        self,
    ) -> None:
        for boundary in (
            "existing",
            "failed-start",
            "replaced-start",
            "replaced-cleanup",
        ):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory() as tmp,
                mocked_run(Path(tmp)) as h,
            ):
                if boundary == "existing":
                    h.pid_file.write_text("9999 9999\n", encoding="utf-8")
                    h.identity.return_value = (9999, 9999)
                elif boundary == "failed-start":
                    h.faults.foreign_on_start = True
                    h.faults.start_error = subprocess.CalledProcessError(
                        2, "direct-start"
                    )
                elif boundary == "replaced-start":
                    h.faults.foreign_on_start = True
                else:

                    def replace_owner(*_args: object, **_kwargs: object) -> None:
                        h.pid_file.write_text("9999 9999\n", encoding="utf-8")
                        h.identity.return_value = (9999, 9999)
                        raise RuntimeError("native failure after owner replacement")

                    h.capture.side_effect = replace_owner
                with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                    h.run()
                self.assertEqual(h.pid_file.read_text(), "9999 9999\n")
                self.assertNotIn("fixture-stop", h.events)
                if boundary != "replaced-cleanup":
                    self.assertNotIn("launch", h.events)
                self.assert_restored(h)

    def test_native_failure_cleanup_and_unexpected_finalization_preserve_original_exception(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp, mocked_run(Path(tmp)) as h:
            primary = RuntimeError("native engine invalidation failed")
            h.faults.native_error = primary
            h.faults.stop_error = OSError("fixture cleanup failed")
            h.finalizer.side_effect = AssertionError("unexpected finalizer exception")
            with self.assertRaises(RuntimeError) as caught:
                h.run()
            self.assertIs(caught.exception, primary)
            notes = " ".join(primary.__notes__)
            self.assertIn("fixture cleanup failed", notes)
            self.assertIn("unexpected finalizer exception", notes)
            self.assertIs(h.finalizer.call_args.kwargs["primary_error"], primary)
            self.assertFalse(h.finalizer.call_args.kwargs["completed"])
            self.assertEqual(h.events[-1], "fixture-stop")
            self.assert_restored(h)

    def test_install_receipt_launch_and_checker_failures_leave_failed_manifest_and_stop_owned(
        self,
    ) -> None:
        for boundary in ("install", "receipt", "launch", "launch-delivered", "checker"):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory() as tmp,
                mocked_run(Path(tmp)) as h,
                ExitStack() as stack,
            ):
                if boundary == "install":
                    h.faults.install_error = subprocess.CalledProcessError(1, "install")
                elif boundary == "receipt":
                    h.receipt.side_effect = fixture_session.FixtureSessionError(
                        "wrong native receipt"
                    )
                elif boundary == "launch":
                    h.faults.launch_stdout = "Error: launch rejected"
                elif boundary == "launch-delivered":
                    h.faults.launch_stdout = DELIVERED_WARNING
                else:
                    stack.enter_context(
                        patch.object(
                            runner.pending_reload_ledger,
                            "verify",
                            side_effect=RuntimeError("physical checker rejected"),
                        )
                    )
                with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                    h.run()
                self.assertIn("fixture-stop", h.events)
                self.assertEqual(h.events[-1], "finish")
                if boundary in ("receipt", "install"):
                    self.assertNotIn("launch", h.events)
                if boundary == "launch-delivered":
                    h.capture.assert_not_called()
                    force_stop_index = next(
                        index
                        for index, argv in enumerate(h.commands)
                        if "force-stop" in argv
                    )
                    launch_index = next(
                        index
                        for index, argv in enumerate(h.commands)
                        if "start" in argv
                    )
                    self.assertLess(force_stop_index, launch_index)
                    self.assertEqual(
                        h.finalizer.call_args.kwargs["primary_error"].args,
                        (f"pending reload launch failed: {DELIVERED_WARNING}",),
                    )
                bundle = h.finalizer.call_args.args[0]
                self.assertEqual(
                    read_document(bundle.attempt / evidence.MANIFEST)["status"],
                    "failed",
                )
                self.assert_restored(h)

    def test_completed_run_cannot_hide_cleanup_or_finalizer_failure(self) -> None:
        for boundary in ("cleanup", "finalizer"):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory() as tmp,
                mocked_run(Path(tmp)) as h,
            ):
                if boundary == "cleanup":
                    h.faults.stop_error = RuntimeError("owned fixture could not stop")
                else:
                    h.finalizer.side_effect = AssertionError(
                        "unexpected finish failure"
                    )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "owned fixture could not stop|unexpected finish failure",
                ):
                    h.run()
                self.assertTrue(h.finalizer.call_args.kwargs["completed"])
                self.assertIsNone(h.finalizer.call_args.kwargs["primary_error"])
                self.assert_restored(h)

    def test_sigterm_interrupt_stops_owned_fixture_restores_handler_and_keeps_primary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp, mocked_run(Path(tmp)) as h:

            def interrupt(*_args: object, **_kwargs: object) -> None:
                installed_handler = h.handlers.call_args_list[0].args[1]
                installed_handler(signal.SIGTERM, None)

            h.capture.side_effect = interrupt
            with self.assertRaisesRegex(InterruptedError, "SIGTERM") as caught:
                h.run()
            self.assertIs(
                h.finalizer.call_args.kwargs["primary_error"], caught.exception
            )
            self.assertIn("fixture-stop", h.events)
            self.assertEqual(h.events[-1], "finish")
            self.assert_restored(h)

    def test_owner_lost_during_permissions_blocks_launch_not_only_later_capture(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp, mocked_run(Path(tmp)) as h:

            def replace_owner() -> None:
                h.pid_file.write_text("9999 9999\n", encoding="utf-8")
                h.identity.return_value = (9999, 9999)

            h.faults.permission_action = replace_owner
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                h.run()
            self.assertNotIn("launch", h.events)
            self.assertNotIn("fixture-stop", h.events)
            self.assertEqual(h.pid_file.read_text(), "9999 9999\n")
            self.assert_restored(h)


if __name__ == "__main__":
    unittest.main()
