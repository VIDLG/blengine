"""Direct shared-pump crossing with a real production provider writer; no RF."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import write_reject as protocol
from tests.peripherals.nrf52840.cancellation_control import (
    read_document,
    write_document,
)
from tools.react_native import reject_capture, write_reject_ledger
from tools.react_native import write_reject_capture as api
from tools.react_native.capture_transport import CaptureTransport
from tools.tests.react_native.support.write_reject_capture import Replay


class WriteRejectCaptureTests(unittest.TestCase):
    def make(self, platform="android", fault=""):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Replay(Path(temporary.name), platform, fault)

    def test_both_platforms_production_writer_actual_pump_native_then_js_before_sdk_return(
        self,
    ):
        for platform in ("android", "ios"):
            with self.subTest(platform=platform):
                h = self.make(platform)
                text, cycles = h.run()
                self.assertEqual(cycles, ())
                self.assertEqual(h.controls, list(protocol.PHASES))
                self.assertTrue(h.published_during_sdk)
                self.assertTrue(h.provider_closed_after_app)
                self.assertEqual(h.provider_errors, [])
                self.assertEqual(
                    h.capture.__class__.__bases__, (reject_capture.RejectCapture,)
                )
                self.assertEqual(
                    reject_capture.RejectCapture.__bases__, (CaptureTransport,)
                )
                identity = read_document(h.log.with_suffix(".identity.json"))
                capture = read_document(h.log.with_suffix(".capture.json"))
                self.assertEqual(identity["status"], "passed")
                self.assertEqual(capture["status"], "passed")
                self.assertEqual(identity["native_acknowledged"], list(protocol.PHASES))
                self.assertEqual(identity["js_acknowledged"], list(protocol.PHASES))
                self.assertEqual(len(identity["native_signals"]), 4)
                self.assertNotIn("source", identity["bootstrap"])
                for action in capture["actions"]:
                    self.assertEqual(action["sdk_exit"], 0)
                    self.assertEqual(
                        action["url"], h.phone.control.uri(action["phase"])
                    )
                    self.assertLessEqual(action["native_ack_at"], action["js_ack_at"])
                    self.assertTrue((h.log.parent / action["sdk_output"]).exists())
                    if platform == "ios":
                        self.assertEqual(
                            action["notification"],
                            h.phone.control.notification(action["phase"]),
                        )
                        self.assertTrue((h.log.parent / action["sdk_receipt"]).exists())
                self.assertEqual(text, h.log.read_text())
                self.assertEqual(text, (h.root / "runtime.log").read_text())
                result = write_reject_ledger.verify(
                    text,
                    platform=platform,
                    run_id=h.phone.plan.session_id,
                    service_uuid=h.phone.plan.service_uuid,
                    physical_path=h.root / protocol.PHYSICAL_NAME,
                    fixture_request=h.phone.plan.request_path,
                    fixture_identity=(os.getpid(), os.getpgid(0)),
                )
                self.assertEqual(result["outcome"], "INJECTED_REJECT")
                self.assertTrue(all(c.returncode is not None for c in h.children))
                launch = next(
                    c.command
                    for c in h.children
                    if "--console" in c.command or "blehub.runtimeMode" in c.command
                )
                self.assertIn(
                    "--blehub-nitro-rf-write-reject-control"
                    if platform == "ios"
                    else "blehub.rfWriteRejectControl",
                    launch,
                )
                self.assertNotIn("blehub.rfCancellationControl", launch)

    def test_foreign_duplicate_early_ack_pid_error_and_raw_drift_fail_without_retry(
        self,
    ):
        for platform, fault in (
            ("android", "token"),
            ("ios", "boot"),
            ("android", "duplicate"),
            ("ios", "js-first"),
            ("android", "early"),
            ("android", "pid"),
            ("ios", "pid"),
            ("android", "wrong-error"),
            ("android", "raw-drift"),
            ("android", "lease"),
        ):
            with self.subTest(platform=platform, fault=fault):
                h = self.make(platform, fault)
                with self.assertRaises((ValueError, RuntimeError, OSError)):
                    h.run()
                capture = read_document(h.log.with_suffix(".capture.json"))
                self.assertEqual(capture["status"], "failed")
                self.assertTrue(h.log.with_suffix(".raw.log").exists())
                self.assertEqual(len(h.controls), len(set(h.controls)))
                self.assertTrue(all(c.returncode is not None for c in h.children))
                self.assertFalse(h.thread.is_alive())

    def test_sdk_rejection_timeout_or_wrong_official_notification_receipt_preserves_failure(
        self,
    ):
        for platform, fault in (
            ("android", "sdk-reject"),
            ("ios", "sdk-reject"),
            ("android", "sdk-timeout"),
            ("android", "ack-timeout"),
            ("ios", "sdk-receipt"),
            ("ios", "post-budget"),
            ("android", "post-budget"),
        ):
            with self.subTest(platform=platform, fault=fault):
                h = self.make(platform, fault)
                with self.assertRaises(
                    (ValueError, RuntimeError, OSError, subprocess.SubprocessError)
                ):
                    h.run()
                capture = read_document(h.log.with_suffix(".capture.json"))
                self.assertEqual(capture["status"], "failed")
                self.assertTrue(capture["primary_error"])
                self.assertEqual(len(h.controls), 1)
                self.assertFalse((h.root / "request-write-dispatched.json").exists())
                self.assertEqual(h.capture.relay.sent, ["armed"])

    def test_request_publication_orders_snapshot_first_and_consumer_resnapshots(self):
        h = self.make()
        text, _ = h.run()
        root = h.log.parent / "relay-order"
        root.mkdir()
        write_document(root / protocol.CONTROL_NAME, asdict(h.phone.control))
        relay = protocol.WriteRejectRelay(root)
        partial = text.split(
            protocol.PREFIX + h.phone.control.marker("observed", "armed")
        )[0]
        original = protocol.write_document

        def publish(path, value):
            if path.name == "request-arm.json":
                self.assertEqual((root / "runtime.log").read_text(), partial)
            original(path, value)

        with patch.object(protocol, "write_document", side_effect=publish):
            relay.observe(partial)
        owner = h.phone.owner
        facts = protocol.runtime_facts(text, run_id=h.phone.plan.session_id)
        with (
            patch.object(owner, "guard", Mock()),
            patch.object(owner, "drain", Mock()),
            patch.object(
                owner,
                "snapshot",
                side_effect=[("old snapshot", {"steps": {}}), (text, facts)],
            ) as snapshot,
        ):
            self.assertEqual(owner.wait_request("arm", time.monotonic() + 1), facts)
        self.assertEqual(snapshot.call_count, 2)
        immutable = protocol.read_document

        def frozen_only(path):
            self.assertNotEqual(path.name, protocol.PHYSICAL_NAME)
            return immutable(path)

        with patch.object(protocol, "read_document", side_effect=frozen_only):
            self.assertIsNone(h.capture.relay.pending())

    def test_native_failure_or_nonzero_close_does_not_accept_app_pass_marker(self):
        for fault in ("native-close", "provider-held"):
            with self.subTest(fault=fault):
                h = self.make(fault=fault)
                with self.assertRaises((ValueError, RuntimeError)):
                    h.run()
                self.assertEqual(
                    read_document(h.log.with_suffix(".capture.json"))["status"],
                    "failed",
                )
        h = self.make(fault="nested-error")
        text, _ = h.run()
        self.assertIn("ATT:failed:expected native error", text)
        self.assertEqual(
            read_document(h.log.with_suffix(".capture.json"))["status"], "passed"
        )

    def test_final_capture_publication_failure_downgrades_identity_and_capture(self):
        h = self.make()
        original = reject_capture.write_document
        failed = False

        def publish(path, value):
            nonlocal failed
            if (
                path == h.log.with_suffix(".capture.json")
                and value["status"] == "passed"
                and not failed
            ):
                failed = True
                raise OSError("capture publication failed")
            original(path, value)

        with (
            patch.object(reject_capture, "write_document", side_effect=publish),
            self.assertRaisesRegex(OSError, "capture publication failed"),
        ):
            h.run()
        self.assertEqual(
            read_document(h.log.with_suffix(".identity.json"))["status"], "failed"
        )
        capture = read_document(h.log.with_suffix(".capture.json"))
        self.assertEqual(capture["status"], "failed")
        self.assertIn("capture publication failed", capture["publication_error"])
        self.assertTrue(all(c.returncode is not None for c in h.children))

    def test_public_capture_rejects_invalid_or_stale_inputs_before_launch(self):
        h = self.make()
        args = (
            "android",
            "device",
            h.log,
            h.root,
            h.phone.plan.session_id,
            h.phone.plan.service_uuid,
            h.phone.fixture._profile.local_name,
        )
        for timeout in (0, -1, 301, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                api.capture(
                    *args,
                    timeout=timeout,
                    guard=h.guard,
                    fixture_identity=(os.getpid(), os.getpgid(0)),
                )
        h.log.write_text("stale")
        with self.assertRaisesRegex(ValueError, "stale"):
            api.capture(
                *args,
                timeout=3,
                guard=h.guard,
                fixture_identity=(os.getpid(), os.getpgid(0)),
            )
        self.assertEqual(h.children, [])
