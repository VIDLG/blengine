"""Synchronous simulated firmware/Central using the real BTP dispatcher."""

from __future__ import annotations

import json
import struct
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import fault_control as fault
from tests.peripherals.nrf52840 import recovery_ten as api
from tests.peripherals.nrf52840.btp_protocol import BtpFrame
from tests.peripherals.nrf52840.btp_session import BtpSession
from tests.peripherals.nrf52840.gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
    GAP_DISCONNECT,
    GAP_START_ADVERTISING,
    GAP_STOP_ADVERTISING,
    GapCommands,
)
from tests.peripherals.nrf52840.gatt_commands import GATT_SET_VALUE, GattCommands
from tests.peripherals.nrf52840.profile import load_profile
from tests.peripherals.nrf52840.support.read_fault_emulator import read_receipt
from tests.peripherals.nrf52840.support.session_receipts import fake_attempt

BOOT = "22222222-2222-4222-8222-222222222222"

HANDLES = {
    "read-write": 51,
    "updates": 54,
    "read-only": 57,
    "write-only": 59,
    "indications": 61,
}


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Wire:
    """Synchronous simulated firmware, still using the real BTP dispatcher."""

    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self.frames: deque[BtpFrame] = deque()
        self.commands: list[BtpFrame] = []
        self.advertising = False
        self.remote_reads: list[tuple[int, int, bytes]] = []
        self.retained_status: fault.Status | None = None
        self.pending_evidence = 0
        self.evidence_on_gap = 0
        self.busy_limits = 0
        self.limits_flags: int | None = None
        self.limits_boot = bytes(range(1, 17))

    def gap(self, connected: bool, peer: bytes, fault: str = "") -> None:
        self.pending_evidence = max(self.pending_evidence, self.evidence_on_gap)
        payload = peer + (bytes(6) if connected else b"")
        if fault == "malformed":
            payload = payload[:-1]
        self.frames.append(
            BtpFrame(
                1,
                GAP_DEVICE_CONNECTED if connected else GAP_DEVICE_DISCONNECTED,
                0,
                payload,
            )
        )
        if fault == "duplicate":
            self.frames.append(self.frames[-1])

    def send(self, frame: BtpFrame) -> None:
        self.commands.append(frame)
        h = self.harness
        payload = b""
        if frame.service == 255 and frame.opcode == fault.Opcode.STATUS:
            ticket, _ = fault.decode_ticket_command(fault.Opcode.STATUS, frame.payload)
            if self.retained_status is None or ticket != self.retained_status.ticket:
                payload = b"\x02\x03"
            else:
                snapshot = replace(
                    self.retained_status,
                    refs=replace(
                        self.retained_status.refs,
                        evidence_pending=self.pending_evidence,
                    ),
                )
                payload = fault.encode_status(snapshot)
                self.pending_evidence = max(0, self.pending_evidence - 1)
        elif frame.service == 255 and frame.opcode == 0x10:
            active = h.fault == "foreign-hold" and h.phase == "subscription-enabled"
            payload = (
                b"\x02\x00"
                + self.limits_boot
                + struct.pack(
                    "<BBBBBHIIIIB7sIII",
                    7,
                    0,
                    1,
                    1,
                    20,
                    self.limits_flags
                    if self.limits_flags is not None
                    else 4
                    if active or self.pending_evidence or self.busy_limits
                    else 0,
                    10000,
                    20000,
                    2000,
                    5000,
                    0,
                    bytes(7),
                    0,
                    0,
                    0,
                )
            )
            self.busy_limits = max(0, self.busy_limits - 1)

        elif frame.service == 2 and frame.opcode == GATT_SET_VALUE:
            handle, length = struct.unpack("<HH", frame.payload[:4])
            value = frame.payload[4:]
            if handle != HANDLES["updates"] or value != b"\xef" or length != 1:
                raise AssertionError(
                    "coordinator mutated RW or used wrong value/handle"
                )
            h.append_cycle(
                "notification-value",
                f"connectionId={h.base + 4}:gattSessionId={h.base + 6}:subscriptionId={h.base + 8}:value=ef",
            )
        elif frame.service == 1 and frame.opcode == GAP_DISCONNECT:
            requested_peer = (frame.payload[1:7][::-1].hex(), frame.payload[0])
            if h.fixture._connected_peers != {requested_peer}:
                raise AssertionError("provider disconnected an arbitrary peer")
            peer = (
                bytes.fromhex("01665544332211")
                if h.fault == "foreign-disconnect"
                else frame.payload
            )
            self.gap(
                False, peer, "duplicate" if h.fault == "duplicate-disconnect" else ""
            )
            if h.phase == "notification-observed":
                h.passive()
        elif frame.service == 1 and frame.opcode == GAP_STOP_ADVERTISING:
            if h.fault == "cleanup-failure" and h.phase == "subscription-enabled":
                raise RuntimeError("injected advertising cleanup failure")
            self.advertising = False
        elif frame.service == 1 and frame.opcode == GAP_START_ADVERTISING:
            self.advertising = True
        else:
            raise AssertionError(f"unexpected synthetic wire command {frame}")
        self.frames.append(BtpFrame(frame.service, frame.opcode, frame.index, payload))

    def receive(self, timeout: float) -> BtpFrame:
        self.harness.clock.now += 0.001
        if self.frames:
            return self.frames.popleft()
        self.harness.clock.now += timeout
        raise TimeoutError("synthetic idle BTP transport")

    def close(self) -> None:
        pass


class Harness:
    """The synthetic Central is driven by provider progress, never by extra owners."""

    def __init__(self, directory: Path, platform: str = "ios", fault: str = "") -> None:
        self.plan = fake_attempt(directory).plan
        self.root = self.plan.root
        self.platform, self.fault = platform, fault
        self.clock = Clock()
        self.retained_ticket: fault.Ticket | None = None
        self.lines: list[str] = []
        self.phases: list[dict[str, object]] = []
        self.cycle = 0
        self.phase = ""
        self.wire = Wire(self)
        self.session = BtpSession(self.wire)
        self.fixture = SimpleNamespace(
            _fixture_session=self.plan,
            _session=self.session,
            _profile=load_profile(self.plan.profile_path),
            _transport=Mock(),
            _connected_peers=set(),
            _stop_requested=False,
            _gap=GapCommands(self.session),
            _gatt=GattCommands(self.session),
        )
        # Profile inspection is mocked; all control/status/value/GAP commands use BTP.
        self.fixture._gatt.get_attributes = Mock(
            side_effect=lambda **kw: [
                SimpleNamespace(
                    handle=HANDLES[c.role],
                    uuid=c.uuid,
                    permissions=api.tester_characteristic_native_permissions(
                        c.permissions
                    ),
                )
                for c in self.fixture._profile.characteristics
                if c.uuid == kw["attribute_uuid"]
            ]
        )
        self.fixture._start_advertising = lambda: self.fixture._gap.start_advertising(
            {}
        )
        api.write_document(
            self.root / api.CONTROL_NAME,
            {
                "schema_version": 1,
                "run_id": self.plan.session_id,
                "cycles": 10,
            },
        )
        self.writer = api.write_document

    @property
    def base(self) -> int:
        return 10000 + self.cycle * 100

    @property
    def old_peer(self) -> bytes:
        return b"\x01" + (100 + self.cycle * 2).to_bytes(6, "little")

    @property
    def new_peer(self) -> bytes:
        if self.fault == "same-address":
            return self.old_peer
        return b"\x01" + (101 + self.cycle * 2).to_bytes(6, "little")

    def identity(self) -> dict[str, object]:
        return {
            "run_id": self.plan.session_id,
            "pid": 42,
            "boot_id": BOOT,
            "bundle_id": next(
                bundle
                for bundle, platform in api.BUNDLES.items()
                if platform == self.platform
            ),
            "executable": "/synthetic-only/BleHubNitroHost",
            "source": "synthetic-native-callback",
        }

    def append(self, *markers: str) -> None:
        self.lines.extend(api.PREFIX + marker for marker in markers)
        temporary = self.root / "runtime.tmp"
        temporary.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        temporary.replace(self.root / "runtime.log")

    def append_cycle(self, name: str, fields: str) -> None:
        self.append(f"cycle:{self.cycle}:{name}:passed:{fields}")

    def resource(self, phase: str) -> None:
        # Explicitly simulated snapshots, NOT an implementation of native factories.
        counts = {key: 0 for key in api.RESOURCE_FIELDS}
        if phase != "closed":
            counts.update(
                {
                    key: 1
                    for key in (
                        "hubRoots",
                        "handoffRoots",
                        "ingressRoots",
                        "backendLeases",
                        "adapterDelegates",
                        "listeners",
                    )
                }
            )
            counts.update(
                workerThreads=2 if self.platform == "android" else 0,
                lifecycleOwners=1 if self.platform == "android" else 0,
            )
            if phase == "active":
                counts.update(
                    connections=1,
                    subscriptions=1,
                    gattAttributes=12,
                    callbackRoutes=3,
                    peerCacheEntries=1,
                )
            elif self.platform == "ios":
                counts.update(gattAttributes=12, callbackRoutes=3, peerCacheEntries=1)
        if phase == "closed" and self.fault == "closed-nonzero":
            counts["callbackRoutes"] = 1
        if phase == "active" and self.fault == "active-zero":
            counts["gattAttributes"] = 0
        self.append(
            f"resources:cycle={self.cycle}:phase={phase}:snapshot={json.dumps(counts)}"
        )

    def begin(self) -> None:
        b = self.base
        if self.cycle == 0:
            self.append(
                "native:bootstrap:" + json.dumps(self.identity()),
                f"runtime:runId={self.plan.session_id}:nonce=synthetic-one-runtime:phase=start",
            )
            if self.platform == "android":
                self.append(api.SCAN_BUDGET_POLICY)
        self.append(
            f"cycle:{self.cycle}:start", "listener:registered-before-initialize"
        )
        self.append_cycle("initialize", f"commandId={b}")

        self.resource("baseline")
        if self.platform == "android":
            now = self.cycle * 40000
            dispatch = now + (30000 if self.cycle == 0 else 0)
            self.append(
                f"cycle:{self.cycle}:scan-budget-wait:waitMs={30000 if self.cycle == 0 else 0}:monotonicMs={now}:policy=android-conservative-test-only-v1:quietMs=30000:minStartIntervalMs=7000",
                f"cycle:{self.cycle}:scan-budget-dispatch:monotonicMs={dispatch}",
                f"cycle:{self.cycle}:scan-budget-started:monotonicMs={dispatch}",
            )
        self.append_cycle(
            "scan",
            f"scanSessionId={b + 1}:startCommandId={b + 2}:stopCommandId={b + 3}:peripheralId=88:serviceUuid={self.plan.service_uuid}:silenceMs=2000",
        )
        self.append_cycle(
            "connected", f"commandId={b + 5}:connectionId={b + 4}:peripheralId=88"
        )
        self.append_cycle(
            "gatt",
            f"commandId={b + 7}:connectionId={b + 4}:gattSessionId={b + 6}:serviceCount=1:characteristicCount=5:descriptorCount=2",
        )
        self.append_cycle(
            "notification-enable",
            f"commandId={b + 9}:connectionId={b + 4}:gattSessionId={b + 6}:subscriptionId={b + 8}:characteristicId=98765:actualMode=Notification",
        )
        self.resource("active")
        if self.fault != "no-peer":
            fault = {
                "malformed-connect": "malformed",
                "duplicate-connect": "duplicate",
            }.get(self.fault, "")
            self.wire.gap(True, self.old_peer, fault)
        if self.fault in ("abort", "cleanup-failure"):
            (self.root / "abort").write_text("cancel\n", encoding="ascii")
        if self.fault == "control-change":
            (self.root / api.CONTROL_NAME).write_text("{}\n", encoding="ascii")
        if self.fault == "fake-cycle-passed":
            self.append(f"cycle:{self.cycle}:passed")

    def passive(self) -> None:
        b = self.base
        failed = self.platform == "android" or self.fault == "apple-passive-error"
        self.append(
            "event:"
            + json.dumps(
                {
                    "kind": "connectionStateChanged",
                    "connectionId": str(b + 4),
                    "runId": self.plan.session_id,
                    "nonce": "synthetic-one-runtime",
                    "cycle": self.cycle,
                    **(
                        {
                            "error": {
                                "category": "connection",
                                "message": "CoreBluetooth connection ended unexpectedly"
                                if self.platform == "ios"
                                else "physical device loss",
                                "platformDetails": {
                                    "platform": "platform_apple"
                                    if self.platform == "ios"
                                    else "platform_android",
                                    "code": "7",
                                    "name": "CBErrorDomain"
                                    if self.platform == "ios"
                                    else "BluetoothGatt",
                                    "message": "device disconnected",
                                },
                            }
                        }
                        if failed
                        else {"connectionState": "Disconnected"}
                    ),
                }
            )
        )
        self.append_cycle(
            "passive-disconnect",
            f"commandId=none:connectionId={b + 4}:error={'connection' if failed else 'none'}",
        )
        self.append_cycle(
            "old-route-silence",
            f"connectionId={b + 4}:gattSessionId={b + 6}:subscriptionId={b + 8}:observationMs=1000",
        )
        self.resource("passive")
        self.append(
            f"cycle:{self.cycle}:recovery-awaiting:runId={self.plan.session_id}"
        )

    def ready(self) -> None:
        if self.fault == "missing-native-ack":
            return
        b = self.base
        signal = {
            **self.identity(),
            "cycle": self.cycle,
            "url": f"blehub-rn-spike://recover/{self.plan.session_id}/{self.cycle}",
        }
        if self.fault == "foreign-native":
            signal["boot_id"] = self.plan.session_id
        self.append(
            "native:signal:" + json.dumps(signal),
            f"cycle:{self.cycle}:recovery-ready:runId={self.plan.session_id}:source=linking",
        )
        self.wire.gap(True, self.new_peer)
        self.append_cycle(
            "recovery-connected",
            f"commandId={b + 11}:connectionId={b + 10}:peripheralId=88",
        )
        self.append_cycle(
            "recovery-gatt",
            f"commandId={b + 13}:connectionId={b + 10}:gattSessionId={b + 14}:serviceCount=1:characteristicCount=5:descriptorCount=2",
        )
        self.wire.remote_reads.append((self.cycle, HANDLES["read-write"], b"\x00"))
        self.append_cycle(
            "recovery-read",
            f"commandId={b + 15}:connectionId={b + 10}:gattSessionId={b + 14}:characteristicId=98765:value=00",
        )
        if self.fault == "fast-logical-disconnect":
            self.wire.gap(False, self.new_peer)

    def closed(self) -> None:
        b = self.base
        self.append_cycle(
            "recovery-disconnected",
            f"commandId={b + 12}:connectionId={b + 10}:observationMs=1000",
        )
        self.resource("recovery-disconnected")
        self.resource("closed")
        self.append_cycle(
            "cycle",
            f"scanSessionId={b + 1}:connectionId={b + 4}:recoveryConnectionId={b + 10}",
        )
        self.append(f"cycle:{self.cycle}:passed")
        if self.cycle == 9:
            self.append("close:passed:listener-removed=true:probe-closed=true")
            if self.fault != "missing-runtime-close":
                self.append(
                    f"runtime:runId={self.plan.session_id}:nonce=synthetic-one-runtime:phase=close",
                    f"passed:cycles=10:runId={self.plan.session_id}",
                )

    def progress(self, path: Path, value: dict[str, object]) -> None:
        self.writer(path, value)
        self.phases.append(value)
        self.phase = str(value["phase"])
        self.cycle = max(0, int(value["cycle"]))
        if self.phase == "subscription-enabled":
            self.begin()
        elif self.phase == "recovery-ready":
            self.ready()
        elif self.phase == "recovery-read" and self.fault != "fast-logical-disconnect":
            self.wire.gap(False, self.new_peer)
        # Two publications use this phase; only the wait publication triggers JS.
        elif self.phase == "cycle-closed" and not any(
            line.endswith(f"cycle:{self.cycle}:passed") for line in self.lines
        ):
            self.closed()
        if self.phase == "notification-observed" and self.fault == "rewind":
            (self.root / "runtime.log").write_text("", encoding="utf-8")

    def retain_watchdog(self) -> fault.Ticket:
        """Synthetic previous watchdog generation; never ARM it on the test wire."""
        self.retained_ticket = fault.Ticket(
            bytes(range(1, 17)),
            fault.owner_nonce(self.plan.session_id),
            8,
            2,
            2,
            1,
            fault.Operation.READ,
            HANDLES["read-write"],
        )
        self.wire.retained_status = read_receipt(
            self.retained_ticket,
            fault.Phase.RETIRED,
            fault.Winner.DISCONNECT,
        )
        return self.retained_ticket

    def run(self) -> dict[str, object]:
        with (
            patch.object(api.time, "monotonic", self.clock.monotonic),
            patch.object(api.time, "sleep", self.clock.sleep),
            patch.object(api, "write_document", self.progress),
        ):
            if self.retained_ticket is None:
                api.run(self.fixture, self.root, HANDLES)
            else:
                api.RecoveryTen(
                    self.fixture,
                    self.root,
                    HANDLES,
                    retained_ticket=self.retained_ticket,
                ).run()
        return api.read_document(self.root / api.PHYSICAL_NAME)
