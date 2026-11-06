"""Original production-written reports replayed with adversarial raw/fact edits."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.peripherals.nrf52840 import write_reject as api
from tests.peripherals.nrf52840.cancellation_control import (
    read_document,
    write_document,
)
from tests.peripherals.nrf52840.fault_control import (
    status_document,
    status_from_document,
)
from tests.peripherals.nrf52840.support.write_reject_emulator import Harness
from tools.react_native import write_reject_ledger as ledger


class WriteRejectLedgerTests(unittest.TestCase):
    def make(self, platform="android"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        h = Harness(Path(temporary.name), platform)
        h.run()
        return h

    def verify(self, h, text=None):
        return ledger.verify(
            h.text if text is None else text,
            platform=h.platform,
            run_id=h.plan.session_id,
            service_uuid=h.plan.service_uuid,
            physical_path=h.root / api.PHYSICAL_NAME,
            fixture_request=h.plan.request_path,
            fixture_identity=(os.getpid(), os.getpgid(0)),
        )

    def test_both_platforms_replay_production_writer_report_and_all_native_facts(self):
        for platform in ("android", "ios"):
            with self.subTest(platform=platform):
                h = self.make(platform)
                result = self.verify(h)
                self.assertEqual(result["outcome"], "INJECTED_REJECT")
                self.assertEqual(
                    result["closed_resources"], dict.fromkeys(api.RESOURCE_FIELDS, 0)
                )
                self.assertEqual(result["rejected_command_id"], "30010")

    def test_optional_disconnecting_preserves_exact_disconnect_completion(self):
        for platform in ("android", "ios"):
            with self.subTest(platform=platform):
                h = self.make(platform)
                document = read_document(h.root / api.PHYSICAL_NAME)
                disconnected = next(
                    line
                    for line in h.text.splitlines()
                    if '"connectionState": "Disconnected"' in line
                )
                disconnecting = disconnected.replace(
                    '"Disconnected"', '"Disconnecting"'
                )
                for suffix, valid in (
                    (disconnected, True),
                    (disconnecting + "\n" + disconnected, True),
                    (disconnecting + "\n" + disconnecting + "\n" + disconnected, False),
                    (disconnected + "\n" + disconnecting, False),
                    (disconnecting, False),
                    (disconnected.replace('"Disconnected"', '"Connecting"'), False),
                ):
                    text = h.text.replace(disconnected, suffix)
                    document["boundaries"][-1]["runtime_sha256"] = hashlib.sha256(
                        text.encode()
                    ).hexdigest()
                    write_document(h.root / api.PHYSICAL_NAME, document)
                    if valid:
                        self.verify(h, text)
                    else:
                        with self.assertRaises(ValueError):
                            self.verify(h, text)

    def test_rehashed_wrong_raw_error_routes_values_domains_and_duplicate_terminal_fail(
        self,
    ):
        h = self.make()
        original = read_document(h.root / api.PHYSICAL_NAME)
        texts = [
            h.text.replace('"code": "14"', '"code": "133"'),
            h.text.replace('"category": "gatt"', '"category": "timeout"'),
            h.text.replace('"characteristicId": "31"', '"characteristicId": "32"'),
            h.text.replace('"value": [0]', '"value": [17]', 1),
            h.text.replace(
                '"kind": "characteristicWritten"', '"kind": "characteristicRead"'
            ),
            h.text.replace(
                '"nonce": "synthetic-write-boot"', '"nonce": "restarted-boot"', 1
            ),
        ]
        ready = api.PREFIX + h.control.marker("ready", "arm") + "\n"
        texts.append(
            h.text.replace(ready, "").replace(
                api.PREFIX + "rf-write-reject:step:baseline-read:",
                ready + api.PREFIX + "rf-write-reject:step:baseline-read:",
            )
        )
        terminal = next(
            line for line in h.text.splitlines() if '"commandId": "30010"' in line
        )
        texts.append(h.text.replace(terminal, terminal + "\n" + terminal))
        for text in texts:
            with self.subTest(text=text[-80:]):
                document = json.loads(json.dumps(original))
                document["boundaries"][-1]["runtime_sha256"] = hashlib.sha256(
                    text.encode()
                ).hexdigest()
                write_document(h.root / api.PHYSICAL_NAME, document)
                with self.assertRaises(ValueError):
                    self.verify(h, text)

    def test_wrong_physical_local_retained_gap_deadline_and_zero_fail(self):
        for fault in (
            "local",
            "retained",
            "gap",
            "deadline",
            "resources",
            "decision",
            "foreign-process",
            "signal-order",
        ):
            with self.subTest(fault=fault):
                h = self.make()
                report = read_document(h.root / api.PHYSICAL_NAME)
                rows = report["boundaries"]
                by_name = {r["boundary"]: r for r in rows}
                if fault == "local":
                    by_name["rejected"]["local"]["att_response"] = 14
                elif fault == "retained":
                    row = by_name["recovery"]
                    row["status"] = status_document(
                        replace(status_from_document(row["status"]), after=b"\x22")
                    )
                elif fault == "gap":
                    next(r for r in rows if r["boundary"] == "gap")["peer"] = (
                        "01665544332212"
                    )
                elif fault == "deadline":
                    by_name["resolve-dispatched"]["retirement_deadline"] = by_name[
                        "rejected"
                    ]["monotonic"]
                elif fault == "resources":
                    by_name["closed"]["resources"]["lifecycleOwners"] = 1
                elif fault == "decision":
                    by_name["resolve-dispatched"]["decision"] = 1
                elif fault == "foreign-process":
                    report["fixture_pid"] += 1
                else:
                    rows[2], rows[3] = rows[3], rows[2]
                write_document(h.root / api.PHYSICAL_NAME, report)
                with self.assertRaises(ValueError):
                    self.verify(h)
