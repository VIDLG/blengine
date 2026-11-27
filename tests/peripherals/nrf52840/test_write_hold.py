"""Synthetic write-hold tests: ARM_WRITE, REJECT and COMMIT; never access USB.

The write lane's discriminator is that a rejection and a commit are told apart by the
*peer's receipt*, not by what the central saw: both answer the central's request, and only
one of them changed anything.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from . import fault_control as fault
from .support.fault_emulator import PEER
from .support.hold_fixture import ROLE, VALUE_HANDLE, bind

REQUEST = b"\x11"


class WriteHoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)

    def make(self, *, connected: bool = True):
        root = Path(tempfile.mkdtemp(dir=self._temporary.name))
        return bind(root, connected=connected)

    def arm(self):
        case = self.make()
        return case, case.fixture.arm_write_hold(ROLE, REQUEST)

    def held(self):
        case, hold = self.arm()
        case.wire.capture_write(REQUEST)
        case.fixture.require_hold_held(hold, deadline=time.monotonic() + 0.5)
        return case, hold

    def test_arm_binds_the_exact_request_and_the_current_value(self) -> None:
        case, hold = self.arm()
        self.assertEqual(hold.operation, fault.Operation.RESPONSE_WRITE)
        self.assertEqual(hold.handle, VALUE_HANDLE)
        self.assertEqual(hold.before, b"\x00")
        self.assertEqual(hold.requested, REQUEST)
        self.assertEqual(hold.peer, PEER)
        self.assertEqual(case.wire.phase, fault.Phase.ARMED)
        arm = [
            frame
            for frame in case.wire.commands
            if frame.opcode == fault.Opcode.ARM_WRITE
        ]
        self.assertEqual(len(arm), 1)
        self.assertEqual(case.wire.requested, REQUEST)
        self.assertEqual(case.inventory.value_reads, [VALUE_HANDLE])

    def test_a_hold_is_never_reported_before_the_write_arrives(self) -> None:
        case, hold = self.arm()
        with self.assertRaises(TimeoutError):
            case.fixture.require_hold_held(hold, deadline=time.monotonic() + 0.01)
        case.wire.capture_write(REQUEST)
        case.fixture.require_hold_held(hold, deadline=time.monotonic() + 0.5)
        self.assertEqual(case.wire.phase, fault.Phase.HELD)

    def test_a_write_that_is_not_the_armed_request_is_not_captured(self) -> None:
        case, _ = self.arm()
        # The firmware only captures the exact request; a different write keeps its normal
        # path, so a scene cannot claim to be holding whatever the App happened to send.
        with self.assertRaises(AssertionError):
            case.wire.capture_write(b"\x22")
        self.assertEqual(case.wire.phase, fault.Phase.ARMED)
        self.assertFalse(case.wire.captured)

    def test_reject_retires_without_a_callback_or_a_commit(self) -> None:
        case, hold = self.held()
        status = case.fixture.release_hold(
            hold, fault.Decision.REJECT, deadline=time.monotonic() + 0.5
        )
        self.assertTrue(fault.injected_reject(status))
        self.assertFalse(fault.mutation_committed(status, REQUEST))
        self.assertEqual(status.before, status.after)
        self.assertEqual(status.observed, REQUEST)
        # The peer stored nothing: the request reached it and was refused.
        self.assertEqual(case.wire.commits, [])
        self.assertEqual(case.wire.remote_value, b"\x00")

    def test_commit_stores_exactly_the_request(self) -> None:
        case, hold = self.held()
        status = case.fixture.release_hold(
            hold, fault.Decision.COMMIT, deadline=time.monotonic() + 0.5
        )
        self.assertTrue(fault.mutation_committed(status, REQUEST))
        self.assertFalse(fault.injected_reject(status))
        self.assertEqual(status.observed, REQUEST)
        self.assertEqual(status.after, REQUEST)
        self.assertEqual(case.wire.commits, [(VALUE_HANDLE, REQUEST)])
        self.assertEqual(case.wire.remote_value, REQUEST)

    def test_an_empty_request_is_refused_before_the_wire(self) -> None:
        case = self.make()
        with self.assertRaises(ValueError):
            case.fixture.arm_write_hold(ROLE, b"")
        self.assertEqual(
            [
                frame
                for frame in case.wire.commands
                if frame.opcode == fault.Opcode.ARM_WRITE
            ],
            [],
        )

    def test_an_unknown_role_never_touches_the_wire(self) -> None:
        case = self.make()
        before = list(case.wire.commands)
        with self.assertRaises(ValueError) as raised:
            case.fixture.arm_write_hold("absent", REQUEST)
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
                    case.fixture.arm_write_hold(ROLE, REQUEST)
                self.assertEqual(case.wire.commands, before)

    def test_arm_requires_a_connected_peer(self) -> None:
        case = self.make(connected=False)
        with self.assertRaises(RuntimeError) as raised:
            case.fixture.arm_write_hold(ROLE, REQUEST)
        self.assertIn("connected peer", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
