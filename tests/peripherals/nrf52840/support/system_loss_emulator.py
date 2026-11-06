"""Owner-local system-loss BTP boundary; no USB, SDK, installation or RF."""

from __future__ import annotations

import copy
import struct
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from .. import fault_control as f
from ..btp_protocol import BtpFrame
from ..btp_session import BtpSession
from ..direct_fixture import DirectFixture
from ..gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED
from ..gatt_commands import GATT_GET_ATTRIBUTE_VALUE, GattCommands
from ..profile import load_profile
from .ccc_enable_reject_emulator import CCC_HANDLE, READ_HANDLE, VALUE_HANDLE, CccWire
from .read_fault_emulator import PEER, Clock
from .session_receipts import fake_attempt


class SystemLossWire(CccWire):
    """Exercise the real FaultController and GattCommands via BtpSession frames."""

    def __init__(self, clock: Clock, service: str) -> None:
        super().__init__(clock, "", service, enabled_readback=True)
        self.changes: dict = {}
        self.status_reads = 0
        self.limit_flags: int | None = None
        self.on_status = lambda: None

    def send(self, frame: BtpFrame) -> None:
        if frame.service == 2 and frame.opcode == GATT_GET_ATTRIBUTE_VALUE:
            handle = int.from_bytes(frame.payload[-2:], "little")
            if handle == READ_HANDLE:
                self.commands.append(frame)
                self.reply(
                    frame,
                    b"\x00"
                    + len(self.remote_value).to_bytes(2, "little")
                    + self.remote_value,
                )
                return
        if frame.service == 255 and frame.opcode in (
            f.Opcode.ARM_WRITE,
            f.Opcode.ARM_CCC,
        ):
            self.commands.append(frame)
            self.request = f.ArmRequest.decode(f.Opcode(frame.opcode), frame.payload)
            common = self.request.common
            assert self.connected and common.peer == PEER
            expected = (
                READ_HANDLE
                if self.request.operation is f.Operation.RESPONSE_WRITE
                else CCC_HANDLE
            )
            assert common.handle == expected
            assert self.request.before == (
                self.remote_value if expected == READ_HANDLE else self.ccc
            )
            self.sequence += 1
            self.ticket = f.Ticket(
                common.boot_nonce,
                common.owner_nonce,
                self.sequence,
                self.epoch,
                self.epoch,
                1,
                self.request.operation,
                expected,
            )
            self.phase = f.Phase.ARMED
            self.arm_deadline = self.clock.now + common.arm_budget_ms / 1000
            self.hold_budget_ms = common.hold_budget_ms
            self.reply(frame, b"\x02\x00" + self.ticket.encode() + bytes([self.phase]))
            return
        if frame.service == 255 and frame.opcode == f.Opcode.STATUS:
            self.commands.append(frame)
            ticket, _ = f.decode_ticket_command(f.Opcode.STATUS, frame.payload)
            assert ticket == self.ticket
            self.status_reads += 1
            self.on_status()
            if self.phase is f.Phase.RESOLUTION_QUEUED:
                self.phase = f.Phase.RETIRED
            retired = self.phase is f.Phase.RETIRED
            flags = (
                f.StatusFlags.REQUEST_CAPTURED if self.captured else f.StatusFlags(0)
            )
            if retired:
                flags |= f.StatusFlags.REARMABLE
            outcome = f.Outcome.PENDING
            if retired:
                outcome = {
                    f.Winner.DISCONNECT: f.Outcome.DISCONNECTED_UNCOMMITTED,
                    f.Winner.HOLD_EXPIRE: f.Outcome.HOLD_EXPIRED_UNCOMMITTED,
                    f.Winner.ABORT: f.Outcome.INJECTED_REJECT
                    if self.captured
                    else f.Outcome.ARM_CANCELLED,
                }[self.winner]
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
                self.request.requested if self.captured else b"",
                self.request.before,
                self.request.before,
                f.RefCounters(*(0,) * 6)
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
                outcome,
                f.InfrastructureError.NONE,
            )
            self.reply(frame, f.encode_status(replace(status, **self.changes)))
            return
        super().send(frame)
        if (
            frame.service == 255
            and frame.opcode == f.Opcode.GET_LIMITS
            and self.limit_flags is not None
        ):
            reply = self.frames.pop()
            payload = (
                reply.payload[:23]
                + struct.pack("<H", self.limit_flags)
                + reply.payload[25:]
            )
            self.frames.append(replace(reply, payload=payload))

    def capture(self) -> None:
        assert self.phase is f.Phase.ARMED
        self.captured = True
        self.phase = f.Phase.HELD
        self.hold_deadline = self.clock.now + self.hold_budget_ms / 1000


class Harness:
    """Synthetic native witnesses are explicitly not evidence of native observability."""

    def __init__(
        self, directory: Path, case: str, *, native_control: dict | None = None
    ) -> None:
        from .. import system_loss as api

        if native_control is None:
            attempt = fake_attempt(directory)
        else:
            # Only the synthetic fixture UUID is fixed to the Swift test run.
            # No physical SDK/serial boundary is reachable through this harness.
            with patch(
                "uuid.uuid4", return_value=uuid.UUID(native_control["session_id"])
            ):
                attempt = fake_attempt(
                    directory, native_control["route"]["service_uuid"]
                )
        self.root, self.plan = attempt.root, attempt.plan
        self.clock = Clock()
        self.wire = SystemLossWire(self.clock, self.plan.service_uuid)
        self.fixture = object.__new__(DirectFixture)
        self.fixture._session = BtpSession(self.wire)
        self.fixture._fixture_session = self.plan
        self.fixture._profile = load_profile(self.plan.profile_path)
        self.fixture._transport = Mock()
        self.fixture._connected_peers = set()
        self.fixture._stop_requested = False
        self.fixture._mutation_allowed = True
        self.fixture._gatt = GattCommands(self.fixture._session)
        self.identity = {
            "pid": 321,
            "boot_id": str(uuid.uuid4()),
            "owner_id": str(uuid.uuid4()),
            "backend_id": str(uuid.uuid4()),
            "initialize_command_id": "1",
        }
        self.route = {
            "service_uuid": self.plan.service_uuid,
            "connection_id": "2",
            "gatt_session_id": "3",
            "read_characteristic_id": "4",
            "updates_characteristic_id": "5",
            "ccc_descriptor_id": "6",
            "read_uuid": api.READ_UUID,
            "updates_uuid": api.UPDATES_UUID,
            "ccc_uuid": "00002902-0000-1000-8000-00805f9b34fb",
            "read_properties": ["read", "write-response", "write-command"],
            "updates_properties": ["read", "notify", "indicate"],
        }
        arguments = {
            "case": case,
            "identity": self.identity,
            "route": self.route,
            "baseline_subscription_id": "7" if case == api.CCC_DISABLE_WRITE else None,
            "baseline_command_id": "8" if case == api.CCC_DISABLE_WRITE else None,
        }
        if native_control is None:
            self.control = api.prepare_control(self.root, self.plan, **arguments)
        else:
            arguments.update({key: native_control[key] for key in arguments})
            self.identity = copy.deepcopy(arguments["identity"])
            self.route = copy.deepcopy(arguments["route"])
            before = bytes.fromhex(native_control["write_before"])
            self.wire.remote_value = before
            with patch("uuid.uuid4", return_value=uuid.UUID(native_control["token"])):
                self.control = api.prepare_control(
                    self.root,
                    self.plan,
                    **arguments,
                    write_before=before,
                    write_requested=bytes.fromhex(native_control["write_requested"]),
                )
        self.handles = {"read-write": READ_HANDLE, "updates": VALUE_HANDLE}
        self.witness = {
            "schema_version": 1,
            "control_sha256": api.control_digest(self.control),
            "identity": copy.deepcopy(self.identity),
            "events": [
                {
                    "kind": "discovered",
                    "source": "native-gatt-discovery",
                    "route": copy.deepcopy(self.route),
                }
            ],
        }
        if case == api.CCC_DISABLE_WRITE:
            self.wire.ccc = b"\x01\x00"
            self.witness["events"].extend(
                [
                    {
                        "kind": "baseline-enabled",
                        "source": "native-domain-event",
                        "command_id": self.control["baseline_command_id"],
                        "subscription_id": self.control["baseline_subscription_id"],
                    },
                    {
                        "kind": "baseline-value",
                        "source": "native-domain-event",
                        "subscription_id": self.control["baseline_subscription_id"],
                        "value_hex": "ef",
                    },
                ]
            )
        self.wire.gap(GAP_DEVICE_CONNECTED)
        self.owner = api.SystemLossCoordinator(self.fixture, self.root, self.handles)

    def pending(
        self,
        head_command_id: str = "10",
        sibling_command_id: str = "11",
        subscription_id: str = "7",
    ) -> None:
        from .. import system_loss as api

        self.wire.capture()
        write_first = self.control["case"] == api.WRITE_CCC_ENABLE
        for role, command, operation, characteristic in (
            (
                "head",
                head_command_id,
                "response-write" if write_first else "ccc-disable",
                "4" if write_first else "5",
            ),
            (
                "sibling",
                sibling_command_id,
                "ccc-enable" if write_first else "response-write",
                "5" if write_first else "4",
            ),
        ):
            self.witness["events"].append(
                {
                    "kind": "dispatch-accepted",
                    "source": "backend-dispatch-return",
                    "role": role,
                    "command_id": command,
                    "operation": operation,
                    "characteristic_id": characteristic,
                    "accepted": True,
                    "connection_id": "2",
                    "gatt_session_id": "3",
                    "subscription_id": subscription_id
                    if operation.startswith("ccc-")
                    else None,
                    "descriptor_id": "6" if operation.startswith("ccc-") else None,
                    "before_hex": "0100"
                    if operation == "ccc-disable"
                    else "0000"
                    if operation == "ccc-enable"
                    else "00",
                    "requested_hex": "0000"
                    if operation == "ccc-disable"
                    else "0100"
                    if operation == "ccc-enable"
                    else "10",
                }
            )
        self.witness["events"].append(
            {
                "kind": "queue-pending",
                "source": "native-io-lane",
                "head_command_id": head_command_id,
                "sibling_command_id": sibling_command_id,
                "subscription_id": subscription_id,
                "head_state": "pending",
                "sibling_state": "queued",
            }
        )

    def off_request(self) -> dict:
        from .. import system_loss as api

        return {
            "schema_version": 1,
            "control_sha256": api.control_digest(self.control),
            "binding_sha256": self.owner.document.get("binding_sha256"),
            "witness_sha256": api.witness_digest(self.witness),
        }

    def disconnect(self) -> None:
        self.wire.gap(GAP_DEVICE_DISCONNECTED)
