"""Synthetic BTP tests only: no USB, radio, App, native count factory or RF claim."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840.support.recovery_emulator import (
    BOOT,
    HANDLES,
    Harness,
)

from . import direct_fixture, provider
from . import recovery_ten as api
from .btp_protocol import BtpFrame
from .fault_control import Opcode
from .gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
)
from .gatt_commands import GATT_SET_VALUE


class RecoveryTenTests(unittest.TestCase):
    def test_passive_actual_result_marker_and_adversarial_shapes(self) -> None:
        for platform, fault in (
            ("ios", ""),
            ("ios", "apple-passive-error"),
            ("android", ""),
        ):
            with (
                tempfile.TemporaryDirectory() as directory,
                self.subTest(platform=platform, fault=fault),
            ):
                h = Harness(Path(directory), platform, fault)
                document = h.run()
                self.assertEqual(document["final_groups"]["disconnections"], 20)

                def parse(lines: list[str], harness: Harness = h) -> api.RuntimeFacts:
                    facts = api.RuntimeFacts(
                        harness.plan.session_id, harness.plan.service_uuid
                    )
                    for line in lines:
                        if "native:signal:" in line:
                            facts.authorized = facts.cycle
                        facts.observe(line)
                    return facts

                self.assertEqual(len(parse(h.lines).passive_errors), 10)
                index = next(
                    i
                    for i, line in enumerate(h.lines)
                    if line.startswith(api.PREFIX + "event:")
                )
                event = json.loads(h.lines[index].removeprefix(api.PREFIX + "event:"))
                for fields in (
                    {"commandId": "10005"},
                    {"commandId": "19999"},
                    {"connectionId": "10010"},
                    {"connectionId": "9904"},
                    {"gattSessionId": "10006"},
                    {"subscriptionId": "10008"},
                    {"connectionState": "Disconnecting"},
                    {"error": {"category": "gatt", "message": "wrong category"}},
                    {
                        "connectionState": "Disconnected",
                        "error": {"category": "connection", "message": "mixed"},
                    },
                    {"connectionState": None, "error": None},
                    {
                        "connectionState": None,
                        "error": {
                            "category": "connection",
                            "message": "foreign",
                            "platformDetails": {"platform": "platform_test"},
                        },
                    },
                ):
                    with self.subTest(fields=fields), self.assertRaises(RuntimeError):
                        lines = list(h.lines)
                        lines[index] = (
                            api.PREFIX + "event:" + json.dumps({**event, **fields})
                        )
                        parse(lines)
                for operation in (
                    "missing",
                    "duplicate",
                    "early",
                    "late",
                    "marker-mismatch",
                    "missing-with-forged-none",
                ):
                    with (
                        self.subTest(operation=operation),
                        self.assertRaises(RuntimeError),
                    ):
                        lines = list(h.lines)
                        if operation == "missing":
                            lines.pop(index)
                        elif operation == "duplicate":
                            lines.insert(index, lines[index])
                        elif operation == "early":
                            lines[index - 1], lines[index] = (
                                lines[index],
                                lines[index - 1],
                            )
                        elif operation == "late":
                            lines[index], lines[index + 1] = (
                                lines[index + 1],
                                lines[index],
                            )
                        elif operation == "missing-with-forged-none":
                            lines.pop(index)
                            lines[index] = (
                                lines[index].rsplit("error=", 1)[0] + "error=None"
                            )
                        else:
                            lines[index + 1] = (
                                lines[index + 1].replace(
                                    "error=none", "error=connection"
                                )
                                if event.get("error") is None
                                else lines[index + 1].replace(
                                    "error=connection", "error=none"
                                )
                            )
                        parse(lines)

    def test_android_scan_budget_parser_exact_schema_order_and_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory), "android")
            h.run()

            def parse(lines: list[str]) -> api.RuntimeFacts:
                facts = api.RuntimeFacts(h.plan.session_id, h.plan.service_uuid)
                for line in lines:
                    if "native:signal:" in line:
                        facts.authorized = facts.cycle
                    facts.observe(line)
                return facts

            self.assertIn("9:scan-budget-started", parse(h.lines).seen)
            for token in (
                "scan-budget-policy:",
                "cycle:0:scan-budget-wait:",
                "cycle:0:scan-budget-dispatch:",
                "cycle:0:scan-budget-started:",
            ):
                index = next(i for i, line in enumerate(h.lines) if token in line)
                for operation in ("missing", "duplicate", "extra-field"):
                    with self.subTest(token=token, operation=operation):
                        lines = list(h.lines)
                        if operation == "missing":
                            lines.pop(index)
                        elif operation == "duplicate":
                            lines.insert(index, lines[index])
                        else:
                            lines[index] += ":extra=true"
                        with self.assertRaises(RuntimeError):
                            parse(lines)
            for old, new in (
                ("quietMs=30000", "quietMs=29999"),
                ("minStartIntervalMs=7000", "minStartIntervalMs=6000"),
                ("waitMs=30000", "waitMs=030000"),
                ("monotonicMs=30000", "monotonicMs=29999"),
                ("monotonicMs=30000", "monotonicMs=9007199254740992"),
            ):
                with self.subTest(new=new), self.assertRaises(RuntimeError):
                    parse([line.replace(old, new, 1) for line in h.lines])
            lines = [
                line.replace(
                    "waitMs=0:monotonicMs=40000", "waitMs=4000:monotonicMs=33000"
                ).replace(
                    "scan-budget-dispatch:monotonicMs=40000",
                    "scan-budget-dispatch:monotonicMs=36999",
                )
                for line in h.lines
            ]
            with self.assertRaisesRegex(RuntimeError, "before quiet/interval"):
                parse(lines)
            lines = list(h.lines)
            wait = next(
                i for i, line in enumerate(lines) if "cycle:0:scan-budget-wait:" in line
            )
            lines[wait], lines[wait + 1] = lines[wait + 1], lines[wait]
            with self.assertRaises(RuntimeError):
                parse(lines)
            identity = h.identity()
            identity["bundle_id"] = "org.reactjs.native.example.BleHubNitroHost"
            lines[0] = api.PREFIX + "native:bootstrap:" + json.dumps(identity)
            with self.assertRaisesRegex(RuntimeError, "outside Android"):
                parse(lines)

    def test_ten_lifecycles_both_platforms_one_session_exact_peers_and_real_dispatch(
        self,
    ) -> None:
        for platform in ("ios", "android"):
            with (
                self.subTest(platform=platform),
                tempfile.TemporaryDirectory() as directory,
            ):
                h = Harness(Path(directory), platform)
                evidence = h.run()
                self.assertEqual(evidence["status"], "passed")
                self.assertEqual(
                    evidence["final_groups"],
                    {
                        "cycles": 10,
                        "connections": 20,
                        "disconnections": 20,
                        "closed_snapshots": 10,
                    },
                )
                self.assertEqual(len(h.wire.remote_reads), 10)
                for index, cycle in enumerate(evidence["cycles"]):
                    self.assertEqual(cycle["cycle"], index)
                    self.assertEqual(
                        [event["kind"] for event in cycle["gap_events"]],
                        ["connected", "disconnected", "connected", "disconnected"],
                    )
                    self.assertEqual(
                        cycle["gap_events"][0]["peer"], cycle["gap_events"][1]["peer"]
                    )
                    self.assertEqual(
                        cycle["gap_events"][2]["peer"], cycle["gap_events"][3]["peer"]
                    )
                    self.assertNotEqual(cycle["old_peer"], cycle["new_peer"])
                    self.assertEqual(set(cycle["resources"]), set(api.RESOURCE_PHASES))
                    self.assertTrue(
                        all(
                            count == 0
                            for count in cycle["resources"]["closed"].values()
                        )
                    )
                    self.assertLess(cycle["readiness_ack_seconds"], 2)
                mutations = [
                    command
                    for command in h.wire.commands
                    if command.service == 2 and command.opcode == GATT_SET_VALUE
                ]
                self.assertEqual(len(mutations), 10)
                self.assertFalse(
                    any(
                        command.service == 255
                        and command.opcode
                        in (
                            Opcode.ARM_READ,
                            Opcode.ARM_WRITE,
                            Opcode.ARM_CCC,
                            Opcode.RESOLVE,
                            Opcode.ABORT,
                        )
                        for command in h.wire.commands
                    )
                )
                self.assertFalse(h.wire.advertising)
                self.assertEqual(h.fixture._connected_peers, set())
                self.assertEqual(
                    (h.root / api.PHYSICAL_NAME).stat().st_mode & 0o777, stat.S_IRUSR
                )
                final = api.read_document(h.root / api.PROGRESS_NAME)
                self.assertEqual(
                    (
                        final["cycle"],
                        final["phase"],
                        final["status"],
                        final["physical_closed"],
                    ),
                    (9, "closed", "passed", True),
                )
                self.assertEqual(
                    len([p for p in h.phases if p["phase"] == "recovery-ready"]), 10
                )

    def test_fast_logical_disconnect_and_reused_address_still_require_two_gap_pairs(
        self,
    ) -> None:
        for fault in ("fast-logical-disconnect", "same-address"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                self.assertEqual(
                    Harness(Path(directory), fault=fault).run()["status"], "passed"
                )

    def test_fail_closed_gap_markers_resources_cancellation_and_identity(self) -> None:
        faults = (
            "malformed-connect",
            "duplicate-connect",
            "foreign-disconnect",
            "duplicate-disconnect",
            "abort",
            "control-change",
            "rewind",
            "fake-cycle-passed",
            "closed-nonzero",
            "active-zero",
            "foreign-native",
            "foreign-hold",
            "cleanup-failure",
        )
        for fault in faults:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), fault=fault)
                with self.assertRaises((RuntimeError, ValueError)):
                    h.run()
                physical = api.read_document(h.root / api.PHYSICAL_NAME)
                self.assertEqual(physical["status"], "failed")
                self.assertTrue(physical["errors"])
                self.assertEqual(
                    api.read_document(h.root / api.PROGRESS_NAME)["status"], "failed"
                )
                self.assertFalse(
                    any(
                        command.service == 255 and command.opcode == Opcode.ABORT
                        for command in h.wire.commands
                    )
                )
                if fault in ("foreign-hold", "cleanup-failure"):
                    self.assertTrue(physical["cleanup_errors"])

    def test_phase_ack_and_whole_budgets_are_absolute_not_poll_renewed(self) -> None:
        for fault, budget in (
            ("no-peer", 90),
            ("missing-native-ack", 2),
            ("missing-runtime-close", 90),
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), fault=fault)
                with self.assertRaises(TimeoutError):
                    h.run()
                self.assertLess(h.clock.now, 100 + budget + 2)
                self.assertEqual(
                    api.read_document(h.root / api.PHYSICAL_NAME)["status"], "failed"
                )
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory), fault="no-peer")
            with (
                patch.object(api, "WHOLE_SECONDS", 0.25),
                self.assertRaises(TimeoutError),
            ):
                h.run()
            self.assertLess(h.clock.now, 101)

    def test_preflight_before_provider_identity_and_hardware_invalid_shape_or_role(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            for control in (
                {"schema_version": True, "run_id": h.plan.session_id, "cycles": 10},
                {"schema_version": 1, "run_id": h.plan.session_id, "cycles": 9},
                {"schema_version": 1, "run_id": BOOT, "cycles": 10},
                {
                    "schema_version": 1,
                    "run_id": h.plan.session_id,
                    "cycles": 10,
                    "extra": 1,
                },
            ):
                api.write_document(h.root / api.CONTROL_NAME, control)
                with (
                    patch.object(provider, "_process_identity") as identity,
                    patch.object(direct_fixture, "DirectFixture") as hardware,
                ):
                    with self.assertRaises(ValueError):
                        provider.direct_start(
                            str(h.root), api.MODE, h.plan.request_path
                        )
                    with self.assertRaises(ValueError):
                        direct_fixture.serve(
                            h.plan.port,
                            h.plan.profile_path,
                            h.root,
                            api.MODE,
                            fixture_session=h.plan,
                        )
                    identity.assert_not_called()
                    hardware.assert_not_called()
            with self.assertRaises(ValueError):
                api.validate_control(h.root, None)

    def test_malformed_foreign_duplicate_gap_never_adopts_or_clears_arbitrary_peer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            coordinator = api.RecoveryTen(h.fixture, h.root, HANDLES)
            coordinator.allow_connection = True
            coordinator.observe_gap(
                BtpFrame(1, GAP_DEVICE_CONNECTED, 0, h.old_peer + bytes(6))
            )
            observed = coordinator.peer
            coordinator.allow_disconnect = True
            for event in (
                BtpFrame(1, GAP_DEVICE_DISCONNECTED, 0, h.new_peer),
                BtpFrame(1, GAP_DEVICE_DISCONNECTED, 1, h.old_peer),
                BtpFrame(1, GAP_DEVICE_DISCONNECTED, 0, h.old_peer + b"\x00"),
                BtpFrame(1, GAP_DEVICE_CONNECTED, 0, h.new_peer + bytes(6)),
            ):
                with self.assertRaises(RuntimeError):
                    coordinator.observe_gap(event)
                self.assertEqual(coordinator.peer, observed)
                self.assertEqual(h.fixture._connected_peers, {observed})
            coordinator.observe_gap(BtpFrame(1, GAP_DEVICE_DISCONNECTED, 0, h.old_peer))
            with self.assertRaises(RuntimeError):
                coordinator.observe_gap(
                    BtpFrame(1, GAP_DEVICE_DISCONNECTED, 0, h.old_peer)
                )

    def test_native_signal_shape_process_boot_url_and_duplicate_are_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            for key, value in (
                ("run_id", BOOT),
                ("pid", 43),
                ("pid", True),
                ("boot_id", h.plan.session_id),
                ("bundle_id", "foreign"),
                ("executable", "/foreign"),
                ("cycle", True),
                ("cycle", 1),
                ("url", f"blehub-rn-spike://recover/{BOOT}/0"),
                ("source", ""),
                ("extra", "invalid"),
            ):
                with self.subTest(key=key, value=value):
                    facts = api.RuntimeFacts(h.plan.session_id, h.plan.service_uuid)
                    facts.native("bootstrap", json.dumps(h.identity()))
                    facts.cycle = facts.authorized = 0
                    facts.seen["0:recovery-awaiting"] = "synthetic-only"
                    record = {
                        **h.identity(),
                        "cycle": 0,
                        "url": facts.url(0),
                        key: value,
                    }
                    with self.assertRaises((RuntimeError, ValueError)):
                        facts.native("signal", json.dumps(record))
            facts = api.RuntimeFacts(h.plan.session_id, h.plan.service_uuid)
            facts.native("bootstrap", json.dumps(h.identity()))
            facts.cycle = facts.authorized = 0
            facts.seen["0:recovery-awaiting"] = "synthetic-only"
            record = json.dumps({**h.identity(), "cycle": 0, "url": facts.url(0)})
            facts.native("signal", record)
            with self.assertRaisesRegex(RuntimeError, "duplicate"):
                facts.native("signal", record)

    def test_provider_ready_and_stop_preserve_one_gatt_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            fixture = Mock()
            fixture._profile.profile_id = h.fixture._profile.profile_id
            fixture.start.return_value = HANDLES
            fixture._stop_requested = True

            def coordinator(
                owner: object, root: Path, handles: dict[str, int], *, on_ready: object
            ) -> None:
                self.assertIs(owner, fixture)
                self.assertEqual((root, handles), (h.root, HANDLES))
                self.assertTrue(fixture._preserve_gatt)
                self.assertTrue(fixture._strict_recovery_cleanup)
                on_ready()

            with (
                patch.object(direct_fixture, "DirectFixture", return_value=fixture),
                patch.object(api, "run", side_effect=coordinator),
                patch.object(direct_fixture.signal, "signal"),
            ):
                direct_fixture.serve(
                    h.plan.port,
                    h.plan.profile_path,
                    h.root,
                    api.MODE,
                    fixture_session=h.plan,
                )
            fixture.start.assert_called_once()
            fixture.stop.assert_called_once()
            owner = SimpleNamespace(
                _stopping=False,
                _mutation_allowed=True,
                _strict_recovery_cleanup=True,
                _preserve_gatt=True,
                _transport=Mock(),
                _gap=Mock(),
                _gatt=Mock(),
                _resources=Mock(),
                _disconnect_connected_peers=Mock(),
            )
            direct_fixture.DirectFixture.stop(owner)
            owner._disconnect_connected_peers.assert_not_called()
            owner._gatt.reset_server.assert_not_called()
            owner._gap.stop_advertising.assert_called_once()
            owner._resources.close.assert_called_once()

    def test_runtime_rewind_foreign_native_and_early_url_not_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory))
            facts = api.RuntimeFacts(h.plan.session_id, h.plan.service_uuid)
            h.begin()
            facts.refresh(h.root / "runtime.log")
            signal = {**h.identity(), "cycle": 0, "url": facts.url(0)}
            with self.assertRaises(RuntimeError):
                facts.observe(api.PREFIX + "native:signal:" + json.dumps(signal))
            (h.root / "runtime.log").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "rewound"):
                facts.refresh(h.root / "runtime.log")


if __name__ == "__main__":
    unittest.main()
