"""Owner-local synthetic phone/CCC server; never USB, installation or RF.

Real BtpSession, GattCommands, fault codec, provider writer and relay are exercised.
Descriptor handles deliberately are not adjacent and a sibling has another CCC.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import ccc_disable_reject as api
from tests.peripherals.nrf52840.btp_session import BtpSession
from tests.peripherals.nrf52840.cancellation_control import read_document
from tests.peripherals.nrf52840.direct_fixture import DirectFixture
from tests.peripherals.nrf52840.gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
    GapCommands,
)
from tests.peripherals.nrf52840.gatt_commands import (
    GattCommands,
)
from tests.peripherals.nrf52840.profile import load_profile
from tests.peripherals.nrf52840.support.ccc_wire import (
    READ_HANDLE,
    VALUE_HANDLE,
    CccWire,
)
from tests.peripherals.nrf52840.support.fault_emulator import Clock
from tests.peripherals.nrf52840.support.session_receipts import fake_attempt


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
        self.wire = CccWire(
            self.clock,
            fault,
            self.plan.service_uuid,
            before=api.BEFORE,
            requested=api.REQUESTED,
            enabled_readback=True,
        )
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
        self.relay = api.CccDisableRejectRelay(self.root)
        self.handles = {"updates": VALUE_HANDLE, "read-write": READ_HANDLE}
        with patch.object(api.time, "monotonic", side_effect=self.clock.monotonic):
            self.owner = api.CccDisableReject(self.fixture, self.root, self.handles)
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
            fields["connectionId"] = "34004"
        if (
            api.STEPS.index("gatt")
            <= api.STEPS.index(name)
            <= api.STEPS.index("end-silence-read")
        ):
            fields.update(gattSessionId="34007", characteristicId="71")
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
            "disable-dispatched",
            "disable-rejected",
            "baseline-enabled",
            "baseline-notified",
            "continued-notified",
            "disabled",
        ):
            fields["subscriptionId"] = "34009"
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
            connectionId="34004",
            gattSessionId="34007",
            characteristicId="72",
            value=[0],
        )
        self.event("commandCompleted", command)

    def state(self, command: str, state: str) -> None:
        self.event(
            "notificationStateChanged",
            command,
            connectionId="34004",
            gattSessionId="34007",
            characteristicId="71",
            subscriptionId="34009",
            notificationState=state,
            actualNotificationMode="Notification",
        )
        self.event("commandCompleted", command)

    def notification(self) -> None:
        self.event(
            "notificationReceived",
            connectionId="34004",
            gattSessionId="34007",
            characteristicId="71",
            subscriptionId="34009",
            value=[239 if len(self.wire.submissions) == 1 else 225],
        )
        self.relay.observe(self.text)

    def start_app(self) -> None:
        self.marker(
            f"runtime:session={self.plan.session_id}:nonce=synthetic-ccc-boot:phase=start"
        )
        self.lines.append(self.control.marker("listener", "registered"))
        self.event(
            "adapterSnapshotChanged",
            "34000",
            adapterState="PoweredOn",
            scanAuthorization="Authorized",
        )
        self.event("commandCompleted", "34000")
        self.step("initialize")
        self.event("scanStarted", "34002", scanSessionId="34001")
        self.event("commandCompleted", "34002")
        self.event(
            "advertisementObserved",
            scanSessionId="34001",
            serviceUuids=[self.plan.service_uuid],
            peripheralId="0",
        )
        self.event("scanStopped", "34003", scanSessionId="34001")
        self.event("commandCompleted", "34003")
        self.step("scan")
        self.wire.gap(GAP_DEVICE_CONNECTED)
        for state in ("Connecting", "Connected"):
            self.event(
                "connectionStateChanged",
                "34005",
                connectionId="34004",
                connectionState=state,
            )
        self.event("commandCompleted", "34005")
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
            "34008",
            connectionId="34004",
            gattSessionId="34007",
            gattServices=[
                {
                    "uuid": self.plan.service_uuid,
                    "primary": True,
                    "characteristics": chars,
                }
            ],
        )
        self.event("commandCompleted", "34008")
        self.step("gatt")
        self.read("34016")
        self.step("baseline-read")
        self.wire.ccc = api.BEFORE
        self.state("34010", "Enabled")
        self.step("baseline-enabled", actualMode="Notification")
        self.ready("baseline-enabled")

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
        if phase == "baseline-value-sent":
            if not self.inline:
                self.notification()
            self.step("baseline-notified")
            if self.fault == "lost-enabled":
                self.wire.ccc = bytes(2)
            self.ready("arm")
        elif phase == "armed":
            self.wire.capture()
            self.step("disable-dispatched")
            self.ready("disable-dispatched")
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
                "34011",
                error={
                    "category": "gatt",
                    "message": "ATT error",
                    "platformDetails": native,
                },
            )
            self.step("disable-rejected")
            self.read("34012")
            self.step("queued-read")
            warm = dict.fromkeys(api.RESOURCE_FIELDS, 0)
            warm.update(
                connections=1,
                gattAttributes=8,
                subscriptions=1,
                hubRoots=1,
                listeners=1,
            )
            self.marker("resources:phase=failed-disable:snapshot=" + json.dumps(warm))
            if self.fault == "remote-disabled":
                self.wire.ccc = bytes(2)
            self.ready("reject-observed")
        elif phase == "continued-value-sent":
            if not self.inline:
                self.notification()
            self.step("continued-notified")
            self.ready("continued-notified")
        elif phase == "retry-allowed":
            self.wire.ccc = api.REQUESTED
            self.state("34013", "Disabled")
            self.step("disabled")
            self.read("34014")
            self.step("post-disable-read")
            self.ready("disabled")
        elif phase == "silence-started":
            if self.fault != "short-host-silence":
                self.clock.sleep(1.001)
            self.step("silence", elapsedMs=1000)
            self.read("34015")
            self.step("end-silence-read")
            self.ready("silence-done")
        else:
            self.wire.gap(GAP_DEVICE_DISCONNECTED)
            self.event(
                "connectionStateChanged",
                "34006",
                connectionId="34004",
                connectionState="Disconnected",
            )
            self.event("commandCompleted", "34006")
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
