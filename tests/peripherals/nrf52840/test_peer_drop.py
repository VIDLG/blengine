"""Synthetic peer-drop tests for the passive-disconnect scene; never access USB."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from .support.fault_emulator import PEER
from .support.hold_fixture import ROLE, bind


class PeerDropTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)

    def make(self, *, connected: bool = True):
        root = Path(tempfile.mkdtemp(dir=self._temporary.name))
        return bind(root, connected=connected)

    def test_a_drop_ends_the_link_the_controller_reported(self) -> None:
        case = self.make()
        peer = case.fixture.drop_peer(deadline=time.monotonic() + 1)
        # The address comes from the fault engine's live peer, not from a cached value.
        self.assertEqual(peer, PEER)
        self.assertEqual(len(case.wire.drops), 1)
        self.assertEqual(case.wire.drops[0], PEER)
        # The controller's own bookkeeping now says no peer is present.
        self.assertFalse(case.wire.connected)
        self.assertFalse(
            case.fixture.peer_present(deadline=time.monotonic() + 1)
        )

    def test_a_drop_without_a_peer_never_touches_the_wire(self) -> None:
        case = self.make(connected=False)
        with self.assertRaises(RuntimeError) as raised:
            case.fixture.drop_peer(deadline=time.monotonic() + 1)
        self.assertIn("connected peer", str(raised.exception))
        # Reading the engine is not a mutation: nothing was disconnected.
        self.assertEqual(case.wire.drops, [])
        self.assertEqual(
            [frame for frame in case.wire.commands if frame.service == 1], []
        )

    def test_an_inactive_session_refuses_to_drop(self) -> None:
        for attribute, value in (
            ("_started", False),
            ("_stop_requested", True),
            ("_mutation_allowed", False),
        ):
            with self.subTest(attribute=attribute):
                case = self.make()
                setattr(case.fixture, attribute, value)
                with self.assertRaises(RuntimeError):
                    case.fixture.drop_peer(deadline=time.monotonic() + 1)
                self.assertEqual(case.wire.drops, [])

    def test_the_drop_is_not_reported_before_the_controller_agrees(self) -> None:
        """A drop that the controller never confirms must fail, not be assumed.

        The synthetic firmware confirms on the command, so this states the boundary the
        other way round: `drop_peer` returns only after the follow-up `GET_LIMITS` says the
        peer is gone, and that read is what the test counts.
        """
        case = self.make()
        limits_before = len(
            [f for f in case.wire.commands if f.opcode == 0x10]
        )
        case.fixture.drop_peer(deadline=time.monotonic() + 1)
        limits_after = len([f for f in case.wire.commands if f.opcode == 0x10])
        self.assertGreater(limits_after, limits_before)


if __name__ == "__main__":
    unittest.main()
