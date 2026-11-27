"""One synthetic DirectFixture bound to the v2 fault engine, per operation family.

A hold is a statement about the *peer*: the firmware parked the exact remote request and
is waiting for the owner's decision. So the tests below never invent the state a hold is
bound to. The emulator is the firmware and the inventory is the server's own attribute
table; both answer the same objects the production fixture reads, which is what lets a
`before` value, a descriptor handle and a retired receipt be checked instead of assumed.

Nothing here opens a serial port, attests a lease or performs RF.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import direct_fixture
from ..btp_session import BtpSession
from ..fault_control import FaultController
from ..gap_commands import GapCommands
from ..gatt_commands import GattAttribute, GattAttributeValue
from .fault_emulator import Clock, Wire
from .session_receipts import fake_attempt

ROLE = "read-write"
VALUE_HANDLE = 0x31
# The managed CCC descriptor sits between its characteristic's value and the next
# declaration, which is the ordering the real server uses and the resolver relies on.
VALUE_UUID = "fff10000-0000-1000-8000-00805f9b34fb"
CCC_HANDLE = 0x33
CCC_UUID = "2902"
NEXT_DECLARATION = 0x40


class Inventory:
    """The fixture-side GATT reads one hold performs, answered by the emulator.

    Both surfaces are read-only observations of the server: the attribute table and one
    value. They are recorded so a test can assert what a hold actually consulted, which is
    how "the CCC handle came from the table" differs from "the test passed a literal".
    """

    def __init__(self, wire: Wire) -> None:
        self.wire = wire
        self.attributes = (
            GattAttribute(0x0030, 0x01, "2803"),
            GattAttribute(VALUE_HANDLE, 0x43, VALUE_UUID),
            GattAttribute(CCC_HANDLE, 0x03, CCC_UUID),
            GattAttribute(NEXT_DECLARATION, 0x01, "2803"),
        )
        self.inventory_reads = 0
        self.value_reads: list[int] = []

    def get_attributes(self) -> tuple[GattAttribute, ...]:
        self.inventory_reads += 1
        return self.attributes

    def get_attribute_value(self, handle: int) -> GattAttributeValue:
        self.value_reads.append(handle)
        if handle == CCC_HANDLE:
            # The managed descriptor's getter reports the stored default, not the state the
            # central enabled; a test states the difference when it is the point.
            getter = self.wire.ccc_getter
            return GattAttributeValue(
                0, self.wire.ccc_value if getter is None else getter
            )
        return GattAttributeValue(0, self.wire.remote_value)


@dataclass(frozen=True)
class SyntheticHold:
    """The firmware, the fixture that owns it, and the inventory it reads."""

    wire: Wire
    fixture: direct_fixture.DirectFixture
    inventory: Inventory


def bind(root: Path, *, connected: bool = True) -> SyntheticHold:
    """One fixture whose BTP session is the synthetic v2 firmware, not a serial port.

    `root` is an empty directory the caller owns: the session receipt lives there, so the
    test that created it decides when it disappears instead of this module holding a
    lifetime it cannot observe.
    """
    attempt = fake_attempt(root)
    wire = Wire(Clock(), "")
    if connected:
        wire.gap(direct_fixture.GAP_DEVICE_CONNECTED)
    inventory = Inventory(wire)
    fixture = object.__new__(direct_fixture.DirectFixture)
    fixture._fixture_session = attempt.plan
    fixture._session = BtpSession(wire)
    fixture._gatt = inventory
    fixture._gap = GapCommands(fixture._session)
    fixture._connected_peers = set()
    fixture._started = True
    fixture._stopping = False
    fixture._stop_requested = False
    fixture._mutation_allowed = True
    fixture._role_handles = {ROLE: VALUE_HANDLE}
    # The provider observes the connection before any host ARM, exactly as the firmware
    # requires: GET_LIMITS is the receipt that carries the live peer.
    FaultController(fixture._session, attempt.plan.session_id).get_limits()
    return SyntheticHold(wire, fixture, inventory)
