"""Synthetic GATT/CCC wire shared by the CCC reject fixtures; never USB or RF.

The real BtpSession, GattCommands, fault codec and provider writer are exercised.
Descriptor handles deliberately are not adjacent and a sibling has its own CCC.

Extracted from the retired CCC-enable-reject emulator so the surviving
CCC-disable-reject fixture does not import a deleted gate.
"""

from __future__ import annotations

import struct
from dataclasses import replace

from tests.peripherals.nrf52840 import ccc_common
from tests.peripherals.nrf52840 import fault_control as f
from tests.peripherals.nrf52840.btp_protocol import BtpFrame
from tests.peripherals.nrf52840.gap_commands import (
    GAP_DEVICE_DISCONNECTED,
    GAP_DISCONNECT,
    GAP_STOP_ADVERTISING,
)
from tests.peripherals.nrf52840.gatt_commands import (
    GATT_GET_ATTRIBUTE_VALUE,
    GATT_GET_ATTRIBUTES,
    GATT_GET_HANDLE_FROM_UUID,
    GATT_SET_VALUE,
    GattAttribute,
    uuid_to_le_bytes,
)
from tests.peripherals.nrf52840.support.fault_emulator import PEER, Clock, Wire

READ_HANDLE, VALUE_HANDLE, CCC_HANDLE = 34, 40, 45
# CCC value before and during the mutation: enabled then disabled.
BEFORE = bytes.fromhex("0100")
REQUESTED = bytes.fromhex("0000")


class CccWire(Wire):
    def __init__(
        self,
        clock: Clock,
        fault: str,
        service: str,
        *,
        before: bytes = BEFORE,
        requested: bytes = REQUESTED,
        enabled_readback: bool = False,
    ) -> None:
        super().__init__(clock, fault)
        self.database = 1
        self.before, self.requested = before, requested
        self.enabled_readback = enabled_readback
        self.ccc = b"\x00\x00"
        self.local_reads: list[tuple[int, bytes]] = []
        self.declaration_reads: list[int] = []
        self.uuid_lookups: list[str] = []
        self.submissions: list[tuple[int, bytes]] = []
        self.on_emit = lambda: None
        self.request = None
        self.declarations = {
            30: uuid_to_le_bytes(service),
            31: struct.pack("<BH", 0x0E, READ_HANDLE)
            + uuid_to_le_bytes(ccc_common.READ_UUID),
            36: struct.pack("<BH", 0x32, VALUE_HANDLE)
            + uuid_to_le_bytes(ccc_common.UPDATES_UUID),
            50: struct.pack("<BH", 0x22, 52)
            + uuid_to_le_bytes("fff50000-0000-4000-8000-000000000000"),
        }
        self.attrs = [
            GattAttribute(30, 1, "2800"),
            GattAttribute(31, 1, "2803"),
            GattAttribute(READ_HANDLE, 0x43, ccc_common.READ_UUID),
            GattAttribute(36, 1, "2803"),
            GattAttribute(VALUE_HANDLE, 0x41, ccc_common.UPDATES_UUID),
            GattAttribute(41, 1, "2901"),
            GattAttribute(CCC_HANDLE, 3, "2902"),
            GattAttribute(50, 1, "2803"),
            GattAttribute(52, 0x41, "fff50000-0000-4000-8000-000000000000"),
            GattAttribute(54, 3, "2902"),
        ]
        if fault == "duplicate-ccc":
            self.attrs.insert(7, GattAttribute(47, 3, "2902"))
        if fault == "sibling-ccc-only":
            self.attrs = [a for a in self.attrs if a.handle != CCC_HANDLE]
        if fault == "wrong-permissions":
            self.attrs[6] = replace(self.attrs[6], permissions=0x43)
        if fault == "wrong-order":
            self.attrs.insert(4, GattAttribute(37, 1, "2901"))
        if fault == "split-service":
            self.attrs.insert(3, GattAttribute(35, 1, "2800"))
        if fault == "duplicate-value-outside":
            self.attrs.extend(
                [
                    GattAttribute(60, 1, "2800"),
                    GattAttribute(61, 1, "2803"),
                    GattAttribute(64, 0x41, ccc_common.UPDATES_UUID),
                ]
            )
        if fault == "enabled-before":
            self.ccc = self.requested

    def reply(self, frame: BtpFrame, payload: bytes = b"") -> None:
        self.frames.append(BtpFrame(frame.service, frame.opcode, frame.index, payload))

    def send(self, frame: BtpFrame) -> None:
        if frame.service == 2:
            self.commands.append(frame)
            if frame.opcode == GATT_GET_ATTRIBUTES:
                payload = bytes([len(self.attrs)]) + b"".join(
                    struct.pack(
                        "<HBB", a.handle, a.permissions, len(uuid_to_le_bytes(a.uuid))
                    )
                    + uuid_to_le_bytes(a.uuid)
                    for a in self.attrs
                )
            elif frame.opcode == GATT_GET_HANDLE_FROM_UUID:
                assert frame.payload[0] == len(frame.payload) - 1
                matches = [
                    a
                    for a in self.attrs
                    if uuid_to_le_bytes(a.uuid) == frame.payload[1:]
                ]
                self.uuid_lookups.append(matches[0].uuid if matches else "unknown")
                if not matches or self.fault == "lookup-error":
                    self.frames.append(BtpFrame(2, 0, 0, b"\x01"))
                    return
                handle = matches[0].handle
                if (
                    self.fault == "wrong-lookup"
                    and matches[0].uuid == ccc_common.UPDATES_UUID
                ):
                    handle = 52
                payload = struct.pack("<H", handle)
            elif frame.opcode == GATT_GET_ATTRIBUTE_VALUE:
                handle = int.from_bytes(frame.payload[-2:], "little")
                att = 0
                if handle == CCC_HANDLE:
                    assert frame.payload[:7] == PEER, (
                        "CCC read lost/reversed the live peer"
                    )
                    self.local_reads.append((handle, self.ccc))
                    value = self.ccc
                    if value == bytes.fromhex("0100") and not self.enabled_readback:
                        att = 12  # Installed Tester's enabled-CCC userdata defect.
                    if self.fault == "local-att-error":
                        att = 1
                else:
                    self.declaration_reads.append(handle)
                    value = self.declarations[handle]
                    att = (
                        12  # Same unsafe normal-value cast for service/chrc user_data.
                    )
                payload = bytes([att]) + struct.pack("<H", len(value)) + value
            elif frame.opcode == GATT_SET_VALUE:
                assert self.phase is f.Phase.RETIRED or (
                    self.enabled_readback and self.phase is f.Phase.IDLE
                ), "mutation while fault owner is busy"
                handle, length = struct.unpack_from("<HH", frame.payload)
                value = frame.payload[4:]
                assert handle == VALUE_HANDLE and length == len(value) == 1
                self.submissions.append((handle, value))
                self.database += 1
                if self.fault == "submission-failure":
                    self.frames.append(BtpFrame(2, 0, 0, b"\x01"))
                    return
                if value in (b"\xef", b"\xe1"):
                    assert self.ccc == bytes.fromhex("0100")
                    self.on_emit()
                else:
                    assert self.ccc == bytes(2)
                payload = b""
            else:
                raise AssertionError("unexpected database provisioning/reset")
            self.reply(frame, payload)
        elif frame.service == 1:
            self.commands.append(frame)
            if frame.opcode == GAP_DISCONNECT:
                self.gap(GAP_DEVICE_DISCONNECTED)
            else:
                assert frame.opcode == GAP_STOP_ADVERTISING
            self.reply(frame)
        elif frame.opcode == f.Opcode.ARM_CCC:
            self.commands.append(frame)
            self.request = f.ArmRequest.decode(f.Opcode.ARM_CCC, frame.payload)
            common = self.request.common
            assert (
                self.connected and common.peer == PEER and common.handle == CCC_HANDLE
            )
            assert (
                self.request.before == self.ccc == self.before
                and self.request.requested == self.requested
            )
            self.sequence += 1
            self.ticket = f.Ticket(
                common.boot_nonce,
                common.owner_nonce,
                self.sequence,
                self.epoch,
                self.epoch,
                self.database,
                f.Operation.CCC_WRITE,
                CCC_HANDLE,
            )
            self.phase = f.Phase.ARMED
            self.arm_deadline = self.clock.now + common.arm_budget_ms / 1000
            self.hold_budget_ms = common.hold_budget_ms
            self.reply(frame, b"\x02\x00" + self.ticket.encode() + bytes([self.phase]))
        elif frame.opcode == f.Opcode.STATUS:
            self.commands.append(frame)
            ticket, _ = f.decode_ticket_command(f.Opcode.STATUS, frame.payload)
            assert ticket == self.ticket
            if self.phase is f.Phase.RESOLUTION_QUEUED:
                self.phase = f.Phase.RETIRED
            retired = self.phase is f.Phase.RETIRED
            flags = (
                f.StatusFlags.REQUEST_CAPTURED if self.captured else f.StatusFlags(0)
            )
            if retired:
                flags |= f.StatusFlags.REARMABLE
                if self.captured and self.winner in (f.Winner.REJECT, f.Winner.ABORT):
                    flags = ccc_common.REJECT_FLAGS
            status = f.Status(
                ticket,
                self.phase,
                self.winner,
                flags,
                0x12 if self.captured else 0,
                0,
                0,
                PEER,
                0,
                0,
                self.requested if self.captured else b"",
                self.before,
                self.before,
                f.RefCounters(0, 0, 0, 0, 0, 0)
                if retired
                else f.RefCounters(1, 1, 1, 1, 0, 0),
                f.Remaining(
                    self.remaining(self.arm_deadline)
                    if self.phase is f.Phase.ARMED
                    else 0,
                    self.remaining(self.hold_deadline)
                    if self.phase is f.Phase.HELD
                    else 0,
                    0,
                    0,
                ),
                f.Outcome.INJECTED_REJECT
                if retired and self.captured
                else f.Outcome.ARM_CANCELLED
                if retired
                else f.Outcome.PENDING,
                f.InfrastructureError.NONE,
            )
            if self.phase is f.Phase.HELD:
                if self.fault == "wrong-request":
                    status = replace(status, observed=b"\x02\x00")
                elif self.fault == "wrong-ticket":
                    status = replace(
                        status, ticket=replace(ticket, sequence=ticket.sequence + 1)
                    )
                elif self.fault in ("callback", "commit", "normal-path"):
                    flag = {
                        "callback": f.StatusFlags.CALLBACK_INVOKED,
                        "commit": f.StatusFlags.COMMIT_OBSERVED,
                        "normal-path": f.StatusFlags.NORMAL_PATH_EVALUATED,
                    }[self.fault]
                    status = replace(status, flags=flags | flag)
                elif self.fault == "expired":
                    status = replace(status, remaining=f.Remaining(0, 0, 0, 0))
            if retired and self.fault == "retained-drift" and self.submissions:
                status = replace(status, callback_result=2)
            if retired and self.fault == "pinned-evidence":
                status = replace(status, refs=replace(status.refs, evidence_pending=1))
            self.reply(frame, f.encode_status(status))
        else:
            super().send(frame)
            if frame.opcode == f.Opcode.GET_LIMITS and self.connected:
                response = self.frames.pop()
                self.frames.append(
                    replace(
                        response,
                        payload=response.payload[:-4]
                        + struct.pack("<I", self.database),
                    )
                )

    def capture(self) -> None:
        assert self.phase is f.Phase.ARMED
        self.phase = f.Phase.HELD
        self.captured = True
        self.hold_deadline = self.clock.now + self.hold_budget_ms / 1000
