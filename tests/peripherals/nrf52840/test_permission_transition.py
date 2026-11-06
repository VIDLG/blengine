"""Synthetic facts/real-BTP dispatch tests. Never acquire hardware or change OS state."""

from __future__ import annotations

import copy
import hashlib
import json
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from . import permission_transition as api
from . import provider
from .btp_protocol import BtpFrame
from .btp_session import BtpSession
from .cancellation_control import write_document
from .fault_control import Opcode, Phase, decode_status, encode_status, ticket_document
from .support.read_fault_emulator import Wire as FaultWire
from .support.read_fault_emulator import read_receipt

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
    GATT_GET_ATTRIBUTES,
    GATT_SET_VALUE,
    GattCommands,
)
from .profile import load_profile
from .support import permission_provider_samples as samples
from .support.recovery_emulator import HANDLES, Clock
from .support.session_receipts import fake_attempt

PEER = bytes.fromhex("01665544332211")


class Wire(FaultWire):
    def __init__(self, harness: Harness) -> None:
        super().__init__(harness.clock, harness.fault)
        self.h = harness
        self.advertising = False

    def gap(self, connected: bool, peer: bytes = PEER) -> None:
        super().gap(
            GAP_DEVICE_CONNECTED if connected else GAP_DEVICE_DISCONNECTED, peer
        )

    def send(self, frame: BtpFrame) -> None:
        h, opcode = self.h, frame.opcode
        if frame.service == 255:
            if h.fault == "foreign-hold" and h.stage == 0:
                self.phase = Phase.HELD
            if (
                opcode == STATUS
                and h.stage >= 4
                and self.phase is Phase.ARMED
                and h.fault != "never-waiting"
            ):
                self.capture_read()
            super().send(frame)
            if opcode == STATUS and h.fault in ("false-stillwaiting", "hold-drift"):
                response = self.frames.pop()
                status = decode_status(response.payload)
                if h.fault == "hold-drift" and h.stage >= 4:
                    status = replace(
                        status,
                        ticket=replace(
                            status.ticket, sequence=status.ticket.sequence + 1
                        ),
                    )
                elif h.fault == "false-stillwaiting" and h.stage == 5:
                    status = read_receipt(status.ticket, Phase.ARMED)
                self.frames.append(BtpFrame(255, STATUS, 255, encode_status(status)))
            return
        self.commands.append(frame)
        payload = b""
        if frame.service == 2:
            if opcode == GATT_GET_ATTRIBUTES:
                uuid_bytes = frame.payload[5:]
                payload = (
                    b"\x01"
                    + struct.pack(
                        "<HBB", 51, 0x41 if h.fault == "readonly-native" else 0x43, 16
                    )
                    + uuid_bytes
                )
            elif opcode == GATT_GET_ATTRIBUTE_VALUE:
                payload = bytes([0, 1, 0, 0])
            elif opcode == GATT_SET_VALUE:
                baseline_reset = h.owner.baseline_closed and not h.fixture._connected_peers
                if not h.owner.facts.loss_confirmed and not baseline_reset:
                    raise AssertionError("canonical restore before death/no-peer")
            else:
                raise AssertionError(f"unexpected GATT mutation: {frame}")
        elif opcode == GAP_START_ADVERTISING:
            self.advertising = True
        elif opcode == GAP_STOP_ADVERTISING:
            if h.fault == "stop-ack" and h.owner.facts.passed:
                raise RuntimeError("missing stop advertising ACK")
            self.advertising = False
        elif opcode == GAP_DISCONNECT:
            if h.fixture._connected_peers != {
                (frame.payload[1:7][::-1].hex(), frame.payload[0])
            }:
                raise AssertionError("cleanup tried to adopt foreign peer")
            self.gap(False, frame.payload)
        else:
            raise AssertionError(f"unexpected GAP mutation: {frame}")
        self.frames.append(BtpFrame(frame.service, opcode, frame.index, payload))

    def receive(self, timeout: float) -> BtpFrame:
        self.h.clock.now += 0.001
        if self.frames:
            return self.frames.popleft()
        self.h.clock.now += timeout
        raise TimeoutError("synthetic idle")

    def close(self) -> None:
        pass


class Harness:
    def __init__(self, directory: Path, fault: str = "") -> None:
        attempt = fake_attempt(directory)
        self.root, self.plan = attempt.root, attempt.plan
        for path in self.root.glob("*-control.json"):
            path.unlink()
        write_document(
            self.root / api.CONTROL_NAME,
            {"schema_version": 1, "run_id": self.plan.session_id},
        )
        self.clock, self.fault, self.stage = Clock(), fault, 0
        self.wire = Wire(self)
        session = BtpSession(self.wire)
        self.fixture = SimpleNamespace(
            _session=session,
            _fixture_session=self.plan,
            _profile=load_profile(self.plan.profile_path),
            _transport=Mock(),
            _connected_peers=set(),
            _stop_requested=False,
            _mutation_allowed=False,
            _gap=GapCommands(session),
            _gatt=GattCommands(session),
            start=self.start,
        )
        self.fixture._start_advertising = lambda: self.fixture._gap.start_advertising(
            {}
        )
        self.owner = api.PermissionTransition(self.fixture, self.root)
        self.original_sample = self.owner.sample
        self.owner.sample = self.sample
        self.lines: list[str] = []

    def start(self, *, before_mutations: object) -> dict[str, int]:
        before_mutations()
        self.fixture._mutation_allowed = True
        self.fixture._start_advertising()
        return dict(HANDLES)

    def append(self, markers: list[str]) -> None:
        self.lines.extend(api.PREFIX + m for m in markers)
        temporary = self.root / "runtime.tmp"
        temporary.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        temporary.replace(self.root / api.RUNTIME_NAME)

    def doc(self, name: str, **fields: object) -> None:
        write_document(
            self.root / name,
            {"schema_version": 1, "run_id": self.plan.session_id, **fields},
        )

    def sample(self, deadline: float) -> None:
        run, name = self.plan.session_id, self.fixture._profile.local_name
        if self.stage == 0:
            self.append(samples.denied(run))
            if self.fault == "early-gap":
                self.wire.gap(True)
            self.stage = 1
        elif self.stage == 1 and "denied" in self.owner.facts.passes:
            self.doc(api.ACTIVE_GO_NAME, phase="active")
            self.append(samples.baseline(run, name))
            self.wire.gap(True)
            self.wire.gap(False)
            self.stage = 2
        elif self.stage == 2 and self.owner.baseline_closed:
            self.stage = 25
        elif self.stage == 25:
            self.wire.gap(True)
            if self.fault == "foreign-peer":
                self.wire.gap(True, bytes.fromhex("01010203040506"))
            self.append(samples.setup(run, name))
            self.stage = 3
        elif self.stage == 3 and (self.root / api.ARM_READY_NAME).exists():
            self.append(samples.pending(run))
            if self.fault == "early-read-terminal":
                self.append(
                    ['active:event:{"kind":"commandCompleted","commandId":"52008"}']
                )
            self.stage = 4
        elif self.stage == 4 and (self.root / api.REVOKE_READY_NAME).exists():
            ready = (self.root / api.REVOKE_READY_NAME).read_bytes()
            self.doc(
                api.REVOKE_REQUEST_NAME,
                route=samples.ROUTE,
                ready_sha256="00" * 32
                if self.fault == "stale-challenge"
                else hashlib.sha256(ready).hexdigest(),
                request_nonce=samples.REQUEST_NONCE,
            )
            if self.fault == "control-mutation":
                (self.root / api.CONTROL_NAME).write_text("{}", encoding="utf-8")
            self.stage = 5
        elif self.stage == 5 and (self.root / api.REVOKE_AUTHORIZED_NAME).exists():
            boot = self.owner.facts.bootstraps["active"]
            self.doc(
                api.LOSS_CONFIRMED_NAME,
                authorized_sha256=hashlib.sha256(
                    (self.root / api.REVOKE_AUTHORIZED_NAME).read_bytes()
                ).hexdigest(),
                request_nonce=samples.REQUEST_NONCE,
                pid=boot["pid"] + (1 if self.fault == "wrong-dead-pid" else 0),
                uid=boot["uid"],
                user_id=0,
                reason=9 if self.fault == "wrong-death-reason" else 8,
                exit_proof_sha256="ab" * 32,
                revoked_elapsed_ms=4001 if self.fault == "late-revoke" else 100,
            )
            if self.fault != "missing-real-disconnect":
                self.wire.gap(False)
            self.stage = 6
        elif self.stage == 6 and self.owner.restored:
            if self.fault == "callback-after-death":
                self.append(
                    ['active:event:{"kind":"characteristicRead","commandId":"52008"}']
                )
            else:
                self.append(samples.restored(run, name))
            self.wire.gap(True)
            self.wire.gap(False)
            self.stage = 7
        self.original_sample(deadline)

    def run(self) -> dict:
        with (
            patch.object(api.time, "monotonic", self.clock.monotonic),
            patch.object(api.time, "sleep", self.clock.sleep),
        ):
            self.owner.run()
        return json.loads((self.root / api.PHYSICAL_NAME).read_bytes())


class FactsTests(unittest.TestCase):
    def test_complete_three_processes_no_dead_probe_close(self) -> None:
        facts = samples.replay()
        self.assertTrue(facts.passed)
        self.assertEqual(
            set(facts.resources), {"denied", "baseline", "pending", "restored"}
        )
        self.assertEqual(
            facts.setup_facts.terminals,
            dict.fromkeys(("52000", "52002", "52003", "52004", "52006")),
        )
        self.assertEqual(facts.accepted, ["52008", "52009"])

    def test_all_missing_duplicate_and_late_records_fail(self) -> None:
        groups = [
            samples.denied(),
            samples.baseline(),
            samples.setup(),
            samples.pending(),
            samples.restored(),
        ]
        for g, group in enumerate(groups):
            for index in range(len(group)):
                for duplicate in (False, True):
                    # Repeated real advertisements are legal only in the same
                    # bounded scan window and must retain the exact peer.
                    if duplicate and '"kind":"advertisementObserved"' in group[index]:
                        continue
                    invalid = copy.deepcopy(groups)
                    invalid[g][index : index + 1] = (
                        [group[index]] * 2 if duplicate else []
                    )
                    with self.subTest(group=g, index=index, duplicate=duplicate):
                        try:
                            self.assertFalse(samples.replay(invalid).passed)
                        except RuntimeError:
                            pass
        for marker in (
            'active:event:{"kind":"commandCompleted","commandId":"52008"}',
            samples.zero("active:resources:"),
            "failed:late",
            samples.start(samples.RUN, "active")[1],
        ):
            facts = samples.replay()
            with self.subTest(marker=marker), self.assertRaises(RuntimeError):
                facts.consume(marker)
            self.assertFalse(facts.passed)

    def test_raw_lines_json_and_identity_are_strict_and_failure_sticky(self) -> None:
        facts = api.RuntimeFacts(samples.RUN, samples.SERVICE)
        for marker in samples.denied():
            facts.consume(
                "10-17 12:00:00.123 41 41 I ReactNativeJS: " + api.PREFIX + marker
            )
        self.assertIn("denied", facts.passes)
        invalid = [
            "denied:event:{",
            'denied:event:{"kind":"commandCompleted","kind":"commandCompleted"}',
            api.PREFIX + api.PREFIX + "denied:passed",
        ]
        for marker in invalid:
            f = api.RuntimeFacts(samples.RUN, samples.SERVICE)
            for m in samples.denied()[:2]:
                f.consume(m)
            with self.assertRaises(RuntimeError):
                f.consume(marker)
            with self.assertRaises(RuntimeError):
                f.consume(samples.denied()[2])
        for field, value in (
            ("pid", True),
            ("user_id", 1),
            ("uid", 999),
            ("started_elapsed_ms", -1),
            ("boot_id", "bad"),
        ):
            boot = samples.bootstrap(samples.RUN, "denied")
            boot[field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                api.RuntimeFacts(samples.RUN, samples.SERVICE).consume(
                    "native:bootstrap:" + json.dumps(boot)
                )

    def test_wrong_routes_properties_domains_and_pending_counts(self) -> None:
        groups = [
            samples.denied(),
            samples.baseline(),
            samples.setup(),
            samples.pending(),
            samples.restored(),
        ]
        mutations = (
            (0, '"permission"', '"gatt"'),
            (1, "value=ab", "value=00"),
            (2, "52005", "52099"),
            (2, '"writableWithResponse":true', '"writableWithResponse":false'),
            (2, '"descriptorCount":2', '"descriptorCount":1'),
            (2, samples.NAME, "foreign"),
            (3, "queuedCommandId=52009", "queuedCommandId=52008"),
            (3, '"pendingIo": 2', '"pendingIo": 1'),
            (3, '"watchdogs": 1', '"watchdogs": 0'),
            (3, '"source": "payload-url"', '"source": "launch"'),
            (4, "value=00", "value=10"),
        )
        for group, old, new in mutations:
            invalid = copy.deepcopy(groups)
            invalid[group] = [m.replace(old, new) for m in invalid[group]]
            with self.subTest(old=old), self.assertRaises(RuntimeError):
                samples.replay(invalid)

    def test_cumulative_reader_bounds_and_native_capture_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / api.RUNTIME_NAME
            facts = api.RuntimeFacts(samples.RUN, samples.SERVICE)
            path.write_text(api.PREFIX + samples.denied()[0] + "\n", encoding="utf-8")
            facts.refresh(path)
            path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "rewound"):
                facts.refresh(path)
            with self.assertRaisesRegex(RuntimeError, "already failed"):
                facts.consume(samples.denied()[1])
        for marker in (
            "10-17 12:00:00.123 999 999 I Native: " + api.PREFIX + samples.denied()[0],
            "x" * (api.MAX_LINE_BYTES + 1),
        ):
            with self.subTest(marker=marker[:100]), self.assertRaises(RuntimeError):
                api.RuntimeFacts(samples.RUN, samples.SERVICE).consume(marker)
        facts = api.RuntimeFacts(samples.RUN, samples.SERVICE)
        facts.count = api.MAX_MARKERS
        with self.assertRaisesRegex(RuntimeError, "overflow"):
            facts.consume(samples.denied()[0])

    def test_shared_topology_only_never_adapter_death_terminal_policy(self) -> None:
        with (
            patch.object(
                api.ActiveReadFacts,
                "event",
                side_effect=AssertionError("adapter policy used"),
            ),
            patch.object(
                api.ActiveReadFacts,
                "observe",
                side_effect=AssertionError("adapter policy used"),
            ),
        ):
            self.assertTrue(samples.replay().passed)
        for incoming in ("native:signal:", "native:arm:"):
            groups = [
                samples.denied(),
                samples.baseline(),
                samples.setup(),
                samples.pending(),
                samples.restored(),
            ]
            groups[3][0] = groups[3][0].replace("native:signal:", incoming)
            self.assertTrue(samples.replay(groups).passed)
        groups[1] = [
            m.replace("active:baseline:resources:", "active:baseline-resources:")
            for m in groups[1]
        ]
        self.assertTrue(samples.replay(groups).passed)


class ProviderTests(unittest.TestCase):
    def test_success_has_three_real_pairs_and_only_alive_zero_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            doc = h.run()
            self.assertEqual(doc["status"], "passed")
            self.assertEqual((doc["connections"], doc["disconnections"]), (3, 3))
            self.assertEqual(doc["aliveProbes"], ["denied", "baseline", "restored"])
            self.assertEqual(
                doc["cleanup"],
                {"advertising_stopped": True, "hold_inactive": True, "peer_count": 0},
            )
            self.assertEqual(
                [
                    c.opcode
                    for c in h.wire.commands
                    if c.service == 255 and c.opcode in (ARM, RELEASE, ABORT)
                ],
                [ARM],
            )
            self.assertFalse(
                any(
                    c.service == 1 and c.opcode == GAP_DISCONNECT
                    for c in h.wire.commands
                )
            )
            self.assertIs(h.owner.fault._session, h.fixture._session)
            self.assertFalse(h.fixture._mutation_allowed)
            auth = doc["protocol"][api.REVOKE_AUTHORIZED_NAME]
            self.assertEqual(auth["route"], samples.ROUTE)
            self.assertEqual(auth["hold"], ticket_document(h.owner.owned_hold))
            self.assertEqual(
                auth["ready_sha256"],
                hashlib.sha256(
                    (h.root / api.REVOKE_READY_NAME).read_bytes()
                ).hexdigest(),
            )

    def test_failed_boundaries_never_become_success_after_cleanup(self) -> None:
        for fault in (
            "foreign-hold",
            "early-gap",
            "foreign-peer",
            "readonly-native",
            "never-waiting",
            "hold-drift",
            "early-read-terminal",
            "false-stillwaiting",
            "stale-challenge",
            "control-mutation",
            "wrong-dead-pid",
            "wrong-death-reason",
            "late-revoke",
            "missing-real-disconnect",
            "callback-after-death",
            "stop-ack",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), fault)
                with self.assertRaises((RuntimeError, ValueError, TimeoutError)):
                    h.run()
                doc = json.loads((h.root / api.PHYSICAL_NAME).read_bytes())
                self.assertEqual(doc["status"], "failed")
                self.assertTrue(doc["primary_error"])
                if fault in (
                    "false-stillwaiting",
                    "stale-challenge",
                    "control-mutation",
                ):
                    self.assertFalse((h.root / api.REVOKE_AUTHORIZED_NAME).exists())
                if fault in ("wrong-dead-pid", "wrong-death-reason", "late-revoke"):
                    self.assertFalse(
                        any(
                            c.service == 2 and c.opcode == RELEASE
                            for c in h.wire.commands
                        )
                    )

    def test_preflight_and_immutable_handshake_reject_reuse(self) -> None:
        for filename in (api.PHYSICAL_NAME, api.RUNTIME_NAME, api.REVOKE_REQUEST_NAME):
            with (
                self.subTest(filename=filename),
                tempfile.TemporaryDirectory() as directory,
            ):
                h = Harness(Path(directory))
                (h.root / filename).write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    api.validate_control(h.root, h.plan)
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            h.run()
            (h.root / api.REVOKE_REQUEST_NAME).write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "immutable permission handshake"):
                h.owner.authority()

    def test_mode_wiring_validates_before_any_process_or_port_operation(self) -> None:
        for command in ("direct-start", "direct-serve"):
            with patch.object(
                provider.sys,
                "argv",
                ["provider", command, "--coordination-mode", api.MODE],
            ):
                self.assertEqual(provider.parse_args().coordination_mode, api.MODE)
        with (
            patch.object(provider, "_port") as port,
            patch.object(provider, "_process_identity") as identity,
        ):
            with self.assertRaisesRegex(
                provider.Nrf52840Error, "immutable --fixture-request required"
            ):
                provider.direct_start(coordination_mode=api.MODE)
            port.assert_not_called()
            identity.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            with (
                patch.object(provider, "_port", return_value=h.plan.port),
                patch.object(api, "serve") as serve,
            ):
                provider.direct_serve(str(h.root), api.MODE, h.plan.request_path)
                serve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
