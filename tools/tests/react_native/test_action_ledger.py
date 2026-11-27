"""Unit contract for the pinned action-ledger projection.

The encoder is the App's (`src/actionLedger.ts`), and a scene asserts the exact ordered
actions it caused. These tests hold the decoder to the same strictness: a frame that is
merely *close* must not be accepted, because the failure it would hide is "the App
recorded a different sequence than the one I drove".
"""

from __future__ import annotations

import unittest

from tools.react_native import action_ledger as ledger


class ActionLedgerTests(unittest.TestCase):
    def test_an_empty_ledger_is_the_header_alone(self) -> None:
        self.assertEqual(ledger.parse("omitted=0"), (0, []))
        self.assertEqual(ledger.actions("omitted=0"), [])

    def test_it_reports_the_dropped_count_and_every_entry_in_order(self) -> None:
        text = (
            "omitted=8\n41|close|succeeded|\n42|notify-pull|succeeded|ended: disabled"
        )
        self.assertEqual(
            ledger.parse(text),
            (
                8,
                [
                    (41, "close", "succeeded", ""),
                    (42, "notify-pull", "succeeded", "ended: disabled"),
                ],
            ),
        )

    def test_a_foreign_or_truncated_line_is_refused(self) -> None:
        for text, message in (
            ("omitted=0\n1|start|succeeded", "invalid action-ledger line"),
            ("omitted=0\n1|start|succeeded|extra|field", "invalid action-ledger line"),
            ("1|start|succeeded|", "invalid action-ledger header"),
            ("omitted=x", "invalid action-ledger header"),
            ("omitted=0\n\n", "empty action-ledger line"),
            ("omitted=0\n1|Start|succeeded|", "action/result"),
            ("omitted=0\n0|start|succeeded|", "not increasing"),
            ("omitted=0\n2|start|succeeded|\n2|close|succeeded|", "not increasing"),
            ("omitted=0\n1|start|succeeded|" + "x" * 600, "unbounded"),
        ):
            with (
                self.subTest(text=text[:40]),
                self.assertRaisesRegex(ledger.ActionLedgerError, message),
            ):
                ledger.parse(text)

    def test_it_refuses_more_entries_than_the_app_can_ever_publish(self) -> None:
        text = "omitted=0\n" + "\n".join(
            f"{index}|start|succeeded|" for index in range(1, 70)
        )
        with self.assertRaisesRegex(ledger.ActionLedgerError, "exceeds its bound"):
            ledger.parse(text)

    def test_actions_drops_the_detail_a_scene_does_not_assert(self) -> None:
        text = "omitted=0\n1|start|succeeded|\n2|read|failed|bad"
        self.assertEqual(
            ledger.actions(text), [("start", "succeeded"), ("read", "failed")]
        )


if __name__ == "__main__":
    unittest.main()
