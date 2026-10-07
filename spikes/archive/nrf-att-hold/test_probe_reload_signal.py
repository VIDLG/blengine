"""Hardware-free event ledger for the host-controlled native reload probe."""

from __future__ import annotations

import unittest

from probe_reload_signal import MARKERS, PREFIX, verify_markers


class ReloadSignalLedgerTests(unittest.TestCase):
    def test_two_engines_and_one_host_signal_must_be_ordered(self) -> None:
        lines = [PREFIX + marker for marker in MARKERS]
        verify_markers("\n".join(lines))
        for invalid in (
            lines[:-1],
            lines + [PREFIX + "reload:notification-received"],
            lines[:2] + [PREFIX + "reload:notification-received"] + lines[2:],
            lines[:3] + [PREFIX + "reload:native-triggered"] + lines[3:],
            lines[:3] + [PREFIX + "reload:notification-before-ready"] + lines[3:],
            lines[:3] + [PREFIX + "reload:failed:unexpected"] + lines[3:],
            lines[:4] + [PREFIX + "reload:ready"] + lines[4:],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                verify_markers("\n".join(invalid))


if __name__ == "__main__":
    unittest.main()
