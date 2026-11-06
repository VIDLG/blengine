"""Synthetic protocol/BTP tests only; never open a port, toggle a radio or claim RF."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840.support.recovery_emulator import Clock
from tests.peripherals.nrf52840.support.session_receipts import fake_attempt
from tests.support.rf_lab.markers import IO_MARKERS, SERVICE

from . import adapter_transition as api
from . import direct_fixture, fixture_session, provider
from .btp_protocol import BtpFrame
from .btp_session import BtpSession
from .cancellation_control import write_document
from .fault_control import Opcode, Phase, RefCounters, decode_status, encode_status
from .support.read_fault_emulator import Wire as FaultWire

ARM, RELEASE, ABORT, STATUS = (
    Opcode.ARM_READ,
    Opcode.RESOLVE,
    Opcode.ABORT,
    Opcode.STATUS,
)
from .gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
    GAP_DISCONNECT,
    GAP_START_ADVERTISING,
    GAP_STOP_ADVERTISING,
    GapCommands,
)
from .gatt_commands import (
    GATT_GET_ATTRIBUTE_VALUE,
    GATT_RESET_SERVER,
    GATT_SET_VALUE,
    GattCommands,
)
from .profile import load_profile

PEER = bytes.fromhex("01665544332211")


def protocol(run: str, local_name: str) -> list[str]:
    identity = {
        "run_id": run,
        "pid": 42,
        "boot_id": "22222222-2222-4222-8222-222222222222",
        "bundle_id": "com.blehubnitrohost",
        "executable": "file:///synthetic.apk",
        "source": "launch",
    }
    resources = json.dumps(dict.fromkeys(api.RESOURCE_FIELDS, 0))
    events = (
        {
            "kind": "adapterSnapshotChanged",
            "commandId": "31000",
            "adapterState": "PoweredOff",
            "scanAuthorization": "Authorized",
        },
        {"kind": "commandCompleted", "commandId": "31000"},
        {
            "kind": "commandCompleted",
            "commandId": "31002",
            "error": {"category": "adapter_unavailable", "message": "off"},
        },
    )
    flow = [
        f"rf:start:serviceUuid={SERVICE}:expectedLocalName={local_name}:initCommandId=9001:startCommandId=9002:stopCommandId=9003:scanSessionId=9004",
        *IO_MARKERS[:-2],
        "rf:listener:removed",
        "rf:close:resolved",
        *IO_MARKERS[-2:],
    ]
    return [
        "native:bootstrap:" + json.dumps(identity),
        f"runtime:runId={run}:nonce=synthetic-boot:phase=start",
        *("off:event:" + json.dumps(event) for event in events),
        "off:resources:" + resources,
        "off:passed",
        f"awaiting-on:runId={run}",
        "native:signal:"
        + json.dumps(
            {
                **identity,
                "source": "payload-url",
                "url": f"blehub-rn-spike://adapter/{run}/on",
            }
        ),
        f"on-signal:runId={run}:source=linking",
        "on:started",
        *("on:flow:" + line for line in flow),
        "on:resources:" + resources,
        "on:passed",
        f"runtime:runId={run}:nonce=synthetic-boot:phase=close",
        f"passed:runId={run}",
    ]


class Wire(FaultWire):
    """One synthetic v2 firmware owner, plus existing GAP lifecycle controls."""

    def __init__(self, h: Harness) -> None:
        super().__init__(h.clock, h.fault)
        self.h = h

    def gap(self, connected: bool, peer: bytes = PEER, *, index: int = 0) -> None:
        super().gap(
            GAP_DEVICE_CONNECTED if connected else GAP_DEVICE_DISCONNECTED, peer
        )
        frame = self.frames.pop()
        self.frames.append(BtpFrame(frame.service, frame.opcode, index, frame.payload))

    def send(self, frame: BtpFrame) -> None:
        h = self.h
        if frame.service == 255:
            before_ready = h.owner.facts.stage == 7 and h.owner.readiness is None
            if h.fault == "ready-early-gap" and before_ready:
                self.gap(True)
            if h.fault == "foreign-hold" or (
                h.fault == "ready-foreign-hold" and before_ready
            ):
                self.phase = Phase.HELD
            if (
                frame.opcode == STATUS
                and self.phase is Phase.ARMED
                and getattr(h, "entered", False)
                and h.fault != "never-waiting"
            ):
                self.capture_read()
            super().send(frame)
            if frame.opcode == STATUS and h.fault in (
                "hold-drift",
                "retirement-pinned",
            ):
                response = self.frames.pop()
                status = decode_status(response.payload)
                if h.fault == "hold-drift" and getattr(h, "entered", False):
                    status = replace(
                        status,
                        ticket=replace(
                            status.ticket, sequence=status.ticket.sequence + 1
                        ),
                    )
                if h.fault == "retirement-pinned" and status.phase is Phase.RETIRED:
                    status = replace(status, refs=RefCounters(0, 0, 0, 0, 0, 1))
                self.frames.append(BtpFrame(255, STATUS, 255, encode_status(status)))
            return
        self.commands.append(frame)
        payload = b""
        if frame.service == 2 and frame.opcode == GATT_GET_ATTRIBUTE_VALUE:
            payload = bytes([0, 1, 0, 0])
        elif frame.service == 2 and frame.opcode == GATT_SET_VALUE:
            pass
        elif frame.service == 1 and frame.opcode == GAP_DISCONNECT:
            if h.fixture._connected_peers != {
                (frame.payload[1:7][::-1].hex(), frame.payload[0])
            }:
                raise AssertionError("disconnecting an unowned peer")
            self.gap(False, frame.payload)
        elif frame.service == 1 and frame.opcode in (
            GAP_START_ADVERTISING,
            GAP_STOP_ADVERTISING,
        ):
            if h.fault == "cleanup-ack" and frame.opcode == GAP_STOP_ADVERTISING:
                raise RuntimeError("injected missing advertising ACK")
        else:
            raise AssertionError(f"unexpected mutation/session command: {frame}")
        if h.fault == "final-extra-peer" and frame.opcode == GAP_STOP_ADVERTISING:
            self.gap(True, bytes.fromhex("01010203040506"))
        self.frames.append(BtpFrame(frame.service, frame.opcode, frame.index, payload))

    def receive(self, timeout: float) -> BtpFrame:
        self.h.clock.now += 0.001
        if self.frames:
            return self.frames.popleft()
        self.h.clock.now += timeout
        raise TimeoutError("synthetic idle")

    def close(self) -> None:
        pass


class Harness:
    def __init__(self, root: Path, fault: str = "") -> None:
        attempt = fake_attempt(root)
        self.plan, self.root = attempt.plan, attempt.root
        for path in self.root.glob("*-control.json"):
            path.unlink()
        write_document(
            self.root / api.CONTROL_NAME,
            {"schema_version": 1, "run_id": self.plan.session_id},
        )
        self.clock, self.fault = Clock(), fault
        self.wire = Wire(self)
        self.session = BtpSession(self.wire)
        self.fixture = SimpleNamespace(
            _fixture_session=self.plan,
            _session=self.session,
            _profile=load_profile(self.plan.profile_path),
            _transport=Mock(),
            _connected_peers=set(),
            _stop_requested=False,
            _mutation_allowed=False,
            _gap=GapCommands(self.session),
            _gatt=GattCommands(self.session),
            start=self.start,
        )
        self.owner = api.AdapterTransition(self.fixture, self.root)
        self.markers = protocol(self.plan.session_id, self.fixture._profile.local_name)
        self.lines: list[str] = []
        self.next = 0
        self.go_delay: float | None = 0.0
        self.original_sample = self.owner.sample
        self.owner.sample = self.sample

    def start(self, *, before_mutations: Callable[[], None]) -> dict[str, int]:
        before_mutations()
        if self.fault == "provision-failure":
            raise RuntimeError("synthetic provisioning failure")
        self.fixture._mutation_allowed = True
        self.fixture._gap.start_advertising({})
        return {"read-write": 51}

    def publish(self, marker: str) -> None:
        self.lines.append(api.PREFIX + marker)
        temporary = self.root / "runtime.tmp"
        temporary.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        temporary.replace(self.root / "runtime.log")

    def sample(self, deadline: float) -> None:
        if self.owner.whole_deadline is None:
            started = self.owner.preparing_deadline - api.PREPARING_SECONDS
            if (
                self.go_delay is not None
                and self.clock.now >= started + self.go_delay
                and not (self.root / api.GO_NAME).exists()
            ):
                write_document(
                    self.root / api.GO_NAME,
                    {
                        "schema_version": 1,
                        "run_id": self.plan.session_id,
                        **(
                            {"adapter_mode": self.owner.adapter_mode}
                            if "adapter_mode" in json.loads(self.owner.control_bytes)
                            else {}
                        ),
                    },
                )
            self.original_sample(deadline)
            return
        if self.next < len(self.markers):
            marker = self.markers[self.next]
            self.next += 1
            self.publish(marker)
            if marker == "off:passed" and self.fault == "early":
                self.wire.gap(True)
            if marker == "on:started" and self.fault == "before-started-observed":
                self.wire.gap(True)
            if (
                marker.startswith("awaiting-on:")
                and self.fault == "signal-before-ready"
            ):
                self.publish(self.markers[self.next])
                self.next += 1
            if marker.startswith("native:signal:"):
                assert (self.root / api.READY_NAME).is_file()
                assert self.owner.allow_connection and self.owner.facts.on_authorized
            if (
                marker.startswith("on:flow:rf:start:")
                and self.fault != "before-started-observed"
            ):
                self.wire.gap(True, index=1 if self.fault == "malformed" else 0)
                if self.fault == "duplicate-connect":
                    self.wire.gap(True)
            if marker.startswith("on:flow:rf:step:disconnected:"):
                if self.fault == "foreign-disconnect":
                    self.wire.gap(False, bytes.fromhex("01010203040506"))
                elif self.fault != "missing-disconnect":
                    self.wire.gap(False)
                if self.fault == "duplicate-disconnect":
                    self.wire.gap(False)
                if self.fault == "reconnect":
                    self.wire.gap(True)
            if marker.startswith("on:flow:rf:step:gatt-discovery:"):
                if self.fault == "runtime-failure":
                    self.publish("failed:injected GATT failure")
                elif self.fault == "lost-lease":
                    self.fixture._transport.require.side_effect = RuntimeError(
                        "lease lost"
                    )
                elif self.fault == "foreign-hold-late":
                    self.fault = "foreign-hold"
                elif self.fault == "abort":
                    (self.root / "abort").touch()
                elif self.fault == "receipt-drift":
                    receipt = json.loads(self.plan.receipt_path.read_bytes())
                    receipt["fixture_pgid"] += 1
                    self.plan.receipt_path.write_text(
                        json.dumps(receipt), encoding="utf-8"
                    )
                elif self.fault == "go-drift":
                    write_document(
                        self.root / api.GO_NAME,
                        {"schema_version": 1, "run_id": "foreign"},
                    )
                elif self.fault == "go-replaced":
                    write_document(
                        self.root / api.GO_NAME,
                        {"schema_version": 1, "run_id": self.plan.session_id},
                    )
                elif self.fault == "ready-drift":
                    write_document(self.root / api.READY_NAME, {"status": "foreign"})
                elif self.fault == "control-drift":
                    write_document(
                        self.root / api.CONTROL_NAME,
                        {"schema_version": 1, "run_id": "foreign"},
                    )
        self.original_sample(deadline)

    def run(self) -> None:
        with (
            patch.object(api.time, "monotonic", self.clock.monotonic),
            patch.object(api.time, "sleep", self.clock.sleep),
        ):
            self.owner.run()

    def evidence(self) -> dict:
        return json.loads((self.root / api.PHYSICAL_NAME).read_bytes())


class ActiveHarness(Harness):
    """Extend the existing provider emulator with the owned hold, not another runner."""

    def __init__(
        self, root: Path, fault: str = "", policy: str = "disconnect-first"
    ) -> None:
        super().__init__(root, fault)
        from tools.tests.react_native.support.adapter_transition_samples import (
            active_bodies,
        )

        from .gatt_commands import (
            GattAttribute,
            tester_characteristic_native_permissions,
        )

        self.active_mode, self.entered = True, False
        write_document(
            self.root / api.CONTROL_NAME,
            {
                "schema_version": 1,
                "run_id": self.plan.session_id,
                "adapter_mode": "active-loss",
            },
        )
        self.owner = api.AdapterTransition(self.fixture, self.root)
        rw = next(
            c for c in self.fixture._profile.characteristics if c.role == "read-write"
        )
        self.fixture._gatt.get_attributes = Mock(
            return_value=(
                GattAttribute(
                    51,
                    tester_characteristic_native_permissions(rw.permissions),
                    rw.uuid,
                ),
            )
        )
        self.fixture._start_advertising = lambda: self.fixture._gap.start_advertising(
            {}
        )
        self.markers = active_bodies(
            policy, run=self.plan.session_id, name=self.fixture._profile.local_name
        )
        self.original_sample = self.owner.sample
        self.owner.sample = self.sample

    def start(self, *, before_mutations: Callable[[], None]) -> dict[str, int]:
        super().start(before_mutations=before_mutations)
        return {
            c.role: 51 + i for i, c in enumerate(self.fixture._profile.characteristics)
        }

    def sample(self, deadline: float) -> None:
        if self.owner.whole_deadline is None:
            write_document(
                self.root / api.GO_NAME,
                {
                    "schema_version": 1,
                    "run_id": self.plan.session_id,
                    "adapter_mode": "active-loss",
                },
            )
            self.original_sample(deadline)
            return
        if self.next >= len(self.markers):
            self.original_sample(deadline)
            return
        marker = self.markers[self.next]
        if marker.startswith("native:arm:") and not (self.root / api.ARM_NAME).exists():
            self.original_sample(deadline)
            return
        is_loss = marker.startswith(
            'active:event:{"kind": "commandCompleted", "commandId": "32008"'
        )
        if is_loss and not (self.root / api.OFF_READY).exists():
            if (
                (self.root / api.LOSS_NAME).exists()
                and not (self.root / api.OFF_REQUEST).exists()
                and self.fault != "no-off-request"
            ):
                write_document(
                    self.root / api.OFF_REQUEST,
                    {
                        "schema_version": 1,
                        "run_id": self.plan.session_id,
                        "adapter_mode": "active-loss",
                        "loss_ready_sha256": hashlib.sha256(
                            (self.root / api.LOSS_NAME).read_bytes()
                        ).hexdigest(),
                    },
                )
            self.original_sample(deadline)
            return
        if is_loss:
            if self.fault != "missing-real-disconnect":
                self.wire.gap(False)
            write_document(
                self.root / api.OFF_CONFIRMED,
                {
                    "schema_version": 1,
                    "run_id": self.plan.session_id,
                    "adapter_mode": "active-loss",
                    "off_ready_sha256": hashlib.sha256(
                        (self.root / api.OFF_READY).read_bytes()
                    ).hexdigest(),
                },
            )
            if self.fault == "watchdog-first":
                marker = marker.replace('"connection"', '"timeout"')
        if (
            marker.startswith('active:event:{"kind": "connectionStateChanged"')
            and '"Connecting"' in marker
        ):
            self.wire.gap(True)
        if marker == "active:accepted:32008":
            self.entered = True
        if marker.startswith("on:flow:rf:start:"):
            self.wire.gap(True)
        if marker.startswith("on:flow:rf:step:disconnected:"):
            self.wire.gap(False)
        self.next += 1
        self.publish(marker)
        self.original_sample(deadline)


class ActiveLossProviderTests(unittest.TestCase):
    def test_native_error_message_is_opaque_not_a_synthetic_failure_marker(
        self,
    ) -> None:
        from tools.tests.react_native.support.adapter_transition_samples import (
            RUN,
            active_bodies,
        )

        facts = api.RuntimeFacts(RUN, SERVICE, "BleHubNrf52840", mode="active-loss")
        facts.active.loss_authorized = True
        for marker in active_bodies():
            facts.observe(
                api.PREFIX
                + marker.replace(
                    "synthetic native error", "native:failed:original details"
                )
            )
        self.assertTrue(facts.passed)

    def test_real_coordinator_two_pairs_owned_hold_and_policy(self) -> None:
        from tools.react_native import adapter_transition_ledger as ledger

        for policy in ("disconnect-first", "read-error-first"):
            with (
                self.subTest(policy=policy),
                tempfile.TemporaryDirectory() as directory,
            ):
                h = ActiveHarness(Path(directory), policy=policy)
                h.run()
                physical = h.evidence()
                self.assertEqual(physical["status"], "passed")
                self.assertEqual(
                    (physical["connections"], physical["disconnections"]), (2, 2)
                )
                commands = [f.opcode for f in h.wire.commands if f.service == 255]
                self.assertEqual(commands.count(ARM), 1)
                self.assertEqual(commands.count(RELEASE), 0)
                self.assertNotIn(ABORT, commands)
                runtime = ledger.verify_runtime(
                    "\n".join(h.lines) + "\n",
                    h.plan.session_id,
                    SERVICE,
                    Path("runtime.log"),
                )
                ledger.verify_physical(
                    physical,
                    request=json.loads(h.plan.request_path.read_bytes()),
                    receipt=json.loads(h.plan.receipt_path.read_bytes()),
                    receipt_sha256=physical["receipt_sha256"],
                    runtime=runtime,
                )

    def test_unsafe_pending_watchdog_and_release_fail_closed(self) -> None:
        for fault in (
            "never-waiting",
            "hold-drift",
            "no-off-request",
            "watchdog-first",
            "retirement-pinned",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                h = ActiveHarness(Path(directory), fault=fault)
                with self.assertRaises((RuntimeError, ValueError, TimeoutError)):
                    h.run()
                physical = h.evidence()
                self.assertEqual(physical["status"], "failed")
                self.assertIsNotNone(physical["primary_error"])
                if fault in ("never-waiting", "hold-drift", "no-off-request"):
                    self.assertFalse((h.root / api.OFF_READY).exists())
                self.assertFalse(h.fixture._mutation_allowed)


class AdapterTransitionTests(unittest.TestCase):
    def test_explicit_startup_control_bootstrap_and_ready_remain_startup_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            write_document(
                h.root / api.CONTROL_NAME,
                {
                    "schema_version": 1,
                    "run_id": h.plan.session_id,
                    "adapter_mode": "startup-off",
                },
            )
            h.owner = api.AdapterTransition(h.fixture, h.root)
            h.original_sample = h.owner.sample
            h.owner.sample = h.sample
            for i, marker in enumerate(h.markers):
                if marker.startswith(("native:bootstrap:", "native:signal:")):
                    kind, raw = marker.split(":", 2)[1:]
                    value = json.loads(raw)
                    value["adapter_mode"] = "startup-off"
                    h.markers[i] = f"native:{kind}:" + json.dumps(value)
            h.run()
            result = h.evidence()
            self.assertEqual(result["adapter_mode"], "startup-off")
            self.assertEqual(result["readiness"]["adapter_mode"], "startup-off")
            self.assertEqual(result["connections"], 1)
            self.assertFalse((h.root / api.ARM_NAME).exists())

    def test_success_exact_single_session_pair_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            h.run()
            doc = h.evidence()
            self.assertEqual(
                (doc["status"], doc["connections"], doc["disconnections"]),
                ("passed", 1, 1),
            )
            self.assertEqual(
                doc["cleanup"],
                {"advertising_stopped": True, "hold_inactive": True, "peer_count": 0},
            )
            self.assertEqual(doc["cleanup_errors"], [])
            self.assertIsNone(doc["primary_error"])
            self.assertEqual(doc["run_id"], doc["session_id"])
            self.assertEqual(doc["fixture_pid"], os.getpid())
            self.assertEqual(len(doc["receipt_sha256"]), 64)
            ready = json.loads((h.root / api.READY_NAME).read_bytes())
            self.assertEqual(
                set(ready),
                {
                    "schema_version",
                    "run_id",
                    "status",
                    "receipt_sha256",
                    "native_bootstrap",
                    "monotonic",
                },
            )
            self.assertEqual(ready, doc["readiness"])
            self.assertEqual(ready["status"], "ready")
            self.assertEqual(ready["run_id"], h.plan.session_id)
            self.assertEqual(
                ready["receipt_sha256"],
                hashlib.sha256(h.plan.receipt_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(ready["native_bootstrap"], doc["native_bootstrap"])
            self.assertEqual(
                [b["boundary"] for b in doc["boundaries"]],
                ["on-window-ready", "gap-connected", "gap-disconnected"],
            )
            self.assertEqual(doc["boundaries"][0]["monotonic"], ready["monotonic"])
            self.assertLess(ready["monotonic"], doc["events"][0]["monotonic"])
            self.assertEqual(
                [e["kind"] for e in doc["events"]], ["connected", "disconnected"]
            )
            self.assertTrue(all(type(e["monotonic"]) is float for e in doc["events"]))
            self.assertIs(h.owner.fault._session, h.session)
            self.assertFalse(h.fixture._mutation_allowed)
            self.assertFalse(h.fixture._connected_peers)
            self.assertEqual(
                [c.opcode for c in h.wire.commands if c.service == 1][-1],
                GAP_STOP_ADVERTISING,
            )
            self.assertFalse(
                any(
                    c.service == 2
                    and c.opcode
                    in (ARM, ABORT, RELEASE, GATT_SET_VALUE, GATT_RESET_SERVER)
                    for c in h.wire.commands
                )
            )

    def test_ready_authorizes_connection_before_delayed_on_started_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory), "before-started-observed")
            h.run()
            doc = h.evidence()
            self.assertEqual(doc["status"], "passed")
            self.assertEqual((doc["connections"], doc["disconnections"]), (1, 1))
            self.assertLess(
                doc["readiness"]["monotonic"], doc["events"][0]["monotonic"]
            )
            self.assertLessEqual(doc["events"][0]["monotonic"], doc["on-started"])

    def test_ready_requires_inactive_hold_and_rejects_signal_in_awaiting_batch(
        self,
    ) -> None:
        for fault in ("ready-early-gap", "ready-foreign-hold", "signal-before-ready"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), fault)
                with self.assertRaises((RuntimeError, ValueError)):
                    h.run()
                self.assertFalse((h.root / api.READY_NAME).exists())
                self.assertIsNone(h.owner.readiness)
                self.assertFalse(h.owner.facts.on_authorized)
                self.assertEqual(h.evidence()["status"], "failed")
                self.assertFalse(
                    any(
                        c.service == 1 and c.opcode == GAP_DISCONNECT
                        for c in h.wire.commands
                    )
                )

    def test_long_host_preparing_does_not_consume_rf_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            h.go_delay = 320.0

            def sleep(seconds: float) -> None:
                h.clock.sleep(
                    max(seconds, 10.0) if h.owner.whole_deadline is None else seconds
                )

            with (
                patch.object(api.time, "monotonic", h.clock.monotonic),
                patch.object(api.time, "sleep", sleep),
            ):
                h.owner.run()
            self.assertEqual(h.evidence()["status"], "passed")
            prepared_at = h.owner.preparing_deadline - api.PREPARING_SECONDS
            rf_started = h.owner.whole_deadline - api.WHOLE_SECONDS
            self.assertGreaterEqual(rf_started - prepared_at, 320.0)
            self.assertLess(h.clock.now - rf_started, api.PHASE_SECONDS)

    def test_missing_or_expired_go_fails_at_preparing_budget_without_rf(self) -> None:
        for delay in (None, api.PREPARING_SECONDS + 1):
            with self.subTest(delay=delay), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory))
                h.go_delay = delay
                with (
                    patch.object(api.time, "monotonic", h.clock.monotonic),
                    patch.object(
                        api.time,
                        "sleep",
                        side_effect=lambda s, clock=h.clock: clock.sleep(max(s, 10.0)),
                    ),
                    self.assertRaisesRegex(TimeoutError, "host-preparing"),
                ):
                    h.owner.run()
                doc = h.evidence()
                self.assertEqual(
                    (doc["phase"], doc["status"]), ("host-preparing", "failed")
                )
                self.assertIsNone(h.owner.whole_deadline)
                self.assertIsNone(h.owner.go_bytes)
                self.assertEqual(h.next, 0)
                self.assertFalse((h.root / api.READY_NAME).exists())
                self.assertTrue(doc["cleanup"]["advertising_stopped"])

    def test_go_is_strict_single_use_and_immutable(self) -> None:
        invalid = (
            '{"schema_version":true,"run_id":"RUN"}',
            '{"schema_version":1,"run_id":"foreign"}',
            '{"schema_version":1,"run_id":"RUN","extra":1}',
            '{"schema_version":1,"schema_version":1,"run_id":"RUN"}',
        )
        for raw in invalid:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory))
                (h.root / api.GO_NAME).write_text(
                    raw.replace("RUN", h.plan.session_id), encoding="utf-8"
                )
                with self.assertRaises((RuntimeError, ValueError)):
                    h.run()
                self.assertIsNone(h.owner.whole_deadline)
                self.assertEqual(h.evidence()["status"], "failed")
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            h.run()
            with self.assertRaisesRegex(RuntimeError, "already accepted"):
                h.owner.accept_go()

    def test_runtime_racing_with_go_is_not_rejected_as_pre_go(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            h.owner.prepare()
            h.publish(h.markers[0])
            h.publish(h.markers[1])
            accept = h.owner.accept_go
            checks = 0

            def racing_go() -> bool:
                nonlocal checks
                checks += 1
                if checks == 1:
                    write_document(
                        h.root / api.GO_NAME,
                        {"schema_version": 1, "run_id": h.plan.session_id},
                    )
                    return False
                return accept()

            with patch.object(h.owner, "accept_go", side_effect=racing_go):
                h.original_sample(h.owner.preparing_deadline)
            self.assertIsNotNone(h.owner.whole_deadline)
            self.assertEqual(h.owner.facts.stage, 1)
            self.assertFalse(h.owner.allow_connection)

    def test_gap_and_runtime_failures_keep_failed_records_and_only_owned_cleanup(
        self,
    ) -> None:
        for fault in (
            "early",
            "ready-early-gap",
            "malformed",
            "duplicate-connect",
            "duplicate-disconnect",
            "foreign-disconnect",
            "reconnect",
            "runtime-failure",
            "missing-disconnect",
            "provision-failure",
            "cleanup-ack",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), fault)
                with self.assertRaises((RuntimeError, TimeoutError)):
                    h.run()
                doc = h.evidence()
                self.assertEqual(doc["status"], "failed")
                self.assertTrue(doc["primary_error"])
                disconnects = [
                    c
                    for c in h.wire.commands
                    if c.service == 1 and c.opcode == GAP_DISCONNECT
                ]
                self.assertLessEqual(len(disconnects), 1)
                if fault in (
                    "early",
                    "ready-early-gap",
                    "malformed",
                    "provision-failure",
                ):
                    self.assertEqual(disconnects, [])
                if fault in (
                    "early",
                    "ready-early-gap",
                    "malformed",
                    "duplicate-connect",
                    "duplicate-disconnect",
                    "foreign-disconnect",
                    "reconnect",
                ):
                    self.assertTrue(doc["rejected_events"])
                    self.assertIsNone(doc["cleanup"]["peer_count"])
                if fault == "runtime-failure":
                    self.assertEqual(len(disconnects), 1)
                    self.assertEqual(doc["cleanup"]["peer_count"], 0)
                if fault == "cleanup-ack":
                    self.assertFalse(doc["cleanup"]["advertising_stopped"])
                    self.assertTrue(doc["cleanup_errors"])

    def test_cleanup_authority_and_foreign_hold_fail_closed(self) -> None:
        for fault in ("foreign-hold", "foreign-hold-late", "lost-lease", "bad-receipt"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), fault)
                if fault == "bad-receipt":
                    doc = json.loads(h.plan.receipt_path.read_bytes())
                    doc["fixture_pid"] += 1
                    h.plan.receipt_path.write_text(json.dumps(doc), encoding="utf-8")
                with self.assertRaises((RuntimeError, ValueError)):
                    h.run()
                doc = h.evidence()
                self.assertEqual(doc["status"], "failed")
                self.assertTrue(doc["cleanup_errors"])
                mutations = [
                    c
                    for c in h.wire.commands
                    if c.service == 1
                    or c.opcode
                    in (ARM, RELEASE, ABORT, GATT_SET_VALUE, GATT_RESET_SERVER)
                ]
                if fault in ("foreign-hold", "bad-receipt"):
                    self.assertEqual(mutations, [])
                else:
                    self.assertEqual(
                        [c.opcode for c in mutations], [GAP_START_ADVERTISING]
                    )

    def test_final_drain_and_cleanup_permission_changes(self) -> None:
        for fault in (
            "final-extra-peer",
            "receipt-drift",
            "control-drift",
            "go-drift",
            "go-replaced",
            "ready-drift",
            "abort",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), fault)
                with self.assertRaises((RuntimeError, ValueError)):
                    h.run()
                doc = h.evidence()
                self.assertEqual(doc["status"], "failed")
                commands = [c.opcode for c in h.wire.commands if c.service == 1]
                if fault in (
                    "receipt-drift",
                    "control-drift",
                    "go-drift",
                    "go-replaced",
                    "ready-drift",
                ):
                    self.assertEqual(commands, [GAP_START_ADVERTISING])
                    self.assertTrue(doc["cleanup_errors"])
                if fault == "abort":
                    self.assertIn(GAP_DISCONNECT, commands)
                    self.assertTrue(doc["cleanup"]["advertising_stopped"])
                    self.assertEqual(doc["cleanup"]["peer_count"], 0)
                if fault == "final-extra-peer":
                    self.assertTrue(doc["cleanup_errors"])
                    self.assertNotIn(GAP_DISCONNECT, commands)

    def test_resource_schema_and_foreign_runtime_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            for phase in ("off", "on"):
                index = next(
                    i
                    for i, marker in enumerate(h.markers)
                    if marker.startswith(f"{phase}:resources:")
                )
                for value in (
                    {**dict.fromkeys(api.RESOURCE_FIELDS, 0), "connections": 1},
                    {**dict.fromkeys(api.RESOURCE_FIELDS, 0), "connections": False},
                    {**dict.fromkeys(api.RESOURCE_FIELDS, 0), "foreign": 0},
                    {key: 0 for key in api.RESOURCE_FIELDS if key != "connections"},
                ):
                    facts = api.RuntimeFacts(
                        h.plan.session_id, SERVICE, h.fixture._profile.local_name
                    )
                    with (
                        self.subTest(phase=phase, value=value),
                        self.assertRaises(RuntimeError),
                    ):
                        for marker in h.markers[:index]:
                            facts.observe(api.PREFIX + marker)
                        facts.observe(
                            api.PREFIX + f"{phase}:resources:" + json.dumps(value)
                        )
            for old, new in (
                (h.plan.session_id, "foreign"),
                ("nonce=synthetic-boot:phase=close", "nonce=foreign:phase=close"),
            ):
                facts = api.RuntimeFacts(
                    h.plan.session_id, SERVICE, h.fixture._profile.local_name
                )
                with self.subTest(old=old), self.assertRaises(RuntimeError):
                    for marker in h.markers:
                        facts.observe(api.PREFIX + marker.replace(old, new))

    def test_runtime_rejects_every_duplicate_missing_foreign_failure_and_bad_io(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            original = h.markers

            def parse(markers: list[str]) -> api.RuntimeFacts:
                facts = api.RuntimeFacts(
                    h.plan.session_id, SERVICE, h.fixture._profile.local_name
                )
                for marker in markers:
                    facts.observe(api.PREFIX + marker)
                if not facts.passed:
                    raise RuntimeError("incomplete runtime")
                return facts

            self.assertTrue(parse(original).passed)
            for index in range(len(original)):
                for invalid in (
                    original[:index] + original[index + 1 :],
                    original[:index] + [original[index]] + original[index:],
                ):
                    with (
                        self.subTest(index=index),
                        self.assertRaises((RuntimeError, AssertionError)),
                    ):
                        parse(invalid)
            for old, new in (
                ("PoweredOff", "PoweredOn"),
                ("31002", "31003"),
                ("adapter_unavailable", "gatt"),
                ("source=linking", "source=foreign"),
                ("payload-url", "launch"),
                ("value=ab", "value=00"),
                ("local-submission", "remote-success"),
                ('pid": 42', 'pid": 43'),
            ):
                invalid = [
                    m.replace(old, new) if not m.startswith("native:bootstrap:") else m
                    for m in original
                ]
                with (
                    self.subTest(old=old),
                    self.assertRaises((RuntimeError, AssertionError)),
                ):
                    parse(invalid)
            for marker in (
                "failed:late",
                "native:failed:late",
                "on:flow:rf:close:rejected:late",
                'off:event:{"kind":"advertisementObserved"}',
                "on:flow:rf:foreign:event",
            ):
                with self.subTest(marker=marker), self.assertRaises(RuntimeError):
                    parse([*original, marker])

    def test_safe_live_reader_prefix_rewind_partial_and_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            facts = h.owner.facts
            path = h.root / "runtime.log"
            with patch.object(
                api, "read_live_snapshot", wraps=fixture_session.read_live_snapshot
            ) as read:
                h.publish(h.markers[0])
                facts.refresh(path)
                facts.refresh(path)
                self.assertEqual(read.call_count, 2)
                h.publish(h.markers[1])
                facts.refresh(path)
            for content in ("", h.lines[0], h.lines[0] + "\n" + h.lines[0] + "\n"):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    facts.refresh(path)
            path.unlink()
            path.symlink_to(h.plan.receipt_path)
            with self.assertRaises(fixture_session.FixtureSessionError):
                facts.refresh(path)

    def test_controls_require_exact_session_multifunction_and_fresh_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            control = h.root / api.CONTROL_NAME
            for value in (
                {"schema_version": True, "run_id": h.plan.session_id},
                {"schema_version": 1, "run_id": "foreign"},
                {"schema_version": 1, "run_id": h.plan.session_id, "cycles": 1},
            ):
                write_document(control, value)
                with self.assertRaises(ValueError):
                    api.validate_control(h.root, h.plan)
            write_document(control, {"schema_version": 1, "run_id": h.plan.session_id})
            with self.assertRaises(ValueError):
                api.validate_control(h.root, None)
            for name in (api.PHYSICAL_NAME, api.READY_NAME, api.GO_NAME):
                path = h.root / name
                path.write_text("{}", encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(ValueError):
                    api.validate_control(h.root, h.plan)
                path.unlink()

    def test_provider_cli_and_preflight_reject_before_spawn_or_open(self) -> None:
        for command in ("direct-start", "direct-serve"):
            with patch(
                "sys.argv", ["provider", command, "--coordination-mode", api.MODE]
            ):
                self.assertEqual(provider.parse_args().coordination_mode, api.MODE)
        with (
            patch.object(provider.subprocess, "Popen") as spawn,
            self.assertRaises((ValueError, RuntimeError)),
        ):
            provider.direct_start(coordination_mode=api.MODE)
        spawn.assert_not_called()
        with (
            patch.object(direct_fixture, "DirectFixture") as fixture,
            self.assertRaises((ValueError, RuntimeError)),
        ):
            direct_fixture.serve(
                "not-a-device", Path("not-a-profile"), coordination_mode=api.MODE
            )
        fixture.assert_not_called()

    def test_serve_uses_one_coordinator_and_closes_resources_without_generic_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            h.fixture.stop = Mock()
            h.fixture.start = Mock(wraps=h.start)
            poll = h.session.poll_event
            resident_polls = 0
            commands_at_close: list[BtpFrame] = []

            def stop_owned(timeout: float) -> BtpFrame | None:
                nonlocal resident_polls
                if not (h.root / api.PHYSICAL_NAME).exists():
                    return poll(timeout)
                h.fixture.stop.assert_not_called()
                self.assertEqual(h.evidence()["status"], "passed")
                self.assertIs(h.fixture._session, h.session)
                self.assertEqual(h.owner.process, (os.getpid(), os.getpgid(0)))
                self.assertFalse(h.fixture._mutation_allowed)
                if resident_polls == 0:
                    commands_at_close.extend(h.wire.commands)
                self.assertEqual(h.wire.commands, commands_at_close)
                # A successful resident has no remaining preparing/RF deadline.
                h.clock.now += api.PREPARING_SECONDS + api.WHOLE_SECONDS + 1
                resident_polls += 1
                if resident_polls == 2:
                    terminate = next(
                        call.args[1]
                        for call in handlers.call_args_list
                        if call.args[0] == direct_fixture.signal.SIGTERM
                    )
                    terminate(None, None)
                return None

            with (
                patch.object(
                    direct_fixture, "DirectFixture", return_value=h.fixture
                ) as fixture,
                patch.object(direct_fixture, "require_v2_coordination"),
                patch.object(direct_fixture.signal, "signal") as handlers,
                patch.object(h.session, "poll_event", side_effect=stop_owned),
                patch.object(api, "AdapterTransition", return_value=h.owner),
                patch.object(api.time, "monotonic", h.clock.monotonic),
                patch.object(api.time, "sleep", h.clock.sleep),
                patch("builtins.print") as ready,
            ):
                direct_fixture.serve(
                    h.plan.port,
                    h.plan.profile_path,
                    h.root,
                    api.MODE,
                    fixture_session=h.plan,
                )
            self.assertEqual(resident_polls, 2)
            fixture.assert_called_once()
            h.fixture.start.assert_called_once()
            h.fixture.stop.assert_called_once()
            ready.assert_called_once_with("NRF52840_DIRECT_READY", flush=True)
            self.assertFalse(h.fixture._mutation_allowed)
            self.assertEqual(h.evidence()["status"], "passed")

    def test_read_only_resident_rejects_any_extra_gap_without_mutation(self) -> None:
        for opcode in (GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED):
            with (
                self.subTest(opcode=opcode),
                tempfile.TemporaryDirectory() as directory,
            ):
                h = Harness(Path(directory))
                h.run()
                before = list(h.wire.commands)
                payload = PEER + (bytes(6) if opcode == GAP_DEVICE_CONNECTED else b"")
                with (
                    patch.object(
                        h.session,
                        "poll_event",
                        return_value=BtpFrame(1, opcode, 0, payload),
                    ),
                    self.assertRaisesRegex(RuntimeError, "GAP"),
                ):
                    h.owner.wait_stopped()
                self.assertEqual(h.wire.commands, before)
                self.assertFalse(h.fixture._mutation_allowed)

    def test_resource_acquisition_failure_retains_failed_unknown_cleanup_record(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            with (
                patch.object(direct_fixture, "require_v2_coordination"),
                patch.object(
                    direct_fixture,
                    "DirectFixture",
                    side_effect=RuntimeError("lease conflict"),
                ),
                self.assertRaisesRegex(RuntimeError, "lease conflict"),
            ):
                direct_fixture.serve(
                    h.plan.port,
                    h.plan.profile_path,
                    h.root,
                    api.MODE,
                    fixture_session=h.plan,
                )
            doc = h.evidence()
            self.assertEqual(doc["status"], "failed")
            self.assertEqual(doc["events"], [])
            self.assertIsNone(doc["cleanup"]["peer_count"])
            self.assertFalse(doc["cleanup"]["advertising_stopped"])
            self.assertEqual(h.wire.commands, [])

    def test_direct_start_hook_is_after_services_before_profile_mutation(self) -> None:
        fixture = direct_fixture.DirectFixture.__new__(direct_fixture.DirectFixture)
        fixture._transport, fixture._session = Mock(), Mock()
        fixture._fixture_session = Mock(
            session_id="12345678-1234-4234-8234-123456789abc"
        )
        fixture._lease, fixture._port = Mock(), "synthetic"
        fixture._gap, fixture._gatt = Mock(), Mock()
        fixture._resources, fixture._stopping = Mock(), False
        fixture._mutation_allowed = False
        order: list[str] = []

        def reject() -> None:
            order.append("hook")
            raise RuntimeError("foreign hold")

        with (
            patch(
                "tests.peripherals.nrf52840.fault_control.FaultController.require_idle",
                return_value=SimpleNamespace(peer_present=False),
            ),
            patch.object(
                fixture_session,
                "attest_session",
                side_effect=lambda *a, **kw: order.append("attested"),
            ),
            patch.object(
                direct_fixture.NrfDoctor,
                "ensure_services",
                side_effect=lambda: order.append("services-ready"),
            ),
            self.assertRaisesRegex(RuntimeError, "foreign hold"),
        ):
            fixture.start(before_mutations=reject)
        self.assertEqual(order, ["attested", "services-ready", "hook"])
        self.assertEqual(fixture._gap.mock_calls, [])
        self.assertEqual(fixture._gatt.mock_calls, [])
        fixture._resources.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
