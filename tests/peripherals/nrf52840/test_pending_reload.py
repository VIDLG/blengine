"""Focused fake-only barriers: real BTP dispatcher/hold codec, no USB or Android."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840.support.session_receipts import fake_attempt

from . import direct_fixture, provider
from . import pending_reload as api
from .btp_protocol import BtpFrame
from .btp_session import BtpSession
from .fault_control import (
    Opcode,
    Outcome,
    cleanup_ready,
    decode_status,
    decode_ticket_command,
    read_completed,
)

ARM = Opcode.ARM_READ
RELEASE = Opcode.RESOLVE
ABORT = Opcode.ABORT
STATUS = Opcode.STATUS
from .gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED
from .profile import load_profile

PROBE = "12345678-1234-4234-8234-123456789abc"
PEER = bytes.fromhex("01665544332211")
TARGET = 51  # ATT space; deliberately not the JS characteristic ID 98765.


from .support.read_fault_emulator import Clock, Wire


class Harness:
    def __init__(
        self, root: Path, fault: str = "", *, peripheral_id: str = "88"
    ) -> None:
        attempt = fake_attempt(root)
        self.root = attempt.root
        self.plan = attempt.plan
        self.fault = fault
        self.peripheral_id = peripheral_id
        self.clock = Clock()
        self.wire = Wire(self.clock, fault)
        self.markers: list[str] = []
        self.phases: list[dict[str, object]] = []
        self.profile = load_profile(self.plan.profile_path)
        self.rw = next(
            c for c in self.profile.characteristics if c.role == "read-write"
        )
        self.wire.remote_value = self.rw.initial_value
        api.write_document(
            self.root / api.CONTROL_NAME,
            {"schema_version": 1, "run_id": self.plan.session_id},
        )
        self.fixture = SimpleNamespace(
            _fixture_session=self.plan,
            _session=BtpSession(self.wire),
            _profile=self.profile,
            _connected_peers=set(),
            _stop_requested=False,
            _transport=Mock(),
            _gatt=Mock(),
            _gap=Mock(),
            _start_advertising=Mock(),
        )
        self.fixture._gatt.set_value.side_effect = AssertionError(
            "pending read coordinator must not mutate connected GATT"
        )
        self.fixture._gatt.get_attribute_value.return_value = SimpleNamespace(
            att_response=0, value=self.rw.initial_value
        )
        self.fixture._gatt.get_attributes.return_value = [
            SimpleNamespace(handle=TARGET, uuid=self.rw.uuid, permissions=0x43)
        ]
        self.fixture._gap.disconnect.side_effect = lambda *_: self.wire.gap(
            GAP_DEVICE_DISCONNECTED
        )
        self.fixture._start_advertising.side_effect = lambda: self.boot(2)
        self.boot(1)
        self.writer = api.write_document

    @staticmethod
    def route(generation: int) -> str:
        base = 12000 if generation == 1 else 13000
        return f"commandId={base + 8}:connectionId={base + 4}:gattSessionId={base + 6}:characteristicId=98765"

    def append(self, *markers: str) -> None:
        self.markers.extend(
            api.PREFIX + marker
            for marker in markers
            if not any(
                self.fault == f"missing-{event}"
                and marker.startswith(f"native:{event}:")
                for event in api.NATIVE_EVENTS
            )
        )
        temporary = self.root / "runtime.tmp"
        temporary.write_text("\n".join(self.markers) + "\n", encoding="utf-8")
        temporary.replace(self.root / "runtime.log")

    def boot(self, generation: int) -> None:
        prefix = f"generation={generation}:"
        self.append(
            prefix
            + f"runtime-start:runId={self.plan.session_id}:nonce=engine-{generation}",
            prefix + "native-ready:passed",
            prefix + "awaiting-arm:" + self.route(generation),
        )
        if self.fault != "no-peer":
            self.wire.gap(GAP_DEVICE_CONNECTED)

    def native(self, event: str) -> str:
        probe = (
            PROBE
            if self.fault != "foreign-probe" or event == "read-accepted"
            else self.plan.session_id
        )
        result = (
            f"native:{event}:runId={self.plan.session_id}:probeId={probe}:"
            + self.route(1)
        )
        if event == "late-submit-rejected":
            result += ":source=synthetic:result=closed"
        return result

    def progress(self, path: Path, value: dict[str, object]) -> None:
        self.writer(path, value)
        if path.name != api.PROGRESS_NAME:
            return
        self.phases.append(value)
        phase = value["phase"]
        generation = int(value["generation"])
        prefix = f"generation={generation}:"
        if str(phase).startswith("armed-"):
            self.append(prefix + "arm-js-observed:source=linking:url=matched")
            dispatched = prefix + "read-dispatched:" + self.route(generation)
            if generation == 1:
                if self.fault != "missing-accepted":
                    self.append(self.native("read-accepted"))
                dispatched += f":peripheralId={self.peripheral_id}"
            self.append(dispatched)
            if self.fault != "missing-waiting":
                self.wire.capture_read()
            if self.fault == "abort":
                (self.root / "abort").touch()
            if self.fault == "control-drift":
                self.writer(
                    self.root / api.CONTROL_NAME,
                    {
                        "schema_version": 1,
                        "run_id": self.plan.session_id,
                        "changed": True,
                    },
                )
            if self.fault == "foreign-gap":
                self.wire.gap(GAP_DEVICE_DISCONNECTED, bytes.fromhex("01ffeeddccbbaa"))
            if self.fault == "malformed-gap":
                self.wire.frames.append(
                    BtpFrame(1, GAP_DEVICE_DISCONNECTED, 0, b"\x01")
                )
            if self.fault == "duplicate-gap":
                self.wire.gap(GAP_DEVICE_CONNECTED)
        elif phase == "waiting-1":
            if self.fault not in ("missing-dispose", "cleanup-failure", "arm-budget"):
                self.append(
                    self.native("engine-invalidation-close-started"),
                    self.native("dispose-returned"),
                    self.native("engine-close-complete"),
                    self.native("late-submit-rejected"),
                )
                self.wire.gap(GAP_DEVICE_DISCONNECTED)
            if self.fault == "arm-budget":
                self.clock.now += 24
        elif phase == "released-1":
            if self.fault == "duplicate-disconnect":
                self.wire.gap(GAP_DEVICE_DISCONNECTED)
        elif phase == "released-2":
            value = self.wire.remote_reads[-1][2].hex()
            if value != "00":
                self.append(prefix + f"failed:unexpected read value={value}")
                self.wire.gap(GAP_DEVICE_DISCONNECTED)
                return
            self.append(
                prefix + "read:passed:" + self.route(2) + f":value={value}",
                prefix + "close:passed",
                f"passed:runId={self.plan.session_id}",
            )
            if self.fault != "missing-final-gap":
                self.wire.gap(GAP_DEVICE_DISCONNECTED)

    def run(self) -> None:
        with (
            patch.object(api.time, "monotonic", self.clock.monotonic),
            patch.object(api.time, "sleep", self.clock.sleep),
            patch.object(api, "write_document", self.progress),
        ):
            api.run(self.fixture, self.root, {"read-write": TARGET})

    def evidence(self) -> dict[str, object]:
        return api.read_document(self.root / api.PHYSICAL_NAME)


class IosHarness(Harness):
    """Same BTP holds/session as Android, only the App routes/control differ."""

    def __init__(self, root: Path, fault: str = "") -> None:
        super().__init__(root, fault, peripheral_id="0")
        self.writer(
            self.root / api.CONTROL_NAME,
            {"schema_version": 1, "run_id": self.plan.session_id, "platform": "ios"},
        )

    @staticmethod
    def route(generation: int) -> str:
        base = 12000 if generation == 1 else 13000
        return f"commandId={base + 11}:connectionId={base + 4}:gattSessionId={base + 8}:characteristicId=98765"

    def run(self) -> None:
        with (
            patch.object(api.time, "monotonic", self.clock.monotonic),
            patch.object(api.time, "sleep", self.clock.sleep),
            patch.object(api, "write_document", self.progress),
        ):
            api.run(self.fixture, self.root, {"read-write": TARGET}, platform="ios")


class IosPendingReloadTests(unittest.TestCase):
    def test_ios_mode_same_multifunction_sequence_and_no_android_schema_relabel(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = IosHarness(Path(temporary))
            with self.assertRaises(ValueError):
                api.validate_control(h.root, h.plan)
            h.run()
            result = h.evidence()
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["platform"], "ios")
            self.assertEqual(h.wire.remote_reads, [(2, TARGET, b"\x00")])
            self.assertEqual(
                [
                    f.opcode
                    for f in h.wire.commands
                    if f.opcode in (ARM, RELEASE, ABORT)
                ],
                [ARM, ARM, RELEASE],
            )
            h.fixture._gatt.reset_server.assert_not_called()
            h.fixture._gatt.set_value.assert_not_called()
            self.assertIn(
                "commandId=12011:connectionId=12004:gattSessionId=12008",
                result["boundaries"][1]["route"],
            )
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            with self.assertRaises(ValueError):
                api.validate_control(h.root, h.plan, platform="ios")

    def test_ios_route_never_accepts_android_native_read_or_unknown_platform(
        self,
    ) -> None:
        facts = api.RuntimeFacts(PROBE, platform="ios")
        facts.observe(
            api.PREFIX + f"generation=1:runtime-start:runId={PROBE}:nonce=ios-1"
        )
        facts.observe(api.PREFIX + "generation=1:native-ready:passed")
        with self.assertRaises(RuntimeError):
            facts.observe(api.PREFIX + "generation=1:awaiting-arm:" + Harness.route(1))
        with self.assertRaises(ValueError):
            api.RuntimeFacts(PROBE, platform="other")
        for command in ("direct-start", "direct-serve"):
            with patch(
                "sys.argv", ["provider", command, "--coordination-mode", api.IOS_MODE]
            ):
                self.assertEqual(provider.parse_args().coordination_mode, api.IOS_MODE)


class PendingReloadTests(unittest.TestCase):
    def test_two_real_protocol_holds_explicit_release_and_exact_disconnects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            control_bytes = (h.root / api.CONTROL_NAME).read_bytes()
            receipt_bytes = h.plan.receipt_path.read_bytes()
            h.run()
            evidence = h.evidence()
            self.assertEqual(evidence["status"], "passed")
            self.assertEqual(evidence["run_id"], h.plan.session_id)
            self.assertEqual(evidence["cleanup_errors"], [])
            self.assertEqual(
                {
                    k: v
                    for k, v in evidence["physical_cleanup"].items()
                    if k != "status_wire"
                },
                {
                    "hold_inactive": True,
                    "hold_state": "RETIRED",
                    "no_known_peers": True,
                    "advertising_stopped": True,
                },
            )
            self.assertEqual(
                [p["phase"] for p in h.phases],
                [
                    "armed-1",
                    "waiting-1",
                    "released-1",
                    "armed-2",
                    "waiting-2",
                    "released-2",
                    "closed",
                ],
            )
            self.assertEqual([p["sequence"] for p in h.phases], [1, 1, 1, 2, 2, 2, 2])
            self.assertTrue(all(p["handle"] == TARGET for p in h.phases))
            self.assertEqual(h.fixture._connected_peers, set())
            self.assertEqual((h.root / api.CONTROL_NAME).read_bytes(), control_bytes)
            self.assertEqual(h.plan.receipt_path.read_bytes(), receipt_bytes)
            controls = [f for f in h.wire.commands if f.opcode in (ARM, RELEASE, ABORT)]
            self.assertEqual([f.opcode for f in controls], [ARM, ARM, RELEASE])
            self.assertEqual(
                [
                    decode_ticket_command(Opcode.RESOLVE, f.payload)[0].sequence
                    for f in controls
                    if f.opcode == RELEASE
                ],
                [2],
            )
            h.fixture._gap.disconnect.assert_not_called()
            h.fixture._gatt.reset_server.assert_not_called()
            h.fixture._gatt.set_value.assert_not_called()
            self.assertEqual(
                h.wire.remote_reads,
                [(2, TARGET, b"\x00")],
            )
            self.assertIn(
                api.PREFIX + "generation=2:read:passed:" + h.route(2) + ":value=00",
                h.markers,
            )
            self.assertEqual(
                set(evidence["native_close_facts"]),
                {f"native:{event}" for event in api.NATIVE_EVENTS},
            )
            boundaries = evidence["boundaries"]
            for generation in (1, 2):
                selected = [b for b in boundaries if b["generation"] == generation]
                names = [b["boundary"] for b in selected]
                retired = next(b for b in selected if b["boundary"] == "retired")
                receipt = decode_status(bytes.fromhex(retired["status_wire"]))
                self.assertTrue(cleanup_ready(receipt))
                self.assertEqual(
                    receipt.ticket.owner_nonce.hex(), h.plan.session_id.replace("-", "")
                )
                if generation == 1:
                    self.assertNotIn("resolve-requested", names)
                    self.assertIs(receipt.outcome, Outcome.DISCONNECTED_UNCOMMITTED)
                    self.assertLess(
                        names.index("gap-disconnected"), names.index("retired")
                    )
                else:
                    self.assertTrue(read_completed(receipt))
                    self.assertLess(
                        names.index("before-resolve"), names.index("resolve-requested")
                    )
                    self.assertLess(
                        names.index("retired"), names.index("gap-disconnected")
                    )
                    requested = next(
                        b for b in selected if b["boundary"] == "resolve-requested"
                    )
                    self.assertLess(
                        retired["monotonic"] - requested["resolve_at"],
                        api.RELEASE_SECONDS,
                    )
            self.assertIn(
                "source=synthetic:result=closed",
                evidence["native_close_facts"]["native:late-submit-rejected"],
            )
            original = (h.root / api.PHYSICAL_NAME).read_bytes()
            with self.assertRaisesRegex(ValueError, "stale output"):
                h.run()
            self.assertEqual((h.root / api.PHYSICAL_NAME).read_bytes(), original)

    def test_peripheral_zero_is_canonical_and_noncanonical_decimal_is_rejected(
        self,
    ) -> None:
        for peripheral in (
            "0",
            "1",
            "18446744073709551615",
            "00",
            "01",
            "-0",
            "+0",
            "0.0",
            " 0",
            "0 ",
        ):
            with self.subTest(peripheral=peripheral):
                facts = api.RuntimeFacts(PROBE)
                for marker in (
                    f"generation=1:runtime-start:runId={PROBE}:nonce=engine-1",
                    "generation=1:native-ready:passed",
                    "generation=1:awaiting-arm:" + Harness.route(1),
                    "generation=1:arm-js-observed:source=linking:url=matched",
                ):
                    facts.observe(api.PREFIX + marker)
                dispatched = (
                    api.PREFIX
                    + "generation=1:read-dispatched:"
                    + Harness.route(1)
                    + f":peripheralId={peripheral}"
                )
                if peripheral in ("0", "1", "18446744073709551615"):
                    facts.observe(dispatched)
                    self.assertEqual(facts.seen["1:read-dispatched"], dispatched)
                else:
                    with self.assertRaisesRegex(RuntimeError, "read dispatch"):
                        facts.observe(dispatched)
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary), peripheral_id="0")
            h.run()
            self.assertEqual(h.evidence()["status"], "passed")
            h.fixture._gatt.set_value.assert_not_called()

    def test_remote_read_value_is_not_repaired_or_fabricated_by_coordinator(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            # Fake-only remote value drift after provision: the actual host's JS
            # read check, not fixture-side SET_VALUE, must reject a non-00 read.
            h.wire.remote_value = b"\xab"
            h.fixture._gatt.get_attribute_value.return_value = SimpleNamespace(
                att_response=0, value=b"\xab"
            )
            with self.assertRaisesRegex(RuntimeError, "before value"):
                h.run()
            self.assertEqual(h.evidence()["status"], "failed")
            self.assertEqual(h.wire.remote_reads, [])
            self.assertFalse(any(":read:passed:" in marker for marker in h.markers))
            h.fixture._gatt.set_value.assert_not_called()

    def test_arm_and_release_deadlines_are_fixed_at_their_request_boundaries(
        self,
    ) -> None:
        original_cleanup = api.PendingReload.cleanup
        for fault, boundary, field, budget in (
            ("missing-waiting", "armed", "armed_at", api.ARM_SECONDS),
            ("release-timeout", "resolve-requested", "resolve_at", api.RELEASE_SECONDS),
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary), fault=fault)
                failure_times: list[float] = []

                def cleanup(
                    coordinator: api.PendingReload,
                    *,
                    failed: bool,
                    failure_times: list[float] = failure_times,
                ) -> None:
                    if failed:
                        failure_times.append(api.time.monotonic())
                    original_cleanup(coordinator, failed=failed)

                with (
                    patch.object(
                        api.PendingReload, "cleanup", autospec=True, side_effect=cleanup
                    ),
                    self.assertRaisesRegex(TimeoutError, "deadline"),
                ):
                    h.run()
                anchor = next(
                    b[field]
                    for b in h.evidence()["boundaries"]
                    if b["boundary"] == boundary
                )
                self.assertGreaterEqual(failure_times[0] - anchor, budget)
                self.assertLess(failure_times[0] - anchor, budget + 0.1)
                if fault == "missing-waiting":
                    self.assertNotIn("armed-2", [p["phase"] for p in h.phases])

    def test_every_exact_native_close_marker_is_required_before_second_generation(
        self,
    ) -> None:
        for event in api.NATIVE_EVENTS:
            with self.subTest(event=event), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary), fault=f"missing-{event}")
                with self.assertRaisesRegex(TimeoutError, "deadline"):
                    h.run()
                evidence = h.evidence()
                self.assertEqual(evidence["status"], "failed")
                self.assertNotIn(f"native:{event}", evidence["native_close_facts"])
                self.assertNotIn("armed-2", [p["phase"] for p in h.phases])
                h.fixture._start_advertising.assert_not_called()
                self.assertNotIn(RELEASE, [f.opcode for f in h.wire.commands])
                h.fixture._gatt.set_value.assert_not_called()

    def test_faults_preserve_primary_and_never_pass_or_reset(self) -> None:
        faults = {
            "no-peer": "deadline",
            "abort": "aborted",
            "control-drift": "control changed",
            "foreign-gap": "foreign or duplicate",
            "malformed-gap": "malformed",
            "duplicate-gap": "foreign or duplicate",
            "duplicate-disconnect": "foreign or duplicate",
            "foreign-probe": "foreign native",
            "sequence-reuse": "strictly increase",
            "status-drift": "STATUS ticket mismatch",
            "missing-dispose": "deadline",
            "missing-accepted": "deadline",
            "release-timeout": "deadline",
            "arm-budget": "deadline",
            "missing-final-gap": "deadline",
            "cleanup-failure": "deadline",
        }
        for fault, message in faults.items():
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary), fault)
                with self.assertRaisesRegex(
                    (RuntimeError, TimeoutError, ValueError), message
                ) as caught:
                    h.run()
                evidence = h.evidence()
                self.assertEqual(evidence["status"], "failed")
                self.assertIn(message, evidence["primary_error"])
                h.fixture._gatt.reset_server.assert_not_called()
                h.fixture._gap.stop_advertising.assert_called()
                if fault == "cleanup-failure":
                    self.assertTrue(
                        any(
                            "injected abort failure" in e
                            for e in evidence["cleanup_errors"]
                        )
                    )
                    self.assertTrue(caught.exception.__notes__)
                if fault in (
                    "missing-dispose",
                    "missing-accepted",
                    "arm-budget",
                    "abort",
                    "control-drift",
                ):
                    self.assertNotIn(RELEASE, [f.opcode for f in h.wire.commands])
                if fault == "missing-accepted":
                    self.assertLess(h.clock.now - 100, 12)
                    self.assertNotIn("waiting-1", [p["phase"] for p in h.phases])
                self.assertEqual(h.phases[-1]["phase"], "closed")
                self.assertEqual(h.phases[-1]["status"], "failed")

    def test_cleanup_observes_gap_raced_with_stop_and_keeps_primary_on_unexpected_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            # A new connection arriving while STOP_ADVERTISING is pending must
            # never be promoted into a clean no-peer physical receipt.
            h.fixture._gap.stop_advertising.side_effect = lambda: h.wire.gap(
                GAP_DEVICE_CONNECTED
            )
            with self.assertRaisesRegex(RuntimeError, "physical cleanup failed"):
                h.run()
            self.assertEqual(h.evidence()["status"], "failed")
            self.assertTrue(h.evidence()["cleanup_errors"])
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            with (
                patch.object(
                    api.PendingReload,
                    "cleanup",
                    side_effect=AssertionError("unexpected teardown"),
                ),
                patch.object(
                    api.PendingReload,
                    "cycle",
                    side_effect=RuntimeError("primary route failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "primary route failure") as caught,
            ):
                h.run()
            self.assertIn("unexpected teardown", h.evidence()["cleanup_errors"][0])
            self.assertTrue(caught.exception.__notes__)

    def test_target_receipt_and_session_drift_fail_closed(self) -> None:
        for fault in ("permissions", "handle", "receipt", "snapshot"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                h = Harness(Path(temporary))
                if fault == "permissions":
                    h.fixture._gatt.get_attributes.return_value[0].permissions = 3
                elif fault == "handle":
                    h.fixture._gatt.get_attributes.return_value[0].handle = 98765
                elif fault == "receipt":
                    receipt = api.read_document(h.plan.receipt_path)
                    receipt["fixture_pid"] += 1
                    api.write_document(h.plan.receipt_path, receipt)
                else:
                    h.plan.profile_path.chmod(0o600)
                    h.plan.profile_path.write_text("{}", encoding="utf-8")
                with self.assertRaises((RuntimeError, ValueError)):
                    h.run()
                self.assertEqual(h.evidence()["status"], "failed")
                self.assertNotIn(ARM, [f.opcode for f in h.wire.commands])

    def test_preflight_requires_multifunction_parent_root_and_no_stale_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            for value in (
                {"schema_version": True, "run_id": h.plan.session_id},
                {"schema_version": 1, "run_id": PROBE},
                {"schema_version": 1, "run_id": h.plan.session_id, "extra": 0},
            ):
                api.write_document(h.root / api.CONTROL_NAME, value)
                with self.assertRaises(ValueError):
                    api.validate_control(h.root, h.plan)
            with self.assertRaises(ValueError):
                api.validate_control(h.root, None)
        for function in (provider.direct_start, provider.direct_serve):
            with (
                patch.object(provider, "_port", return_value="fake"),
                patch.object(direct_fixture, "DirectFixture") as owner,
            ):
                with self.assertRaises((ValueError, provider.Nrf52840Error)):
                    function(coordination_mode=api.MODE)
                owner.assert_not_called()

    def test_runtime_snapshot_binding_duplicates_foreign_routes_and_rewind(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            facts = api.RuntimeFacts(h.plan.session_id)
            path = h.root / "runtime.log"
            facts.refresh(path)
            for marker in (
                "generation=1:awaiting-arm:" + h.route(1),
                "native:read-accepted:runId="
                + PROBE
                + ":probeId="
                + PROBE
                + ":"
                + h.route(1),
                "native:read-accepted:runId="
                + h.plan.session_id
                + ":probeId="
                + PROBE
                + ":"
                + h.route(2),
                "generation=1:arm-js-observed:source=notification:url=matched",
                "generation=1:read-dispatched:" + h.route(1) + ":peripheralId=88",
                "generation=2:runtime-start:runId="
                + h.plan.session_id
                + ":nonce=engine-1",
            ):
                with (
                    self.subTest(marker=marker),
                    self.assertRaises((RuntimeError, ValueError)),
                ):
                    facts.observe(api.PREFIX + marker)
            path.write_text(h.markers[0] + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "rewound"):
                facts.refresh(path)
            with self.assertRaisesRegex(RuntimeError, "non-normalized"):
                facts.observe("logcat raw prefix " + h.markers[0])

    def test_provider_cli_and_same_owner_ready_dispatch_preserve_gatt(self) -> None:
        for command in ("direct-start", "direct-serve"):
            with patch(
                "sys.argv",
                [
                    "provider",
                    command,
                    "--coordination-mode",
                    api.MODE,
                    "--coordination-path",
                    "parent",
                    "--fixture-request",
                    "request.json",
                ],
            ):
                self.assertEqual(provider.parse_args().coordination_mode, api.MODE)
        with tempfile.TemporaryDirectory() as temporary:
            h = Harness(Path(temporary))
            h.fixture.start = Mock(return_value={"read-write": TARGET})
            h.fixture.stop = Mock()

            def finish(fixture: object, root: Path, handles: dict[str, int]) -> None:
                self.assertIs(fixture, h.fixture)
                self.assertEqual(root, h.root)
                self.assertEqual(handles, {"read-write": TARGET})
                h.fixture._stop_requested = True

            with (
                patch.object(
                    direct_fixture, "DirectFixture", return_value=h.fixture
                ) as owner,
                patch.object(api, "run", side_effect=finish) as coordinator,
                patch.object(direct_fixture.signal, "signal"),
            ):
                direct_fixture.serve(
                    h.plan.port,
                    h.plan.profile_path,
                    h.root,
                    api.MODE,
                    fixture_session=h.plan,
                )
            owner.assert_called_once_with(
                h.plan.port, h.plan.profile_path, fixture_session=h.plan
            )
            coordinator.assert_called_once()
            self.assertTrue(h.fixture._preserve_gatt)
            h.fixture.stop.assert_called_once()
            # Exercise actual stop(), not the serve mock, to protect no-reset teardown.
            real = object.__new__(direct_fixture.DirectFixture)
            real._stopping = False
            real._mutation_allowed = True
            real._preserve_gatt = True
            real._transport = Mock()
            real._disconnect_connected_peers = Mock()
            real._gap = Mock()
            real._gatt = Mock()
            real._resources = Mock()
            real.stop()
            real._gatt.reset_server.assert_not_called()
            real._resources.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
