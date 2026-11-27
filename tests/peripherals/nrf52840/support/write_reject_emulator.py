"""Synthetic phone/Peripheral boundary for the real write-reject writer and relay.

No serial discovery, firmware provisioning, native BLE or RF is performed. The
production BtpSession, GattCommands, coordinator and relay remain unmocked.
"""

from __future__ import annotations

import json
import struct
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import fault_control as f
from tests.peripherals.nrf52840 import write_reject as api
from tests.peripherals.nrf52840.btp_protocol import BtpFrame
from tests.peripherals.nrf52840.btp_session import BtpSession
from tests.peripherals.nrf52840.cancellation_control import read_document
from tests.peripherals.nrf52840.direct_fixture import DirectFixture
from tests.peripherals.nrf52840.gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
    GAP_DISCONNECT,
    GAP_STOP_ADVERTISING,
    GapCommands,
)
from tests.peripherals.nrf52840.gatt_commands import (
    GATT_GET_ATTRIBUTE_VALUE,
    GATT_GET_ATTRIBUTES,
    GattCommands,
    uuid_to_le_bytes,
)
from tests.peripherals.nrf52840.profile import load_profile
from tests.peripherals.nrf52840.support.fault_emulator import (
    PEER,
    TARGET,
    Clock,
    Wire,
)
from tests.peripherals.nrf52840.support.session_receipts import fake_attempt


class WriteWire(Wire):
    def __init__(self, clock: Clock, fault: str, target_uuid: str) -> None:
        super().__init__(clock, fault)
        self.target_uuid = target_uuid
        self.request = None
        self.write_callbacks = 0
        self.local_reads = []

    def send(self, frame: BtpFrame) -> None:
        if frame.service == 2:
            self.commands.append(frame)
            if frame.opcode == GATT_GET_ATTRIBUTES:
                uuid = uuid_to_le_bytes(self.target_uuid)
                payload = (
                    bytes([1]) + struct.pack("<HBB", TARGET, 0x43, len(uuid)) + uuid
                )
            elif frame.opcode == GATT_GET_ATTRIBUTE_VALUE:
                self.local_reads.append(self.remote_value)
                payload = (
                    bytes([1 if self.fault == "local-att-error" else 0])
                    + struct.pack("<H", len(self.remote_value))
                    + self.remote_value
                )
            else:
                raise AssertionError("unexpected GATT mutation/reset")
            self.frames.append(BtpFrame(2, frame.opcode, 0, payload))
        elif frame.service == 1:
            self.commands.append(frame)
            if frame.opcode == GAP_DISCONNECT:
                self.gap(GAP_DEVICE_DISCONNECTED)
            elif frame.opcode != GAP_STOP_ADVERTISING:
                raise AssertionError("unexpected GAP mutation")
            self.frames.append(BtpFrame(1, frame.opcode, 0, b""))
        elif frame.opcode == f.Opcode.ARM_WRITE:
            self.commands.append(frame)
            self.request = f.ArmRequest.decode(f.Opcode.ARM_WRITE, frame.payload)
            common = self.request.common
            assert (
                self.connected
                and self.remote_value == self.request.before == api.BEFORE
            )
            assert common.peer == PEER and self.request.requested == api.REQUESTED
            self.sequence += 1
            self.ticket = f.Ticket(
                common.boot_nonce,
                common.owner_nonce,
                self.sequence,
                self.epoch,
                self.epoch,
                1,
                f.Operation.RESPONSE_WRITE,
                common.handle,
            )
            self.phase = f.Phase.ARMED
            self.arm_deadline = self.clock.now + common.arm_budget_ms / 1000
            self.hold_budget_ms = common.hold_budget_ms
            self.frames.append(
                BtpFrame(
                    255,
                    frame.opcode,
                    255,
                    b"\x02\x00" + self.ticket.encode() + bytes([self.phase]),
                )
            )
        elif frame.opcode == f.Opcode.STATUS:
            self.commands.append(frame)
            ticket, _ = f.decode_ticket_command(f.Opcode.STATUS, frame.payload)
            assert ticket == self.ticket
            if self.phase is f.Phase.RESOLUTION_QUEUED:
                self.phase = f.Phase.RETIRED
            captured = self.captured
            retired = self.phase is f.Phase.RETIRED
            flags = f.StatusFlags.REQUEST_CAPTURED if captured else f.StatusFlags(0)
            if retired:
                flags |= f.StatusFlags.REARMABLE
                if captured and self.winner in (f.Winner.REJECT, f.Winner.ABORT):
                    flags |= (
                        f.StatusFlags.RESPONSE_QUEUED
                        | f.StatusFlags.ANSWER_ORDERED
                        | f.StatusFlags.TX_COMPLETED
                    )
            status = f.Status(
                ticket,
                self.phase,
                self.winner,
                flags,
                0x12 if captured else 0,
                0,
                0,
                PEER,
                0,
                0,
                api.REQUESTED if captured else b"",
                api.BEFORE,
                api.BEFORE,
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
                if retired and captured
                else f.Outcome.ARM_CANCELLED
                if retired
                else f.Outcome.PENDING,
                f.InfrastructureError.NONE,
            )
            if self.phase is f.Phase.HELD:
                if self.fault == "wrong-request":
                    status = replace(status, observed=b"\x12")
                elif self.fault == "wrong-ticket":
                    status = replace(
                        status, ticket=replace(ticket, sequence=ticket.sequence + 1)
                    )
                elif self.fault == "callback":
                    status = replace(
                        status, flags=flags | f.StatusFlags.CALLBACK_INVOKED
                    )
                elif self.fault == "commit":
                    status = replace(
                        status, flags=flags | f.StatusFlags.COMMIT_OBSERVED
                    )
                elif self.fault == "expired":
                    status = replace(status, remaining=f.Remaining(0, 0, 0, 0))
            if (
                retired
                and self.fault == "retained-drift"
                and self.remote_value == api.RECOVERED
            ):
                status = replace(status, after=api.RECOVERED)
            if retired and self.fault == "pinned-evidence":
                status = replace(status, refs=replace(status.refs, evidence_pending=1))
            self.frames.append(
                BtpFrame(255, frame.opcode, 255, f.encode_status(status))
            )
        else:
            super().send(frame)

    def capture_write(self, payload: bytes) -> None:
        assert self.phase is f.Phase.ARMED and payload == self.request.requested
        self.phase = f.Phase.HELD
        self.captured = True
        self.hold_deadline = self.clock.now + self.hold_budget_ms / 1000


class Harness:
    """A synthetic App advances only when the real writer's relay authorizes it."""

    def __init__(self, root: Path, platform: str = "android", fault: str = "") -> None:
        attempt = fake_attempt(root)
        self.root, self.plan, self.platform, self.fault = (
            attempt.root,
            attempt.plan,
            platform,
            fault,
        )
        self.control = api.prepare_control(self.root, self.plan)
        self.clock = Clock()
        profile = load_profile(self.plan.profile_path)
        self.rw = next(c for c in profile.characteristics if c.role == "read-write")
        self.wire = WriteWire(self.clock, fault, self.rw.uuid)
        self.fixture = object.__new__(DirectFixture)
        self.fixture._session = BtpSession(self.wire)
        self.fixture._fixture_session = self.plan
        self.fixture._profile = profile
        self.fixture._transport = Mock()
        self.fixture._gatt = GattCommands(self.fixture._session)
        self.fixture._gap = GapCommands(self.fixture._session)
        self.fixture._connected_peers = set()
        self.fixture._stop_requested = False
        self.fixture._mutation_allowed = True
        self.relay = api.WriteRejectRelay(self.root)
        with patch.object(api.time, "monotonic", side_effect=self.clock.monotonic):
            self.owner = api.WriteReject(
                self.fixture, self.root, {"read-write": TARGET}
            )
        self.lines: list[str] = []
        self.started = False
        self.requests = []
        self.production_boundaries = []
        self.events = []

    @property
    def text(self) -> str:
        return "".join(api.PREFIX + line + "\n" for line in self.lines)

    def marker(self, body: str) -> None:
        self.lines.append(api.RUNTIME_MODE + ":" + body)

    def event(self, kind: str, command: str | None = None, **fields) -> None:
        event = {
            "runId": self.plan.session_id,
            "nonce": "synthetic-write-boot",
            "kind": kind,
            **fields,
        }
        if command is not None:
            event["commandId"] = command
        self.events.append(event)
        self.marker("event:" + json.dumps(event))

    def step(self, name: str) -> None:
        route = ""
        if name in api.STEPS[:-2] or name == "disconnected":
            route = ":connectionId=30004"
        if name in api.STEPS[1:-2]:
            route += ":gattSessionId=30007:characteristicId=31"
        self.marker(f"step:{name}:passed{route}")

    def ready(self, request: str) -> None:
        self.requests.append(request)
        self.lines.append(self.control.marker("ready", request))
        self.relay.observe(self.text)

    def start_app(self) -> None:
        self.marker(
            f"runtime:session={self.plan.session_id}:nonce=synthetic-write-boot:phase=start"
        )
        self.lines.append(self.control.marker("listener", "registered"))
        self.event(
            "adapterSnapshotChanged",
            "30000",
            adapterState="PoweredOn",
            scanAuthorization="Authorized",
        )
        self.event("commandCompleted", "30000")
        for command, kind in (("30002", "scanStarted"), ("30003", "scanStopped")):
            self.event(kind, command, scanSessionId="30001")
            self.event("commandCompleted", command)
        self.wire.gap(GAP_DEVICE_CONNECTED)
        self.event(
            "connectionStateChanged",
            "30005",
            connectionId="30004",
            connectionState="Connecting",
        )
        self.event(
            "connectionStateChanged",
            "30005",
            connectionId="30004",
            connectionState="Connected",
        )
        self.event("commandCompleted", "30005")
        self.step("connected")
        self.event(
            "gattDiscovered",
            "30008",
            connectionId="30004",
            gattSessionId="30007",
            gattServices=[
                {
                    "uuid": self.plan.service_uuid,
                    "characteristics": [
                        {
                            "id": "31",
                            "uuid": self.rw.uuid,
                            "readable": True,
                            "writableWithResponse": True,
                        }
                    ],
                }
            ],
        )
        self.event("commandCompleted", "30008")
        self.step("gatt")
        self.read("30009", 0)
        self.step("baseline-read")
        self.ready("arm")

    def read(self, command: str, value: int) -> None:
        self.event(
            "characteristicRead",
            command,
            connectionId="30004",
            gattSessionId="30007",
            characteristicId="31",
            value=[value],
        )
        self.event("commandCompleted", command)

    def tick(self, seconds: float = 0.02) -> None:
        self.clock.sleep(seconds)
        if not self.started:
            self.started = True
            self.start_app()
        phase = self.relay.pending()
        if phase is None:
            return
        report = read_document(self.root / api.PHYSICAL_NAME)
        boundary = api.BOUNDARIES[api.PHASES.index(phase)]
        assert any(r["boundary"] == boundary for r in report["boundaries"])
        assert report["status"] == "running"
        self.production_boundaries.append(boundary)
        self.relay.forwarded(phase)
        self.lines.append(self.control.marker("observed", phase))
        if phase == "armed":
            self.wire.capture_write(api.REQUESTED)
            self.step("write-dispatched")
            self.ready("write-dispatched")
        elif phase == "held":
            self.step("queued-read-dispatched")
            if self.fault == "late-reject":
                self.clock.now += 7
            if self.fault == "lease-loss":
                self.fixture._transport.require.side_effect = RuntimeError("lease lost")
            self.ready("reject")
        elif phase == "rejected":
            native = {
                "platform": "platform_android"
                if self.platform == "android"
                else "platform_apple",
                "name": "BluetoothGatt"
                if self.platform == "android"
                else "CBATTErrorDomain",
                "code": "14",
            }
            self.event(
                "commandCompleted",
                "30010",
                error={
                    "category": "gatt",
                    "message": "ATT error",
                    "platformDetails": native,
                },
            )
            self.step("write-rejected")
            self.read("30011", 0)
            self.step("unchanged-read")
            self.wire.remote_value = api.RECOVERED
            self.wire.write_callbacks += 1
            self.event(
                "characteristicWritten",
                "30012",
                connectionId="30004",
                gattSessionId="30007",
                characteristicId="31",
            )
            self.event("commandCompleted", "30012")
            self.step("recovery-write")
            self.read("30013", 34)
            self.step("recovery-read")
            self.ready("recovery-done")
        else:
            self.wire.gap(GAP_DEVICE_DISCONNECTED)
            self.event(
                "connectionStateChanged",
                "30006",
                connectionId="30004",
                connectionState="Disconnected",
            )
            self.event("commandCompleted", "30006")
            self.step("disconnected")
            resources = dict.fromkeys(api.RESOURCE_FIELDS, 0)
            if self.fault == "native-close":
                resources["handoffRoots"] = 1
            self.marker("resources:phase=closed:snapshot=" + json.dumps(resources))
            self.step("close")
            self.marker(
                f"runtime:session={self.plan.session_id}:nonce=synthetic-write-boot:phase=close"
            )
            self.marker("passed")
            self.relay.observe(self.text)

    def run(self) -> None:
        with (
            patch.object(api.time, "monotonic", side_effect=self.clock.monotonic),
            patch.object(api.time, "sleep", side_effect=self.tick),
        ):
            self.owner.run()
