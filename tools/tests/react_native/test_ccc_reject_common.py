"""Shared CCC runner/capture safety boundaries, replayed against both named scenarios.

Only identical mechanics live here; scenario-specific phases and semantics remain
in their named tests. All SDK and Peripheral transport is owner-local synthetic.
"""

from __future__ import annotations

import json
import os
import signal
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from tests.peripherals.nrf52840.cancellation_control import read_document
from tools.react_native import reject_capture
from tools.tests.react_native.support.ccc_disable_reject_capture import (
    Replay as DisableCapture,
)
from tools.tests.react_native.support.ccc_disable_reject_parent import (
    Harness as DisableRunner,
)
from tools.tests.react_native.support.ccc_enable_reject_capture import (
    Replay as EnableCapture,
)
from tools.tests.react_native.support.ccc_enable_reject_parent import (
    Harness as EnableRunner,
)

RUNNER_CASES = (("enable", EnableRunner), ("disable", DisableRunner))
CAPTURE_CASES = (("enable", EnableCapture), ("disable", DisableCapture))


class CccRejectCommonTests(unittest.TestCase):
    @contextmanager
    def scenario(self, **parameters):
        with self.subTest(**parameters), ExitStack() as resources:
            previous = getattr(self, "resources", None)
            self.resources = resources
            try:
                yield
            finally:
                self.resources = previous

    def make(self, platform="android", *args):
        directory = self.resources.enter_context(tempfile.TemporaryDirectory())
        return self.factory(Path(directory), platform, *args)

    def test_missing_or_foreign_live_receipt_blocks_install_and_capture(self):
        for scenario, factory in RUNNER_CASES:
            with self.scenario(scenario=scenario):
                self.factory = factory
                for fault in ("missing", "foreign"):
                    with self.scenario(fault=fault):
                        h = self.make()
                        publish = h._publish_receipt

                        def receipt(fault=fault, publish=publish, h=h):
                            if fault == "foreign":
                                publish()
                                h.corrupt_receipt()

                        h._publish_receipt = receipt
                        with self.assertRaises(RuntimeError):
                            h.run()
                        self.assertNotIn("install", h.events)
                        h.capture.assert_not_called()
                        self.assertFalse(h.finish_arguments["completed"])

    def test_primary_cleanup_errors_distinct_and_foreign_owner_is_not_stopped(self):
        for scenario, factory in RUNNER_CASES:
            with self.scenario(scenario=scenario):
                self.factory = factory
                for boundary in ("install", "capture", "checker"):
                    with self.scenario(boundary=boundary):
                        h = self.make()
                        setattr(h.faults, boundary, RuntimeError("primary " + boundary))
                        h.faults.stop = RuntimeError("cleanup stop")
                        with self.assertRaisesRegex(
                            RuntimeError, "primary " + boundary
                        ) as raised:
                            h.run()
                        self.assertFalse(h.finish_arguments["completed"])
                        self.assertIn(
                            "cleanup stop",
                            str(getattr(raised.exception, "__notes__", [])),
                        )
                        self.assertEqual(
                            h.handlers.call_args_list[-1].args,
                            (signal.SIGTERM, h.previous_handler),
                        )
                h = self.make()

                def replace(*args, h=h, **kwargs):
                    h.replace_owner()
                    raise RuntimeError("capture failed")

                h.capture.side_effect = replace
                with self.assertRaisesRegex(RuntimeError, "capture failed"):
                    h.run()
                self.assertNotIn("fixture-stop", h.events)
                self.assertTrue(h.finish_arguments["cleanup_errors"])

    def test_identity_ack_and_physical_failures_do_not_advance_or_retry(self):
        for scenario, factory in CAPTURE_CASES:
            with self.scenario(scenario=scenario):
                self.factory = factory
                for platform, fault in (
                    ("android", "token"),
                    ("ios", "boot"),
                    ("android", "wrong-flow"),
                    ("ios", "js-first"),
                    ("android", "duplicate"),
                    ("android", "early"),
                    ("android", "pid"),
                    ("ios", "pid"),
                    ("android", "lease"),
                ):
                    with self.scenario(platform=platform, fault=fault):
                        h = self.make(platform, fault)
                        with self.assertRaises((ValueError, RuntimeError, OSError)):
                            h.run()
                        self.assertLessEqual(len(h.controls), 1)
                        self.assertEqual(
                            read_document(h.log.with_suffix(".capture.json"))["status"],
                            "failed",
                        )
                        self.assertTrue(h.log.with_suffix(".raw.log").exists())
                        self.assertTrue(
                            all(c.returncode is not None for c in h.children)
                        )
                        self.assertFalse(h.thread.is_alive())
                        h.ledger.verify.assert_not_called()

    def test_ledger_failure_is_not_app_pass_and_final_publication_failure_downgrades(
        self,
    ):
        for scenario, factory in CAPTURE_CASES:
            with self.scenario(scenario=scenario):
                self.factory = factory
                h = self.make()
                h.ledger.verify.side_effect = ValueError(
                    "independent CCC ledger failed"
                )
                with self.assertRaisesRegex(
                    ValueError, "independent CCC ledger failed"
                ):
                    h.run()
                self.assertEqual(
                    read_document(h.log.with_suffix(".capture.json"))["status"],
                    "failed",
                )
                h = self.make()
                original = reject_capture.write_document
                failed = False

                def publish(path, value, h=h, original=original):
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
                    read_document(h.log.with_suffix(".identity.json"))["status"],
                    "failed",
                )
                self.assertEqual(
                    read_document(h.log.with_suffix(".capture.json"))["status"],
                    "failed",
                )
                self.assertTrue(all(c.returncode is not None for c in h.children))

    def test_frozen_control_request_and_live_receipt_drift_fail_before_app_launch(self):
        for scenario, factory in CAPTURE_CASES:
            with self.scenario(scenario=scenario):
                self.factory = factory
                for name in ("control", "request", "receipt"):
                    with self.scenario(name=name):
                        h = self.make()
                        capture = h.make_capture()
                        path = {
                            "control": h.root / h.protocol.CONTROL_NAME,
                            "request": h.phone.plan.request_path,
                            "receipt": h.phone.plan.receipt_path,
                        }[name]
                        path.chmod(0o600)
                        path.write_bytes(path.read_bytes() + b"\n")
                        if name == "receipt":
                            value = read_document(path)
                            value["fixture_pid"] += 1
                            path.write_text(json.dumps(value))
                        with self.assertRaises((ValueError, RuntimeError)):
                            capture.run()
                        self.assertEqual(h.children, [])

    def test_public_entry_requires_fresh_paths_canonical_identity_and_bounded_timeout(
        self,
    ):
        for scenario, factory in CAPTURE_CASES:
            with self.scenario(scenario=scenario):
                self.factory = factory
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
                    with self.scenario(timeout=timeout), self.assertRaises(ValueError):
                        h.api.capture(
                            *args,
                            timeout=timeout,
                            guard=h.guard,
                            fixture_identity=(os.getpid(), os.getpgid(0)),
                        )
                for index, value in (
                    (1, " device "),
                    (4, "ABCDEFAB-1234-4234-8234-123456789ABC"),
                    (5, "not-a-uuid"),
                ):
                    altered = list(args)
                    altered[index] = value
                    with self.scenario(index=index), self.assertRaises(ValueError):
                        h.api.capture(
                            *altered,
                            timeout=180,
                            guard=h.guard,
                            fixture_identity=(os.getpid(), os.getpgid(0)),
                        )
                h.log.write_text("old evidence")
                with self.assertRaisesRegex(ValueError, "stale"):
                    h.api.capture(
                        *args,
                        timeout=180,
                        guard=h.guard,
                        fixture_identity=(os.getpid(), os.getpgid(0)),
                    )
                self.assertEqual(h.children, [])
