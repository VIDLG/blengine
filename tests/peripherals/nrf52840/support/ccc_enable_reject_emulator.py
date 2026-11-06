"""Owner-local synthetic phone/CCC server; never USB, installation or RF.

Real BtpSession, GattCommands, fault codec, provider writer and relay are exercised.
Descriptor handles deliberately are not adjacent and a sibling has another CCC.
"""

from __future__ import annotations

import json
import struct
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import ccc_enable_reject as api
from tests.peripherals.nrf52840 import fault_control as f
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
    GATT_GET_HANDLE_FROM_UUID,
    GATT_SET_VALUE,
    GattAttribute,
    GattCommands,
    uuid_to_le_bytes,
)
from tests.peripherals.nrf52840.profile import load_profile
from tests.peripherals.nrf52840.support.read_fault_emulator import PEER, Clock, Wire
from tests.peripherals.nrf52840.support.session_receipts import fake_attempt

READ_HANDLE, VALUE_HANDLE, CCC_HANDLE = 34, 40, 45


class CccWire(Wire):
    def __init__(
        self,
        clock: Clock,
        fault: str,
        service: str,
        *,
        before: bytes = api.BEFORE,
        requested: bytes = api.REQUESTED,
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
            31: struct.pack("<BH", 0x0E, READ_HANDLE) + uuid_to_le_bytes(api.READ_UUID),
            36: struct.pack("<BH", 0x32, VALUE_HANDLE)
            + uuid_to_le_bytes(api.UPDATES_UUID),
            50: struct.pack("<BH", 0x22, 52)
            + uuid_to_le_bytes("fff50000-0000-4000-8000-000000000000"),
        }
        self.attrs = [
            GattAttribute(30, 1, "2800"),
            GattAttribute(31, 1, "2803"),
            GattAttribute(READ_HANDLE, 0x43, api.READ_UUID),
            GattAttribute(36, 1, "2803"),
            GattAttribute(VALUE_HANDLE, 0x41, api.UPDATES_UUID),
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
                    GattAttribute(64, 0x41, api.UPDATES_UUID),
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
                if self.fault == "wrong-lookup" and matches[0].uuid == api.UPDATES_UUID:
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
                    flags = api.REJECT_FLAGS
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


class Harness:
    def __init__(
        self,
        root: Path,
        platform: str = "android",
        fault: str = "",
        *,
        inline: bool = True,
    ) -> None:
        attempt = fake_attempt(root)
        self.root, self.plan, self.platform, self.fault = (
            attempt.root,
            attempt.plan,
            platform,
            fault,
        )
        self.control = api.prepare_control(self.root, self.plan)
        self.clock = Clock()
        self.wire = CccWire(self.clock, fault, self.plan.service_uuid)
        self.fixture = object.__new__(DirectFixture)
        self.fixture._session = BtpSession(self.wire)
        self.fixture._fixture_session = self.plan
        self.fixture._profile = load_profile(self.plan.profile_path)
        self.fixture._transport = Mock()
        self.fixture._gatt = GattCommands(self.fixture._session)
        self.fixture._gap = GapCommands(self.fixture._session)
        self.fixture._connected_peers = set()
        self.fixture._stop_requested = False
        self.fixture._mutation_allowed = True
        self.relay = api.CccEnableRejectRelay(self.root)
        self.handles = {"updates": VALUE_HANDLE, "read-write": READ_HANDLE}
        with patch.object(api.time, "monotonic", side_effect=self.clock.monotonic):
            self.owner = api.CccEnableReject(self.fixture, self.root, self.handles)
        self.lines: list[str] = []
        self.events: list[dict] = []
        self.requests: list[str] = []
        self.started = False
        self.inline = inline
        if inline:
            self.wire.on_emit = self.notification

    @property
    def text(self) -> str:
        return "".join(api.PREFIX + line + "\n" for line in self.lines)

    def marker(self, body: str) -> None:
        self.lines.append(api.RUNTIME_MODE + ":" + body)

    def event(self, kind: str, command: str | None = None, **fields) -> None:
        event = dict(
            runId=self.plan.session_id, nonce="synthetic-ccc-boot", kind=kind, **fields
        )
        if command is not None:
            event["commandId"] = command
        self.events.append(event)
        self.marker("event:" + json.dumps(event))

    def step(self, name: str, **fields) -> None:
        if name in api.COMMANDS:
            fields["commandId"] = api.COMMANDS[name]
        if name not in ("initialize", "scan", "close"):
            fields["connectionId"] = "33004"
        if (
            api.STEPS.index("gatt")
            <= api.STEPS.index(name)
            <= api.STEPS.index("end-silence-read")
        ):
            fields.update(gattSessionId="33007", characteristicId="71")
        if name in (
            "baseline-read",
            "queued-read-dispatched",
            "queued-read",
            "post-disable-read",
            "end-silence-read",
        ):
            fields["characteristicId"] = "72"
        if name == "gatt":
            fields["readCharacteristicId"] = "72"
        if name in (
            "enable-dispatched",
            "enable-rejected",
            "recovery-enabled",
            "recovery-notified",
            "disabled",
        ):
            fields["subscriptionId"] = (
                "33009" if name in ("enable-dispatched", "enable-rejected") else "33012"
            )
        self.marker(
            "step:" + name + ":passed" + "".join(f":{k}={v}" for k, v in fields.items())
        )

    def ready(self, request: str) -> None:
        self.requests.append(request)
        self.lines.append(self.control.marker("ready", request))
        self.relay.observe(self.text)

    def read(self, command: str) -> None:
        self.event(
            "characteristicRead",
            command,
            connectionId="33004",
            gattSessionId="33007",
            characteristicId="72",
            value=[0],
        )
        self.event("commandCompleted", command)

    def state(self, command: str, state: str) -> None:
        self.event(
            "notificationStateChanged",
            command,
            connectionId="33004",
            gattSessionId="33007",
            characteristicId="71",
            subscriptionId="33012",
            notificationState=state,
            actualNotificationMode="Notification",
        )
        self.event("commandCompleted", command)

    def notification(self) -> None:
        self.event(
            "notificationReceived",
            connectionId="33004",
            gattSessionId="33007",
            characteristicId="71",
            subscriptionId="33012",
            value=[239],
        )
        self.relay.observe(self.text)

    def start_app(self) -> None:
        self.marker(
            f"runtime:session={self.plan.session_id}:nonce=synthetic-ccc-boot:phase=start"
        )
        self.lines.append(self.control.marker("listener", "registered"))
        self.event(
            "adapterSnapshotChanged",
            "33000",
            adapterState="PoweredOn",
            scanAuthorization="Authorized",
        )
        self.event("commandCompleted", "33000")
        self.step("initialize")
        self.event("scanStarted", "33002", scanSessionId="33001")
        self.event("commandCompleted", "33002")
        self.event(
            "advertisementObserved",
            scanSessionId="33001",
            serviceUuids=[self.plan.service_uuid],
            peripheralId="0",
        )
        self.event("scanStopped", "33003", scanSessionId="33001")
        self.event("commandCompleted", "33003")
        self.step("scan")
        self.wire.gap(GAP_DEVICE_CONNECTED)
        for state in ("Connecting", "Connected"):
            self.event(
                "connectionStateChanged",
                "33005",
                connectionId="33004",
                connectionState=state,
            )
        self.event("commandCompleted", "33005")
        self.step("connected")
        chars = [
            {
                "id": "71",
                "uuid": api.UPDATES_UUID,
                "readable": True,
                "writableWithResponse": False,
                "writableWithoutResponse": False,
                "notifiable": True,
                "indicatable": True,
                "descriptors": [
                    {"id": "73", "uuid": "00002902-0000-1000-8000-00805f9b34fb"}
                ],
            },
            {
                "id": "72",
                "uuid": api.READ_UUID,
                "readable": True,
                "writableWithResponse": True,
                "writableWithoutResponse": True,
                "notifiable": False,
                "indicatable": False,
                "descriptors": [],
            },
        ]
        self.event(
            "gattDiscovered",
            "33008",
            connectionId="33004",
            gattSessionId="33007",
            gattServices=[
                {
                    "uuid": self.plan.service_uuid,
                    "primary": True,
                    "characteristics": chars,
                }
            ],
        )
        self.event("commandCompleted", "33008")
        self.step("gatt")
        self.read("33017")
        self.step("baseline-read")
        self.ready("arm")

    def tick(self, seconds: float = 0.02) -> None:
        self.clock.sleep(seconds)
        if not self.started:
            self.started = True
            self.start_app()
        phase = self.relay.pending()
        if phase is None:
            return
        report = read_document(self.root / api.PHYSICAL_NAME)
        assert report["status"] == "running"
        assert any(r["boundary"] == phase for r in report["boundaries"])
        self.relay.forwarded(phase)
        self.lines.append(self.control.marker("observed", phase))
        if phase == "armed":
            self.wire.capture()
            self.step("enable-dispatched")
            self.ready("enable-dispatched")
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
                "33010",
                error={
                    "category": "gatt",
                    "message": "ATT error",
                    "platformDetails": native,
                },
            )
            self.step("enable-rejected")
            warm = dict.fromkeys(api.RESOURCE_FIELDS, 0)
            warm.update(connections=1, gattAttributes=8, hubRoots=1, listeners=1)
            self.marker("resources:phase=failed-enable:snapshot=" + json.dumps(warm))
            self.read("33011")
            self.step("queued-read")
            self.wire.ccc = api.REQUESTED
            self.state("33013", "Enabled")
            self.step("recovery-enabled", actualMode="Notification")
            self.ready("recovery-enabled")
        elif phase == "value-sent":
            if not self.inline:
                self.notification()
            self.step("recovery-notified")
            self.ready("recovery-notified")
        elif phase == "disable-allowed":
            self.wire.ccc = api.BEFORE
            self.state("33014", "Disabled")
            self.step("disabled")
            self.read("33015")
            self.step("post-disable-read")
            self.ready("disabled")
        elif phase == "silence-started":
            if self.fault != "short-host-silence":
                self.clock.sleep(1.001)
            self.step("silence", elapsedMs=1000)
            self.read("33016")
            self.step("end-silence-read")
            self.ready("silence-done")
        else:
            self.wire.gap(GAP_DEVICE_DISCONNECTED)
            self.event(
                "connectionStateChanged",
                "33006",
                connectionId="33004",
                connectionState="Disconnected",
            )
            self.event("commandCompleted", "33006")
            self.step("disconnected")
            resources = dict.fromkeys(api.RESOURCE_FIELDS, 0)
            if self.fault == "native-close":
                resources["handoffRoots"] = 1
            self.marker("resources:phase=closed:snapshot=" + json.dumps(resources))
            self.step("close")
            self.marker(
                f"runtime:session={self.plan.session_id}:nonce=synthetic-ccc-boot:phase=close"
            )
            self.marker("passed")
            self.relay.observe(self.text)

    def run(self) -> None:
        with (
            patch.object(api.time, "monotonic", side_effect=self.clock.monotonic),
            patch.object(api.time, "sleep", side_effect=self.tick),
        ):
            self.owner.run()
