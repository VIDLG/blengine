"""Synthetic read-hold tests for the managed cancellation scene; never access USB."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from . import direct_fixture
from . import fault_control as fault
from .support.fault_emulator import PEER
from .support.hold_fixture import ROLE, VALUE_HANDLE, bind


class ReadHoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)

    def make(self, *, connected: bool = True):
        root = Path(tempfile.mkdtemp(dir=self._temporary.name))
        return bind(root, connected=connected)

    def test_arm_binds_the_live_peer_and_the_current_value(self) -> None:
        case = self.make()
        hold = case.fixture.arm_read_hold(ROLE)
        self.assertEqual(hold.operation, fault.Operation.READ)
        self.assertEqual(hold.handle, VALUE_HANDLE)
        self.assertEqual(hold.before, b"\x00")
        self.assertEqual(hold.requested, b"")
        self.assertEqual(hold.peer, PEER)
        self.assertEqual(case.wire.phase, fault.Phase.ARMED)
        arm = [
            frame
            for frame in case.wire.commands
            if frame.opcode == fault.Opcode.ARM_READ
        ]
        self.assertEqual(len(arm), 1)
        self.assertEqual(case.inventory.value_reads, [VALUE_HANDLE])

    def test_a_hold_is_never_reported_before_the_read_arrives(self) -> None:
        case = self.make()
        hold = case.fixture.arm_read_hold(ROLE)
        # Nothing has been read yet: the engine is ARMED, so silence is not a hold.
        with self.assertRaises(TimeoutError):
            case.fixture.require_hold_held(hold, deadline=time.monotonic() + 0.01)
        case.wire.capture_read()
        case.fixture.require_hold_held(hold, deadline=time.monotonic() + 0.5)
        self.assertEqual(case.wire.phase, fault.Phase.HELD)

    def test_a_ticket_that_leaves_armed_is_reported_not_polled(self) -> None:
        case = self.make()
        hold = case.fixture.arm_read_hold(ROLE)
        case.wire.gap(direct_fixture.GAP_DEVICE_DISCONNECTED)
        with self.assertRaises(RuntimeError) as raised:
            case.fixture.require_hold_held(hold, deadline=time.monotonic() + 0.5)
        self.assertIn("left ARMED", str(raised.exception))

    def test_release_commits_and_waits_for_the_normal_completion(self) -> None:
        case = self.make()
        hold = case.fixture.arm_read_hold(ROLE)
        case.wire.capture_read()
        case.fixture.require_hold_held(hold, deadline=time.monotonic() + 0.5)
        status = case.fixture.release_hold(
            hold, fault.Decision.COMMIT, deadline=time.monotonic() + 0.5
        )
        self.assertEqual(case.wire.phase, fault.Phase.RETIRED)
        self.assertTrue(fault.read_completed(status))
        # The peer applied exactly one remote read; the hold is not a local read callback.
        self.assertEqual(len(case.wire.remote_reads), 1)
        self.assertEqual(case.wire.remote_reads[0][1:], (VALUE_HANDLE, b"\x00"))

    def test_a_released_engine_is_rearmable_for_the_next_hold(self) -> None:
        case = self.make()
        hold = case.fixture.arm_read_hold(ROLE)
        case.wire.capture_read()
        case.fixture.require_hold_held(hold, deadline=time.monotonic() + 0.5)
        case.fixture.release_hold(
            hold, fault.Decision.COMMIT, deadline=time.monotonic() + 0.5
        )
        again = case.fixture.arm_read_hold(ROLE)
        self.assertNotEqual(again.ticket.sequence, hold.ticket.sequence)
        self.assertEqual(case.wire.phase, fault.Phase.ARMED)

    def test_arm_requires_a_connected_peer(self) -> None:
        case = self.make(connected=False)
        with self.assertRaises(RuntimeError) as raised:
            case.fixture.arm_read_hold(ROLE)
        self.assertIn("connected peer", str(raised.exception))
        self.assertEqual(
            [
                frame
                for frame in case.wire.commands
                if frame.opcode == fault.Opcode.ARM_READ
            ],
            [],
        )

    def test_an_unknown_role_never_touches_the_wire(self) -> None:
        case = self.make()
        before = list(case.wire.commands)
        with self.assertRaises(ValueError) as raised:
            case.fixture.arm_read_hold("absent")
        self.assertIn("no 'absent' characteristic handle", str(raised.exception))
        self.assertEqual(case.wire.commands, before)

    def test_an_inactive_fixture_session_refuses_to_arm(self) -> None:
        for attribute, value in (
            ("_started", False),
            ("_stop_requested", True),
            ("_mutation_allowed", False),
        ):
            with self.subTest(attribute=attribute):
                case = self.make()
                setattr(case.fixture, attribute, value)
                before = list(case.wire.commands)
                with self.assertRaises(RuntimeError):
                    case.fixture.arm_read_hold(ROLE)
                self.assertEqual(case.wire.commands, before)


if __name__ == "__main__":
    unittest.main()
