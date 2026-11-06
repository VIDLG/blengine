"""Real CCC provider/capture crossing over synthetic SDK/BTP; ledger is a boundary."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from tests.peripherals.nrf52840 import ccc_disable_reject as protocol
from tests.peripherals.nrf52840.cancellation_control import read_document
from tools.tests.react_native.support.ccc_disable_reject_capture import (
    PHASES,
    Replay,
)


class CccDisableRejectCaptureTests(unittest.TestCase):
    def make(self, platform="android", fault=""):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Replay(Path(temporary.name), platform, fault)

    def test_eight_native_then_js_acks_stream_before_sdk_return_both_platforms(self):
        for platform in ("android", "ios"):
            with self.subTest(platform=platform):
                h = self.make(platform)
                text, cycles = h.run()
                self.assertEqual(cycles, ())
                protocol.runtime_facts(
                    text,
                    run_id=h.phone.plan.session_id,
                    platform=platform,
                    complete=True,
                )
                self.assertEqual(h.controls, list(PHASES))
                self.assertTrue(h.published_during_sdk)
                self.assertTrue(h.provider_closed_after_app)
                receipt = read_document(h.log.with_suffix(".capture.json"))
                identity = read_document(h.log.with_suffix(".identity.json"))
                self.assertEqual(receipt["scenario"], "ccc-disable-reject")
                self.assertEqual(receipt["status"], "passed")
                self.assertEqual(identity["status"], "passed")
                self.assertEqual(identity["native_acknowledged"], list(PHASES))
                self.assertEqual(identity["js_acknowledged"], list(PHASES))
                self.assertEqual(identity["bootstrap"]["flow"], 12)
                self.assertEqual(identity["bootstrap"]["mode"], "rf-ccc-disable-reject")
                self.assertEqual(receipt["budgets_seconds"]["ack"], 2)
                for action in receipt["actions"]:
                    self.assertEqual(action["sdk_exit"], 0)
                    self.assertLessEqual(action["native_ack_at"], action["js_ack_at"])
                    self.assertEqual(
                        action["url"], h.phone.control.uri(action["phase"])
                    )
                    self.assertTrue((h.log.parent / action["sdk_output"]).is_file())
                    if platform == "ios":
                        path = h.log.parent / action["sdk_receipt"]
                        official = read_document(path)
                        self.assertEqual(
                            official["info"]["arguments"], action["sdk_command"][1:]
                        )
                        self.assertEqual(
                            action["notification"],
                            h.phone.control.notification(action["phase"]),
                        )
                self.assertEqual(text, h.log.read_text())
                self.assertEqual(text, (h.root / "runtime.log").read_text())
                self.assertIn(
                    "rf-ccc-disable-reject:step:end-silence-read:passed", text
                )
                self.assertNotIn("rf-write-reject:", text)
                self.assertIn(
                    "rf-ccc-disable-reject:resources:phase=failed-disable:snapshot=",
                    text,
                )
                self.assertIn(
                    "rf-ccc-disable-reject:resources:phase=closed:snapshot=", text
                )
                self.assertEqual(h.provider_errors, [])
                self.assertEqual(
                    [
                        line.split(":")[2]
                        for line in text.splitlines()
                        if "rf-ccc-disable-reject:step:" in line
                    ],
                    list(protocol.STEPS),
                )
                self.assertTrue(h.ledger.verify.called)
                arguments = h.ledger.verify.call_args
                self.assertEqual(arguments.args, (text,))
                self.assertEqual(
                    arguments.kwargs["physical_path"], h.root / h.protocol.PHYSICAL_NAME
                )
                self.assertEqual(
                    arguments.kwargs["fixture_request"], h.phone.plan.request_path
                )
                self.assertEqual(
                    arguments.kwargs["fixture_identity"], (os.getpid(), os.getpgid(0))
                )
                self.assertTrue(all(c.returncode is not None for c in h.children))
                self.assertFalse(h.thread.is_alive())
                launch = next(
                    c.command
                    for c in h.children
                    if "--console" in c.command or "blehub.runtimeMode" in c.command
                )
                key = (
                    "--blehub-nitro-rf-ccc-disable-reject-control"
                    if platform == "ios"
                    else "blehub.rfCccDisableRejectControl"
                )
                encoded = launch[launch.index(key) + 1]
                self.assertEqual(
                    json.loads(
                        encoded if platform == "ios" else shlex.split(encoded)[0]
                    ),
                    asdict(h.phone.control),
                )
                if platform == "ios":
                    self.assertIn(
                        "--blehub-nitro-rf-ccc-disable-reject-service", launch
                    )
                else:
                    self.assertEqual(
                        launch[launch.index("blehub.runtimeMode") + 1],
                        "rf-ccc-disable-reject",
                    )
                self.assertNotIn("blehub.rfWriteRejectControl", launch)
                self.assertNotIn("--blehub-nitro-rf-write-reject-control", launch)

    def test_original_sdk_receipt_and_deadline_required_even_after_both_acks(self):
        for platform, fault in (
            ("android", "sdk-reject"),
            ("ios", "sdk-reject"),
            ("android", "sdk-timeout"),
            ("ios", "sdk-receipt"),
            ("android", "post-budget"),
            ("ios", "post-budget"),
            ("android", "ack-timeout"),
        ):
            with self.subTest(platform=platform, fault=fault):
                h = self.make(platform, fault)
                with self.assertRaises(
                    (ValueError, RuntimeError, OSError, subprocess.SubprocessError)
                ):
                    h.run()
                receipt = read_document(h.log.with_suffix(".capture.json"))
                self.assertEqual(receipt["status"], "failed")
                self.assertTrue(receipt["primary_error"])
                self.assertEqual(h.controls, ["baseline-value-sent"])
                self.assertEqual(h.capture.relay.sent, ["baseline-value-sent"])
                self.assertFalse((h.root / "request-arm.json").exists())
                h.ledger.verify.assert_not_called()
