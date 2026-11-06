"""Mock SDK/stream tests only; no installed artifact, hardware or RF verdict."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from tools.react_native import capture_io
from tools.react_native import recovery_ten_capture as api
from tools.react_native.host_contract import (
    ANDROID_PACKAGE,
)
from tools.tests.react_native.support.recovery_capture import (
    BOOT,
    DEVICE,
    RUN,
    SERVICE,
    Harness,
    document,
    resources,
)


class CaptureTests(unittest.TestCase):
    def test_ten_controls_same_identity_streamed_ack_before_sdk_return_and_local_detach(
        self,
    ) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), platform)
                text, actions = h.run()
                self.assertEqual(actions, tuple(range(10)))
                self.assertEqual(
                    h.controls,
                    [f"blehub-rn-spike://recover/{RUN}/{n}" for n in range(10)],
                )
                self.assertTrue(
                    all(line.startswith(api.PREFIX) for line in text.splitlines())
                )
                self.assertEqual(h.log.read_text(), text)
                self.assertEqual((h.coordination / "runtime.log").read_text(), text)
                self.assertTrue(all(c.poll() is not None for c in h.children))
                identity = api.read_document(h.log.with_suffix(".identity.json"))
                receipt = api.read_document(h.log.with_suffix(".capture.json"))
                self.assertEqual(identity["status"], "passed")
                self.assertEqual(identity["bootstrap"], h.identity)
                self.assertEqual(identity["native_acknowledged"], list(range(10)))
                self.assertEqual(identity["js_acknowledged"], list(range(10)))
                self.assertEqual(receipt["status"], "passed")
                self.assertGreater(h.guard_calls, 30)
                if platform == "ios":
                    self.assertTrue(h.published_during_relay)
                    self.assertEqual(receipt["console_cleanup"], "detached")
                    self.assertTrue(
                        h.log.with_suffix(".detached-processes.json").exists()
                    )
                    for child in h.children:
                        if (
                            "--payload-url" in child.command
                            or "processes" in child.command
                        ):
                            self.assertEqual(
                                child.command[child.command.index("--timeout") + 1], "5"
                            )
                        if "--payload-url" in child.command:
                            self.assertNotIn("--terminate-existing", child.command)
                            self.assertNotIn("--console", child.command)
                    self.assertFalse(h.log.with_suffix(".launch.json").exists())
                else:
                    console = next(c for c in h.children if "logcat" in c.command)
                    self.assertIn("--pid=42", console.command)
                    self.assertNotIn("-d", console.command)
                    self.assertIn("threadtime", console.command)
                    self.assertEqual(identity["observations"], [{"output": "42"}])
                    intents = [c.command for c in h.children if "am" in c.command]
                    self.assertEqual(len(intents), 11)
                    self.assertTrue(
                        all(
                            command[command.index("-n") + 1]
                            == "com.blehubnitrohost/com.blehubnitrohost.MainActivity"
                            for command in intents
                        )
                    )
                    self.assertIn("rf-recovery-ten", intents[0])
                    self.assertIn("blehub.rfRecoveryRunId", intents[0])
                    self.assertIn("fixture-name", intents[0])
                self.assertFalse(
                    any(
                        "install" in c.command or "flash" in c.command
                        for c in h.children
                    )
                )

    def test_rejects_early_duplicate_foreign_expired_native_failure_process_replacement_and_physical_failure(
        self,
    ) -> None:
        for fault in (
            "early",
            "duplicate",
            "foreign",
            "expired",
            "native-failed",
            "replacement",
            "physical-failed",
            "js-first",
            "guard",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), fault=fault)
                with self.assertRaises(
                    (RuntimeError, TimeoutError, subprocess.TimeoutExpired)
                ):
                    h.run()
                self.assertEqual(
                    api.read_document(h.log.with_suffix(".capture.json"))["status"],
                    "failed",
                )
                self.assertEqual(
                    api.read_document(h.log.with_suffix(".identity.json"))["status"],
                    "failed",
                )
                self.assertTrue(all(c.poll() is not None for c in h.children))
                self.assertTrue(h.log.with_suffix(".raw.log").exists())
                if fault == "early":
                    self.assertEqual(h.controls, [])

    def test_first_publication_failure_cleans_console_preserves_primary_and_does_not_mark_passed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp))
            original = api._Capture._publish

            def fail_publication(owner: api._Capture) -> None:
                if not owner.lines:
                    raise OSError("first publication failed")
                original(owner)

            with (
                patch.object(api._Capture, "_publish", fail_publication),
                self.assertRaisesRegex(OSError, "first publication failed"),
            ):
                h.run()
            self.assertTrue(all(c.poll() is not None for c in h.children))
            self.assertEqual(
                api.read_document(h.log.with_suffix(".capture.json"))["status"],
                "failed",
            )

    def test_rejected_cleanup_batch_retains_actual_runtime_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp))
            original = h.append
            failure = "cycle:0:failed:fixture advertisement timed out after 45000ms"

            def append(markers: list[str]) -> None:
                if any(marker.startswith("native:bootstrap:") for marker in markers):
                    original(
                        markers[:2]
                        + [
                            "cycle:0:start",
                            "resources:cycle=0:phase=closed:snapshot="
                            + json.dumps(resources("closed", False)),
                            failure,
                        ]
                    )
                else:
                    original(markers)

            with (
                patch.object(h, "append", side_effect=append),
                self.assertRaisesRegex(RuntimeError, "out-of-order") as raised,
            ):
                h.run()
            self.assertIn(api.PREFIX + failure, h.log.read_text())
            receipt = api.read_document(h.log.with_suffix(".capture.json"))
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["runtime_failure"], api.PREFIX + failure)
            self.assertTrue(any(failure in note for note in raised.exception.__notes__))
            self.assertTrue(all(child.poll() is not None for child in h.children))

    def test_cleanup_failure_does_not_replace_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp), fault="early")
            original = api.stop_local

            def cleanup(child: Any, *, console: bool) -> str:
                result = original(child, console=console)
                if console:
                    raise OSError("cleanup publication injected")
                return result

            with (
                patch.object(api, "stop_local", cleanup),
                self.assertRaisesRegex(RuntimeError, "early") as raised,
            ):
                h.run()
            self.assertTrue(
                any(
                    "cleanup publication injected" in note
                    for note in raised.exception.__notes__
                )
            )
            self.assertTrue(all(c.poll() is not None for c in h.children))

    def test_progress_foreign_failed_and_rewind_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp))
            owner = api._Capture(
                "ios",
                DEVICE,
                h.log,
                h.coordination,
                RUN,
                SERVICE,
                "fixture",
                900,
                Mock(),
                h.install,
            )
            h.progress(3)
            owner._progress()
            h.progress(2)
            with self.assertRaisesRegex(RuntimeError, "rewound"):
                owner._progress()
            h.progress(4)
            path = h.coordination / api.PROGRESS_NAME
            value = api.read_document(path)
            value["status"] = "failed"
            api.write_document(path, value)
            with self.assertRaisesRegex(RuntimeError, "failed"):
                owner._progress()
            value.update(status="running", run_id=BOOT)
            api.write_document(path, value)
            with self.assertRaisesRegex(RuntimeError, "foreign"):
                owner._progress()

    def test_bootstrap_exact_fields_source_installed_path_and_launch_deadline(
        self,
    ) -> None:
        faults = [
            {"base": {}},
            {"source": "native"},
            {"pid": True},
            {"bundle_id": ANDROID_PACKAGE},
            {"executable": "file:///wrong"},
            {"boot_id": "not-a-uuid"},
            {"run_id": BOOT},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp))
            for extra in faults:
                with self.subTest(extra=extra):
                    owner = api._Capture(
                        "ios",
                        DEVICE,
                        h.log,
                        h.coordination,
                        RUN,
                        SERVICE,
                        "fixture",
                        900,
                        Mock(),
                        h.install,
                    )
                    owner.boot_deadline = api.time.monotonic() + 45
                    with self.assertRaises((RuntimeError, ValueError)):
                        owner._observe(
                            api.PREFIX
                            + "native:bootstrap:"
                            + json.dumps({**h.identity, **extra})
                        )
            owner.boot_deadline = api.time.monotonic() - 1
            with self.assertRaisesRegex(TimeoutError, "launch-anchored"):
                owner._observe(
                    api.PREFIX + "native:bootstrap:" + json.dumps(h.identity)
                )

    def test_process_receipt_device_or_pid_or_executable_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp))
            owner = api._Capture(
                "ios",
                DEVICE,
                h.log,
                h.coordination,
                RUN,
                SERVICE,
                "fixture",
                900,
                Mock(),
                h.install,
            )
            owner.pid = 42
            for replacement in ("device", "pid", "executable"):

                def query(
                    command: list[str], replacement: str = replacement, **kwargs: Any
                ) -> str:
                    self.assertLessEqual(kwargs["budget"], 1.5)
                    result = document(
                        "devicectl.device.info.processes",
                        {
                            "runningProcesses": [
                                {
                                    "processIdentifier": 43
                                    if replacement == "pid"
                                    else 42,
                                    "executable": "wrong"
                                    if replacement == "executable"
                                    else owner.executable,
                                }
                            ]
                        },
                    )
                    if replacement == "device":
                        result["result"]["deviceIdentifier"] = "foreign-device"
                    api.write_document(
                        Path(command[command.index("--json-output") + 1]), result
                    )
                    return ""

                with (
                    patch.object(owner, "_command", side_effect=query),
                    self.assertRaisesRegex(RuntimeError, "identity|absent"),
                ):
                    owner._verify_process("cycle-0-before")

    def test_ack_and_sdk_budgets_are_absolute_not_the_five_second_cli_floor(
        self,
    ) -> None:
        for fault in ("expired", "command-timeout", "post-budget"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                h = Harness(Path(tmp), fault=fault)
                with self.assertRaises(
                    (TimeoutError, subprocess.TimeoutExpired)
                ) as raised:
                    h.run()
                if fault == "expired":
                    self.assertIn("original 2s deadline", str(raised.exception))
                elif fault == "command-timeout":
                    assert isinstance(raised.exception, subprocess.TimeoutExpired)
                    self.assertEqual(raised.exception.timeout, 1.5)
                    self.assertLess(h.clock.now - 100, 2)
                else:
                    assert h.capture is not None and h.capture.sent_at is not None
                    self.assertLessEqual(h.clock.now - h.capture.sent_at, 2.06)
                    self.assertEqual(h.capture.js_acks, [0])
                self.assertTrue(all(c.poll() is not None for c in h.children))
                self.assertEqual(
                    api.read_document(h.log.with_suffix(".identity.json"))["status"],
                    "failed",
                )

    def test_public_api_rejects_stale_or_foreign_inputs_without_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp))
            args = ("ios", DEVICE, h.log, h.coordination, RUN, SERVICE, "fixture")
            with patch.object(api.subprocess, "Popen") as launch:
                with self.assertRaisesRegex(ValueError, "official install"):
                    api.capture(*args, timeout=900, guard=Mock())
                with self.assertRaisesRegex(ValueError, "positive"):
                    api.capture(*args, timeout=0, guard=Mock(), install_json=h.install)
                h.log.write_text("old evidence\n")
                with self.assertRaisesRegex(ValueError, "stale evidence"):
                    api.capture(
                        *args, timeout=900, guard=Mock(), install_json=h.install
                    )
                launch.assert_not_called()

    def test_same_js_nonce_and_native_before_js_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp))
            owner = api._Capture(
                "ios",
                DEVICE,
                h.log,
                h.coordination,
                RUN,
                SERVICE,
                "fixture",
                900,
                Mock(),
                h.install,
            )
            owner.boot_deadline = api.time.monotonic() + 45
            owner._observe(api.PREFIX + "native:bootstrap:" + json.dumps(h.identity))
            owner._observe(
                api.PREFIX + f"runtime:runId={RUN}:nonce=one-boot:phase=start"
            )
            with self.assertRaisesRegex(RuntimeError, "second JS runtime"):
                owner._observe(
                    api.PREFIX + f"runtime:runId={RUN}:nonce=other-boot:phase=start"
                )


class PrefixTests(unittest.TestCase):
    def test_complete_utf8_duplicates_pid_header_prefix_growth_and_rewind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.log"
            prefix = "09-27 12:34:56.789    42    43 I ReactNativeJS: "
            line = (prefix + api.PREFIX + "native:failed:é\n").encode()
            path.write_bytes(line[:-2])
            reader = capture_io.PrefixStream(path, 42, prefix=api.PREFIX)
            self.assertEqual(reader.poll(), [])
            path.write_bytes(line + line)
            expected = api.PREFIX + "native:failed:é"
            self.assertEqual(reader.poll(), [expected, expected])
            self.assertEqual(reader.poll(), [])
            path.write_bytes(line)
            with self.assertRaisesRegex(RuntimeError, "rewound"):
                reader.poll()
            for raw in (
                line.replace(b"    42", b"    43"),
                (api.PREFIX + "cycle:0:start\n").encode(),
            ):
                path.write_bytes(raw)
                with self.assertRaisesRegex(RuntimeError, "PID/header"):
                    capture_io.PrefixStream(path, 42, prefix=api.PREFIX).poll()

    def test_size_and_invalid_complete_utf8_are_not_silently_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.log"
            path.write_bytes(b"x" * (capture_io.MAX_BYTES + 1))
            with self.assertRaisesRegex(RuntimeError, "8 MiB"):
                capture_io.PrefixStream(path, None, prefix=api.PREFIX).poll()
            path.write_bytes(b"\xff\n")
            with self.assertRaises(UnicodeDecodeError):
                capture_io.PrefixStream(path, None, prefix=api.PREFIX).poll()


if __name__ == "__main__":
    unittest.main()
