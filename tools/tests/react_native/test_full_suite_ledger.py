"""Synthetic semantic audits using the real base, watchdog and recovery checkers."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tests.peripherals.nrf52840 import full_suite as protocol
from tests.peripherals.nrf52840.cancellation_control import (
    read_document,
    write_document,
)
from tests.peripherals.nrf52840.support.full_suite_emulator import FullHarness
from tools.react_native import full_suite_ledger as api
from tools.tests.react_native.support.full_suite_ledger import (
    SuiteFixture,
)


class FullSuiteLedgerTests(unittest.TestCase):
    def make(self, platform: str = "android") -> SuiteFixture:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return SuiteFixture(Path(temporary.name), platform)

    def test_real_checkers_both_platforms_bind_hundred_terminals_and_raw_lineage(
        self,
    ) -> None:
        for platform in ("android", "ios"):
            with self.subTest(platform=platform):
                h = self.make(platform)
                result = h.verify()
                self.assertEqual(result["status"], "passed")
                self.assertEqual(result["counts"]["recovery_terminals"], 100)
                self.assertEqual(result["capture"]["raw_line_numbers"][0], 2)
                self.assertEqual(
                    len(result["capture"]["recovery_raw_line_numbers"]),
                    len(api.recovery_projection(h.text).splitlines()),
                )

    def test_actual_provider_handoff_and_final_schema_without_host_factory(
        self,
    ) -> None:
        for platform in ("ios", "android"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as tmp:
                h = FullHarness(Path(tmp), platform)
                h.run()
                handoff = read_document(h.root / protocol.HANDOFF_NAME)
                final = read_document(h.root / protocol.PHYSICAL_NAME)
                api.check_final_physical(
                    final,
                    handoff,
                    h.root,
                    h.h.plan.session_id,
                    (os.getpid(), os.getpgid(0)),
                )
                self.assertTrue(
                    any(
                        b["boundary"] == "fresh-idle"
                        for b in final["boundaries"][
                            len(handoff["boundaries"]) + 1 : -1
                        ]
                    )
                )

    def test_physical_stage_clocks_do_not_overlap_cancel_watchdog_handoff(self) -> None:
        h = self.make()
        cancellation = read_document(
            h.root / h.controls[0].directory / "physical-evidence.json"
        )
        first_gap = next(
            b for b in h.watchdog.evidence["boundaries"] if b["boundary"] == "gap"
        )
        self.assertLessEqual(
            cancellation["boundaries"][-1]["monotonic"], first_gap["monotonic"]
        )
        last = h.watchdog.evidence["boundaries"][-1]["monotonic"]
        self.assertLessEqual(last, h.handoff["boundaries"][0]["monotonic"])
        h.handoff["boundaries"][0]["monotonic"] = last - 1
        with self.assertRaisesRegex(RuntimeError, "handoff must follow watchdog"):
            api.check_handoff(h.handoff, h.plan.session_id, h.identity, h.root)

    def test_fail_closed_mutations_not_ui_pass(self) -> None:
        for fault in (
            "nonce",
            "control-boot",
            "begin-cycle",
            "begin-expired",
            "physical",
            "handoff",
            "readback",
            "recovery-terminal",
            "base-readback",
            "duplicate",
            "raw-lineage",
        ):
            with self.subTest(fault=fault):
                h = self.make()
                if fault == "nonce":
                    h.text = h.text.replace(
                        "rf-lab-full:runtime:session="
                        + h.plan.session_id
                        + ":nonce=synthetic-one-runtime:phase=close",
                        "rf-lab-full:runtime:session="
                        + h.plan.session_id
                        + ":nonce=restarted:phase=close",
                    )
                elif fault == "control-boot":
                    h.capture["control_bootstrap"]["boot_id"] = h.plan.session_id
                elif fault == "begin-cycle":
                    h.capture["begin"]["native_signal"]["cycle"] = 0
                elif fault == "begin-expired":
                    h.capture["begin"]["js_ack_at"] = 102.0
                elif fault == "physical":
                    h.physical["status"] = "failed"
                    write_document(h.root / protocol.PHYSICAL_NAME, h.physical)
                elif fault in ("handoff", "readback"):
                    if fault == "handoff":
                        h.handoff["peer_count"] = 1
                    else:
                        h.handoff["canonical_values"][0]["readback"] = "ff"
                    write_document(h.root / protocol.HANDOFF_NAME, h.handoff)
                elif fault == "recovery-terminal":
                    h.text = h.text.replace(
                        '"kind": "commandCompleted",', '"kind": "commandFailed",', 1
                    )
                elif fault == "base-readback":
                    h.text = h.text.replace(
                        "rf:step:read-response:passed:",
                        "rf:step:read-response:missing:",
                    )
                elif fault == "duplicate":
                    h.text += h.text.splitlines()[-1] + "\n"
                h.publish()
                if fault == "raw-lineage":
                    h.log.with_suffix(".raw.log").write_text(
                        h.text.replace("ReactNativeJS", "ChangedTag"), encoding="utf-8"
                    )
                with self.assertRaises((RuntimeError, AssertionError, ValueError)):
                    h.verify()


if __name__ == "__main__":
    unittest.main()
