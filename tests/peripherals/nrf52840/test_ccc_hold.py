"""Synthetic CCC-hold tests: ARM_CCC on the managed descriptor; never access USB.

A CCC write is addressed by its descriptor, and enable/disable are the exact u16 the
central writes. These tests hold the fixture to both facts: the handle comes from the
server's own table, and a no-op can never stand in for the operation a scene claims.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from . import fault_control as fault
from .gatt_commands import GattAttribute
from .support.hold_fixture import (
    CCC_HANDLE,
    NEXT_DECLARATION,
    ROLE,
    VALUE_HANDLE,
    bind,
)

ENABLE = b"\x01\x00"


class CccHoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)

    def make(self, *, connected: bool = True):
        root = Path(tempfile.mkdtemp(dir=self._temporary.name))
        return bind(root, connected=connected)

    def arm(self, requested: bytes = ENABLE):
        case = self.make()
        return case, case.fixture.arm_ccc_hold(ROLE, requested)

    def held(self, requested: bytes = ENABLE):
        case, hold = self.arm(requested)
        case.wire.capture_write(requested)
        case.fixture.require_hold_held(hold, deadline=time.monotonic() + 0.5)
        return case, hold

    def test_arm_binds_the_role_descriptor_not_its_value(self) -> None:
        case, hold = self.arm()
        self.assertEqual(hold.operation, fault.Operation.CCC_WRITE)
        self.assertEqual(hold.handle, CCC_HANDLE)
        self.assertEqual(hold.before, bytes(2))
        self.assertEqual(hold.requested, ENABLE)
        self.assertEqual(case.wire.phase, fault.Phase.ARMED)
        arm = [
            frame
            for frame in case.wire.commands
            if frame.opcode == fault.Opcode.ARM_CCC
        ]
        self.assertEqual(len(arm), 1)
        # The descriptor was resolved from the table, and the descriptor is what was read.
        self.assertEqual(case.inventory.inventory_reads, 1)
        self.assertEqual(case.inventory.value_reads, [CCC_HANDLE])
        self.assertNotEqual(hold.handle, VALUE_HANDLE)

    def test_a_no_op_request_is_refused_before_the_wire(self) -> None:
        case = self.make()
        with self.assertRaises(ValueError) as raised:
            case.fixture.arm_ccc_hold(ROLE, bytes(2))
        self.assertIn("no-op", str(raised.exception))
        self.assertEqual(
            [
                frame
                for frame in case.wire.commands
                if frame.opcode == fault.Opcode.ARM_CCC
            ],
            [],
        )

    def test_a_hold_is_never_reported_before_the_ccc_write_arrives(self) -> None:
        case, hold = self.arm()
        with self.assertRaises(TimeoutError):
            case.fixture.require_hold_held(hold, deadline=time.monotonic() + 0.01)
        case.wire.capture_write(ENABLE)
        case.fixture.require_hold_held(hold, deadline=time.monotonic() + 0.5)
        self.assertEqual(case.wire.phase, fault.Phase.HELD)

    def test_reject_leaves_the_descriptor_at_its_previous_value(self) -> None:
        case, hold = self.held()
        status = case.fixture.release_hold(
            hold, fault.Decision.REJECT, deadline=time.monotonic() + 0.5
        )
        self.assertTrue(fault.injected_reject(status))
        self.assertEqual(status.before, status.after)
        self.assertEqual(case.wire.ccc_value, bytes(2))
        self.assertEqual(case.wire.commits, [])

    def test_commit_stores_the_requested_u16(self) -> None:
        case, hold = self.held()
        status = case.fixture.release_hold(
            hold, fault.Decision.COMMIT, deadline=time.monotonic() + 0.5
        )
        self.assertTrue(fault.mutation_committed(status, ENABLE))
        self.assertEqual(status.after, ENABLE)
        self.assertEqual(case.wire.ccc_value, ENABLE)
        self.assertEqual(case.wire.commits, [(CCC_HANDLE, ENABLE)])

    def test_a_disable_is_its_own_binding(self) -> None:
        case, hold = self.held(ENABLE)
        case.fixture.release_hold(
            hold, fault.Decision.COMMIT, deadline=time.monotonic() + 0.5
        )
        # Nothing about the first binding survives: a disable names its own exact request,
        # and its pre-state is the enable the caller proved through the subscription.
        with self.assertRaises(ValueError):
            case.fixture.arm_ccc_hold(ROLE, ENABLE)
        disable = case.fixture.arm_ccc_hold(ROLE, bytes(2), before=ENABLE)
        self.assertEqual(disable.before, ENABLE)
        self.assertEqual(disable.requested, bytes(2))

    def test_a_declared_pre_state_is_what_the_firmware_binds(self) -> None:
        """A subscribed CCC may read back its stored default; the caller's proof wins."""
        case = self.make()
        # The central is subscribed, but a descriptor read still answers `0000`.
        case.wire.ccc_value = ENABLE
        case.wire.ccc_getter = bytes(2)
        # Undeclared, the host would bind a disable whose pre-state is its own request: a
        # no-op, which it refuses rather than arm.
        with self.assertRaises(ValueError) as raised:
            case.fixture.arm_ccc_hold(ROLE, bytes(2))
        self.assertIn("no-op", str(raised.exception))
        hold = case.fixture.arm_ccc_hold(ROLE, bytes(2), before=ENABLE)
        self.assertEqual(hold.before, ENABLE)
        # The firmware bound the declared state, and no live read was needed for it.
        self.assertEqual(case.wire.before_snapshot, ENABLE)
        self.assertEqual(case.inventory.value_reads, [CCC_HANDLE])

    def test_a_declared_no_op_is_refused_before_the_wire(self) -> None:
        case = self.make()
        with self.assertRaises(ValueError) as raised:
            case.fixture.arm_ccc_hold(ROLE, ENABLE, before=ENABLE)
        self.assertIn("no-op", str(raised.exception))

    def test_an_unknown_role_never_touches_the_wire_or_the_table(self) -> None:
        case = self.make()
        before = list(case.wire.commands)
        with self.assertRaises(ValueError) as raised:
            case.fixture.arm_ccc_hold("absent", ENABLE)
        self.assertIn("no 'absent' characteristic handle", str(raised.exception))
        self.assertEqual(case.wire.commands, before)
        self.assertEqual(case.inventory.inventory_reads, 0)

    def test_an_inventory_without_the_target_value_is_refused(self) -> None:
        case = self.make()
        case.inventory.attributes = tuple(
            item for item in case.inventory.attributes if item.handle != VALUE_HANDLE
        )
        with self.assertRaises(RuntimeError) as raised:
            case.fixture.arm_ccc_hold(ROLE, ENABLE)
        self.assertIn("does not hold", str(raised.exception))
        self.assertEqual(case.inventory.value_reads, [])

    def test_an_ambiguous_table_is_refused_instead_of_resolved_by_position(self) -> None:
        case = self.make()
        case.inventory.attributes = (
            *(
                item
                for item in case.inventory.attributes
                if item.handle != CCC_HANDLE
            ),
            GattAttribute(CCC_HANDLE, 0x03, "2902"),
            GattAttribute(CCC_HANDLE + 1, 0x03, "2902"),
            GattAttribute(NEXT_DECLARATION + 1, 0x01, "2803"),
        )
        with self.assertRaises(RuntimeError) as raised:
            case.fixture.arm_ccc_hold(ROLE, ENABLE)
        self.assertIn("exactly one CCC descriptor", str(raised.exception))

    def test_an_inactive_fixture_session_refuses_to_arm(self) -> None:
        case = self.make()
        case.fixture._stop_requested = True
        with self.assertRaises(RuntimeError):
            case.fixture.arm_ccc_hold(ROLE, ENABLE)
        self.assertEqual(case.inventory.inventory_reads, 0)


if __name__ == "__main__":
    unittest.main()
