"""Hardware-free tests for the two-generation pending-read ledger checker."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.peripherals.archive.nrf_att_read_hold import pending_read_ledger as ledger

RUN_ID = "12345678-1234-1234-1234-123456789abc"
PROBE_ID = "abcdefab-cdef-abcd-efab-cdefabcdefab"
OTHER_RUN_ID = "87654321-4321-4321-4321-cba987654321"
OTHER_PROBE_ID = "01234567-89ab-cdef-0123-456789abcdef"
PROJECT_ROOT = Path(__file__).resolve().parents[4]
REAL_LOG = (
    PROJECT_ROOT
    / ".work/nrf-att-hold-dk/rn-pending-read-reload-d8792205409b406cb2bca82fa94bc917.attempt.log"
)


def native_marker(
    event: str,
    *,
    run_id: str = RUN_ID,
    probe_id: str = PROBE_ID,
    characteristic: str = "13",
    command: str = "12008",
    connection: str = "12004",
    gatt: str = "12006",
) -> str:
    return (
        f"native:{event}:runId={run_id}:probeId={probe_id}:"
        f"commandId={command}:connectionId={connection}:gattSessionId={gatt}:"
        f"characteristicId={characteristic}"
    )


def evidence() -> str:
    return "\n".join(
        ledger.PREFIX + ledger.MODE + ":" + marker
        for marker in (
            "generation=1:scan:passed:serviceUuid="
            + ledger.SERVICE
            + ":peripheralId=0:scanSessionId=12001",
            (
                "generation=1:connected:passed:commandId=12005:connectionId=12004:"
                "peripheralId=0"
            ),
            (
                "generation=1:gatt:passed:commandId=12007:connectionId=12004:"
                "gattSessionId=12006"
            ),
            native_marker("read-accepted"),
            (
                "generation=1:read-dispatched:commandId=12008:connectionId=12004:"
                "gattSessionId=12006:characteristicId=13:peripheralId=0"
            ),
            "external-ready",
            "notification-received",
            "generation=1:cleanup-started",
            "generation=2:listener:registered-before-initialize",
            "generation=2:scan:passed:serviceUuid="
            + ledger.SERVICE
            + ":peripheralId=1:scanSessionId=13001",
            (
                "generation=2:connected:passed:commandId=13005:connectionId=13004:"
                "peripheralId=1"
            ),
            (
                "generation=2:gatt:passed:commandId=13007:connectionId=13004:"
                "gattSessionId=13006"
            ),
            native_marker("dispose-returned"),
            native_marker("late-submit-rejected"),
            (
                "generation=2:awaiting-arm:commandId=13008:connectionId=13004:"
                "gattSessionId=13006:characteristicId=17"
            ),
            "arm-ready",
            "arm-notification-received",
            (
                "generation=2:read-dispatched:commandId=13008:connectionId=13004:"
                "gattSessionId=13006:characteristicId=17"
            ),
            (
                "generation=2:read:passed:commandId=13008:connectionId=13004:"
                "gattSessionId=13006:characteristicId=17:value=00"
            ),
            "complete:passed",
        )
    )


def swap(text: str, first: str, second: str) -> str:
    lines = text.splitlines()
    first_index = next(
        index for index, line in enumerate(lines) if line.endswith(first)
    )
    second_index = next(
        index for index, line in enumerate(lines) if line.endswith(second)
    )
    lines[first_index], lines[second_index] = lines[second_index], lines[first_index]
    return "\n".join(lines)


class PendingReadLedgerTests(unittest.TestCase):
    def test_accepts_ordered_two_generation_ledger_and_exports_boundaries(self) -> None:
        text = evidence()
        self.assertEqual(
            ledger.markers(text)[0].split(":", 2)[0:2],
            [ledger.MODE, "generation=1"],
        )
        boundaries = ledger.native_boundaries(
            ledger.markers(text),
            run_id=RUN_ID,
            probe_id=PROBE_ID,
            complete=True,
        )
        self.assertEqual(
            set(boundaries),
            {"read-accepted", "dispose-returned", "late-submit-rejected"},
        )
        self.assertEqual(
            ledger.verify_ledger(
                text,
                run_id=RUN_ID,
                probe_id=PROBE_ID,
                post_steps=("reload", "arm"),
                old_characteristic="13",
            ),
            ("13", "17"),
        )

    def test_accepts_canonical_u64_peripheral_boundaries(self) -> None:
        maximum = "18446744073709551615"
        text = evidence().replace("peripheralId=0", f"peripheralId={maximum}")
        self.assertEqual(
            ledger.verify_ledger(
                text,
                run_id=RUN_ID,
                post_steps=("reload", "arm"),
            ),
            ("13", "17"),
        )

    def test_rejects_missing_duplicate_and_wrong_post_boundaries(self) -> None:
        text = evidence()
        terminal = ledger.PREFIX + ledger.MODE + ":complete:passed"
        invalid = (
            text.replace(terminal, "", 1),
            text + "\n" + terminal,
        )
        post_steps = (("reload",), ("arm", "reload"), ("reload", "arm", "arm"))
        for candidate in invalid:
            with (
                self.subTest(candidate=candidate[-40:]),
                self.assertRaises(RuntimeError),
            ):
                ledger.verify_ledger(
                    candidate,
                    run_id=RUN_ID,
                    post_steps=("reload", "arm"),
                )
        for steps in post_steps:
            with (
                self.subTest(steps=steps),
                self.assertRaisesRegex(RuntimeError, "two ordered"),
            ):
                ledger.verify_ledger(text, run_id=RUN_ID, post_steps=steps)

    def test_rejects_order_and_generation_identity_leaks(self) -> None:
        text = evidence()
        old_dispatch = (
            "generation=1:read-dispatched:commandId=12008:connectionId=12004:"
            "gattSessionId=12006:characteristicId=13:peripheralId=0"
        )
        invalid = (
            swap(
                text,
                "generation=1:cleanup-started",
                "generation=2:listener:registered-before-initialize",
            ),
            swap(text, "arm-ready", "arm-notification-received"),
            text
            + "\n"
            + ledger.PREFIX
            + ledger.MODE
            + ":generation=2:late:event:commandId=12008",
            text.replace(
                "generation=2:read:passed:commandId=13008",
                "generation=2:read:passed:commandId=12008",
            ),
            text.replace(
                old_dispatch,
                old_dispatch.replace("commandId=12008", "commandId=999"),
            ),
            text.replace(ledger.SERVICE, "fd000000-0000-4000-8000-000000000001"),
            text.replace("peripheralId=0", "peripheralId=00"),
        )
        for candidate in invalid:
            with (
                self.subTest(candidate=candidate[-100:]),
                self.assertRaises(RuntimeError),
            ):
                ledger.verify_ledger(
                    candidate,
                    run_id=RUN_ID,
                    post_steps=("reload", "arm"),
                )

    def test_rejects_native_run_probe_read_identity_variants(self) -> None:
        text = evidence()
        native_read = ledger.PREFIX + ledger.MODE + ":" + native_marker("read-accepted")
        variants = (
            text.replace(native_read, native_read.replace(RUN_ID, OTHER_RUN_ID), 1),
            text.replace(native_read, native_read.replace(PROBE_ID, OTHER_PROBE_ID), 1),
            text.replace(
                native_read,
                native_read.replace("commandId=12008", "commandId=12009"),
                1,
            ),
            text.replace(
                native_read,
                native_read.replace("connectionId=12004", "connectionId=12005"),
                1,
            ),
            text.replace(
                native_read,
                native_read.replace("gattSessionId=12006", "gattSessionId=12007"),
                1,
            ),
            text.replace(
                native_read,
                native_read.replace("characteristicId=13", "characteristicId=14"),
                1,
            ),
            text.replace(native_read + "\n", "", 1),
            text + "\n" + native_read,
            text
            + "\n"
            + ledger.PREFIX
            + ledger.MODE
            + ":native:unknown:runId="
            + RUN_ID,
            text
            + "\n"
            + ledger.PREFIX
            + ledger.MODE
            + ":failed:native-boundary-dispose-failed",
        )
        for candidate in variants:
            with (
                self.subTest(candidate=candidate[-140:]),
                self.assertRaises(RuntimeError),
            ):
                ledger.verify_ledger(
                    candidate,
                    run_id=RUN_ID,
                    post_steps=("reload", "arm"),
                )

    def test_rejects_characteristic_changes_old_terminal_and_failures(self) -> None:
        text = evidence()
        variants = (
            text.replace(
                "characteristicId=13:peripheralId=0",
                "characteristicId=14:peripheralId=0",
            ),
            text.replace("characteristicId=17\n", "characteristicId=18\n"),
            text.replace(
                (
                    "generation=2:read:passed:commandId=13008:connectionId=13004:"
                    "gattSessionId=13006:characteristicId=17:value=00"
                ),
                "generation=2:read:passed:commandId=13008:connectionId=13004:"
                "gattSessionId=13006:characteristicId=17:value=01",
            ),
            text
            + "\n"
            + ledger.PREFIX
            + ledger.MODE
            + ":generation=1:read:passed:commandId=12008",
            text + "\n" + ledger.PREFIX + ledger.MODE + ":generation=1:failed:dispatch",
            text
            + "\n"
            + ledger.PREFIX
            + ledger.MODE
            + ":generation=2:close-rejected:late",
        )
        for candidate in variants:
            with (
                self.subTest(candidate=candidate[-120:]),
                self.assertRaises(RuntimeError),
            ):
                ledger.verify_ledger(
                    candidate,
                    run_id=RUN_ID,
                    post_steps=("reload", "arm"),
                    old_characteristic="13",
                )

    @unittest.skipUnless(REAL_LOG.is_file(), "saved nRF pending-read log is absent")
    def test_saved_real_log_accepts_known_markers_but_rejects_new_ones(self) -> None:
        text = REAL_LOG.read_text(encoding="utf-8")
        self.assertEqual(
            ledger.verify_ledger(
                text,
                run_id="d8792205-409b-406c-b2bc-a82fa94bc917",
                post_steps=("reload", "arm"),
                old_characteristic="13",
            ),
            ("13", "13"),
        )
        prefix = ledger.PREFIX + ledger.MODE + ":"
        invalid = (
            text + "\n" + prefix + "unknown:diagnostic-marker",
            text + "\n" + prefix + "generation=2:failed:read",
            text
            + "\n"
            + prefix
            + "generation=2:arm-js-observed:source=linking:url=matched",
            text.replace(
                "generation=2:read-dispatched:commandId=13008",
                "generation=2:read-dispatched:commandId=12008",
                1,
            ),
        )
        for candidate in invalid:
            with (
                self.subTest(candidate=candidate[-120:]),
                self.assertRaises(RuntimeError),
            ):
                ledger.verify_ledger(
                    candidate,
                    run_id="d8792205-409b-406c-b2bc-a82fa94bc917",
                    post_steps=("reload", "arm"),
                    old_characteristic="13",
                )

    def test_native_boundaries_can_be_checked_before_complete_ledger(self) -> None:
        lines = ledger.markers(evidence())
        prefix = ledger.MODE + ":"
        partial = lines[: lines.index(prefix + "external-ready") + 1]
        boundaries = ledger.native_boundaries(
            partial,
            run_id=RUN_ID,
            probe_id=PROBE_ID,
            complete=False,
        )
        self.assertEqual(set(boundaries), {"read-accepted"})
        with self.assertRaises(RuntimeError):
            ledger.native_boundaries(
                partial,
                run_id=RUN_ID,
                probe_id=OTHER_PROBE_ID,
                complete=False,
            )


if __name__ == "__main__":
    unittest.main()
