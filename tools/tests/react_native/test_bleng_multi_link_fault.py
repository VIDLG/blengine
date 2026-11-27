"""The declared fault's host side: arm before the write, refuse once, keep the receipt.

These are the facts the App cannot see. The scene proves the refusal from the App's own
failure; this file pins that the host armed the exact write, resolved it exactly once, and
kept the peer's own receipt instead of inferring one from a symptom.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.peripherals.nrf52840.fault_control import Decision, Operation, Phase
from tools.react_native import bleng_multi_link_fault as fault_module
from tools.react_native.bleng_multi_link_contract import (
    FAULT_WRITE_REJECT,
    READ_WRITE_ROLE,
)


class FakeFixture:
    def __init__(self, *phases: Phase, receipt: object | None = None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._phases = list(phases)
        self._receipt = receipt or SimpleNamespace(
            winner=SimpleNamespace(name="REJECT"),
            outcome=SimpleNamespace(name="INJECTED_REJECT"),
        )

    def arm_hold(self, operation: object, role: str, payload: bytes) -> str:
        self.calls.append(("arm", operation, role, payload))
        return "ticket"

    def hold_phase(self, hold: str) -> Phase:
        self.calls.append(("phase", hold))
        return self._phases.pop(0)

    def release_hold(self, hold: str, decision: Decision) -> object:
        self.calls.append(("release", hold, decision))
        return self._receipt


class WriteRejectFaultTests(unittest.TestCase):
    def fault(self, fixture: FakeFixture, payload: bytes = b"\xef") -> object:
        return fault_module.WriteRejectFault(fixture, payload=payload)

    def refuse(self, fault: object, *, refused: bool = True) -> None:
        with patch.object(fault_module, "injected_reject", return_value=refused):
            fault.service()

    def test_a_payload_is_required(self):
        for payload in (b"", "ef"):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.fault(FakeFixture(), payload=payload)  # type: ignore[arg-type]

    def test_the_hold_is_armed_for_the_exact_write_the_app_will_attempt(self):
        fixture = FakeFixture()
        self.fault(fixture).arm()
        self.assertEqual(
            fixture.calls,
            [("arm", Operation.RESPONSE_WRITE, READ_WRITE_ROLE, b"\xef")],
        )

    def test_an_armed_but_uncaptured_write_is_not_refused_yet(self):
        fixture = FakeFixture(Phase.ARMED)
        fault = self.fault(fixture)
        fault.arm()
        self.refuse(fault)
        self.assertFalse(fault.resolved)
        self.assertEqual([call[0] for call in fixture.calls], ["arm", "phase"])

    def test_the_first_captured_write_is_refused_exactly_once(self):
        fixture = FakeFixture(Phase.HELD)
        fault = self.fault(fixture)
        fault.arm()
        self.refuse(fault)
        self.refuse(fault)
        self.assertTrue(fault.resolved)
        self.assertEqual(
            [call[0] for call in fixture.calls], ["arm", "phase", "release"]
        )
        self.assertEqual(fixture.calls[-1][2], Decision.REJECT)

    def test_a_hold_that_leaves_armed_without_capturing_is_reported(self):
        fault = self.fault(FakeFixture(Phase.RETIRED))
        fault.arm()
        with self.assertRaises(fault_module.MultiLinkFaultError):
            self.refuse(fault)
        self.assertFalse(fault.resolved)

    def test_a_receipt_that_is_not_a_refusal_fails_the_fault(self):
        fault = self.fault(FakeFixture(Phase.HELD))
        fault.arm()
        with self.assertRaises(fault_module.MultiLinkFaultError):
            self.refuse(fault, refused=False)
        self.assertTrue(fault.resolved)

    def test_a_fault_cannot_be_armed_twice(self):
        fault = self.fault(FakeFixture())
        fault.arm()
        with self.assertRaises(fault_module.MultiLinkFaultError):
            fault.arm()

    def test_evidence_states_whether_the_peer_actually_refused(self):
        unresolved = self.fault(FakeFixture())
        self.assertEqual(
            unresolved.evidence(), {"kind": FAULT_WRITE_REJECT, "resolved": False}
        )
        resolved = self.fault(FakeFixture(Phase.HELD))
        resolved.arm()
        self.refuse(resolved)
        self.assertEqual(
            resolved.evidence(),
            {
                "kind": FAULT_WRITE_REJECT,
                "resolved": True,
                "refused": True,
                "winner": "REJECT",
                "outcome": "INJECTED_REJECT",
            },
        )


if __name__ == "__main__":
    unittest.main()
