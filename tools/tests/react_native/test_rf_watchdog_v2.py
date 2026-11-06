"""Typed v2 entry regressions with real BTP dispatch; no hardware/RF claims."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import fault_control as fault
from tests.peripherals.nrf52840.btp_session import BtpSession
from tests.peripherals.nrf52840.gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
)
from tests.peripherals.nrf52840.profile import load_profile
from tests.peripherals.nrf52840.support.read_fault_emulator import (
    PEER,
    TARGET,
    Clock,
)
from tests.peripherals.nrf52840.support.session_receipts import write_v2_inputs
from tools.react_native import run_rf_watchdog as runner
from tools.react_native import watchdog_ledger as ledger
from tools.tests.react_native.support.watchdog_runtime import (
    CONTROL,
    RUN,
    Firmware,
    Runtime,
    marker,
)


class V2WatchdogTests(unittest.TestCase):
    def make(self, root: Path, mode: str = ""):
        _, profile_path = write_v2_inputs(root)
        profile = load_profile(profile_path)
        clock = Clock()
        firmware = Firmware(clock, mode)
        session = BtpSession(firmware)
        report = {"session_id": RUN, "control": asdict(CONTROL)}
        owner = runner.Experiment(session, profile, Mock(), report)
        owner.mutation_allowed = True
        owner.gatt = Mock()
        owner.gatt.get_attribute_value.return_value = SimpleNamespace(
            att_response=0, value=b"\x00"
        )
        owner.gap = Mock()
        owner.gap.disconnect.side_effect = lambda *_: firmware.gap(
            GAP_DEVICE_DISCONNECTED
        )
        return owner, Runtime(firmware, profile.service_uuid), firmware

    def test_v2_preparation_reuses_profile_owner_without_reset_and_rejects_unowned_peers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owner, _, firmware = self.make(Path(directory))
            rw = owner.profile.characteristics[0]
            owner.gatt.get_attributes.return_value = [
                SimpleNamespace(handle=TARGET, permissions=0x43, uuid=rw.uuid)
            ]
            owner.controller_settings = Mock(return_value=1)
            with patch.object(runner, "NrfDoctor") as doctor:
                doctor.return_value.run.return_value = SimpleNamespace(
                    role_handles={"read-write": TARGET}
                )
                self.assertEqual(owner.configure_v2(), TARGET)
                doctor.return_value.run.assert_called_once_with(owner.profile)
            owner.gatt.reset_server.assert_not_called()
            owner.gap.set_powered.assert_called_once_with(True)
            self.assertFalse(
                any(
                    frame.opcode == fault.Opcode.ARM_READ for frame in firmware.commands
                )
            )
        with tempfile.TemporaryDirectory() as directory:
            owner, _, firmware = self.make(Path(directory))
            firmware.gap(GAP_DEVICE_CONNECTED)
            with patch.object(runner, "NrfDoctor") as doctor:
                with self.assertRaisesRegex(
                    runner.WatchdogError, "unowned connected peer"
                ):
                    owner.configure_v2()
                doctor.assert_not_called()
            with self.assertRaisesRegex(runner.WatchdogError, "no peers"):
                owner.cleanup_v2()
            owner.gap.disconnect.assert_not_called()
            owner.gatt.reset_server.assert_not_called()

    def test_v2_preparation_native_permissions_and_power_noop_are_not_relaxed(
        self,
    ) -> None:
        from tests.peripherals.nrf52840.btp_commands import BtpCommandError

        for permissions in (0x03, 0x41, 0x47, 0xC3):
            with (
                self.subTest(permissions=permissions),
                tempfile.TemporaryDirectory() as directory,
            ):
                owner, _, _ = self.make(Path(directory))
                owner.gatt.get_attributes.return_value = [
                    SimpleNamespace(
                        handle=TARGET,
                        permissions=permissions,
                        uuid=owner.profile.characteristics[0].uuid,
                    )
                ]
                with patch.object(runner, "NrfDoctor") as doctor:
                    doctor.return_value.run.return_value = SimpleNamespace(
                        role_handles={"read-write": TARGET}
                    )
                    with self.assertRaisesRegex(runner.WatchdogError, "ordinary RW"):
                        owner.configure_v2()
                owner.gap.set_powered.assert_not_called()
                owner.gatt.reset_server.assert_not_called()
        for before, after, code, allowed in (
            (1, 1, "01", True),
            (0, 1, "01", False),
            (1, 0, "01", False),
            (1, 1, "03", False),
        ):
            with (
                self.subTest(before=before, after=after, code=code),
                tempfile.TemporaryDirectory() as directory,
            ):
                owner, _, _ = self.make(Path(directory))
                owner.gatt.get_attributes.return_value = [
                    SimpleNamespace(
                        handle=TARGET,
                        permissions=0x43,
                        uuid=owner.profile.characteristics[0].uuid,
                    )
                ]
                owner.controller_settings = Mock(side_effect=[before, after, after])
                owner.gap.set_powered.side_effect = BtpCommandError(
                    f"BTP status 0x{code}"
                )
                with patch.object(runner, "NrfDoctor") as doctor:
                    doctor.return_value.run.return_value = SimpleNamespace(
                        role_handles={"read-write": TARGET}
                    )
                    if allowed:
                        self.assertEqual(owner.configure_v2(), TARGET)
                    else:
                        with self.assertRaises(BtpCommandError):
                            owner.configure_v2()
                owner.gatt.reset_server.assert_not_called()

    def test_cleanup_foreign_ticket_blocks_all_gap_and_stop_failure_requires_fresh_radio_state(
        self,
    ) -> None:
        from tests.peripherals.nrf52840.btp_commands import BtpCommandError

        with tempfile.TemporaryDirectory() as directory:
            owner, runtime, firmware = self.make(Path(directory))
            with (
                patch.object(
                    runner.time, "monotonic", side_effect=firmware.clock.monotonic
                ),
                patch.object(runner.time, "sleep", side_effect=firmware.clock.sleep),
            ):
                owner.run(runtime, TARGET)
                firmware.ticket = replace(
                    firmware.ticket, sequence=firmware.ticket.sequence + 1
                )
                owner.gap.reset_mock()
                with self.assertRaisesRegex(runner.WatchdogError, "cleanup blocked"):
                    owner.cleanup_v2()
                self.assertEqual(owner.gap.mock_calls, [])
                self.assertFalse(
                    any(f.opcode == fault.Opcode.ABORT for f in firmware.commands)
                )
        for settings in (1, 1 | (1 << 10)):
            with tempfile.TemporaryDirectory() as directory:
                owner, _, _ = self.make(Path(directory))
                owner.report["physical_cleanup"] = {}
                owner.gap.stop_advertising.side_effect = BtpCommandError(
                    "BTP status 0x01"
                )
                owner.controller_settings = Mock(return_value=settings)
                if settings & (1 << 10):
                    with self.assertRaises(BtpCommandError):
                        owner.stop_advertising_for_cleanup()
                else:
                    owner.stop_advertising_for_cleanup()
                    self.assertTrue(
                        owner.report["physical_cleanup"]["advertising_stopped"]
                    )
                owner.gap.set_powered.assert_not_called()

    def test_connected_discovery_arm_handshake_and_retained_resource_zero_no_resolve(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owner, runtime, firmware = self.make(Path(directory))
            with (
                patch.object(
                    runner.time, "monotonic", side_effect=firmware.clock.monotonic
                ),
                patch.object(runner.time, "sleep", side_effect=firmware.clock.sleep),
            ):
                owner.run(runtime, TARGET)
                owner.cleanup_v2()
            self.assertEqual(runtime.signals, ["armed", "held", "released", "recovery"])
            arms = [
                frame
                for frame in firmware.commands
                if frame.opcode == fault.Opcode.ARM_READ
            ]
            self.assertEqual(len(arms), 1)
            request = fault.ArmRequest.decode(fault.Opcode.ARM_READ, arms[0].payload)
            self.assertEqual(
                (request.common.peer, request.before, request.common.owner_nonce),
                (PEER, b"\x00", fault.owner_nonce(RUN)),
            )
            self.assertEqual(
                (request.common.arm_budget_ms, request.common.hold_budget_ms),
                (10000, 20000),
            )
            self.assertTrue(
                all(frame.service == frame.index == 255 for frame in firmware.commands)
            )
            self.assertFalse(
                any(
                    frame.opcode in (fault.Opcode.RESOLVE, fault.Opcode.ABORT)
                    for frame in firmware.commands
                )
            )
            receipt = next(
                row
                for row in owner.report["boundaries"]
                if row["boundary"] == "retired-after-watchdog"
            )
            status = fault.decode_status(bytes.fromhex(receipt["status_wire"]))
            self.assertTrue(fault.cleanup_ready(status))
            self.assertIs(status.outcome, fault.Outcome.DISCONNECTED_UNCOMMITTED)
            self.assertEqual(
                status.ticket,
                fault.ticket_from_document(
                    {key: receipt[key] for key in ("wire_hex", "sequence", "handle")}
                ),
            )
            self.assertEqual(len(owner.report["ledger"]["native_terminals"]), 13)
            self.assertEqual(owner.report["ledger"]["recovery_scan_session"], "21014")

    def test_uart_latency_does_not_extend_the_reported_remaining_hold_budget(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            owner, runtime, firmware = self.make(Path(directory))
            send = firmware.send
            delayed = False

            def slow_status(frame):
                nonlocal delayed
                send(frame)
                if (
                    frame.opcode == fault.Opcode.STATUS
                    and firmware.phase is fault.Phase.HELD
                    and not delayed
                ):
                    delayed = True
                    response = firmware.frames.pop()
                    status = fault.decode_status(response.payload)
                    firmware.frames.append(
                        replace(
                            response,
                            payload=fault.encode_status(
                                replace(
                                    status,
                                    remaining=replace(status.remaining, hold_ms=10500),
                                )
                            ),
                        )
                    )
                    firmware.clock.now += 0.75

            firmware.send = slow_status
            with (
                patch.object(
                    runner.time, "monotonic", side_effect=firmware.clock.monotonic
                ),
                patch.object(runner.time, "sleep", side_effect=firmware.clock.sleep),
                self.assertRaisesRegex(
                    runner.WatchdogError, "control request deadline"
                ),
            ):
                owner.run(runtime, TARGET)
            self.assertTrue(delayed)
            self.assertNotIn("released", runtime.signals)
            self.assertNotIn("ledger", owner.report)
            self.assertFalse(
                any(f.opcode == fault.Opcode.RESOLVE for f in firmware.commands)
            )

    def test_missing_capture_gap_late_boundary_stale_receipt_pins_and_late_domains_fail_closed(
        self,
    ) -> None:
        for mode in (
            "missing-held",
            "missing-gap",
            "late-gate",
            "stale-receipt",
            "pinned-evidence",
            "late-route",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                owner, runtime, firmware = self.make(Path(directory), mode)
                with (
                    patch.object(
                        runner.time, "monotonic", side_effect=firmware.clock.monotonic
                    ),
                    patch.object(
                        runner.time, "sleep", side_effect=firmware.clock.sleep
                    ),
                    self.assertRaises(
                        (
                            runner.WatchdogError,
                            fault.FaultCommandError,
                            ledger.LedgerError,
                        )
                    ),
                ):
                    owner.run(runtime, TARGET)
                self.assertNotIn("ledger", owner.report)
                self.assertFalse(
                    any(
                        frame.opcode == fault.Opcode.RESOLVE
                        for frame in firmware.commands
                    )
                )
                if mode in ("stale-receipt", "pinned-evidence"):
                    self.assertNotIn("released", runtime.signals)

    def test_foreign_duplicate_or_truncated_control_cannot_claim_complete_handshake(
        self,
    ) -> None:
        from tests.peripherals.nrf52840.cancellation_control import control_markers

        text = "".join(marker(line) for line in control_markers(CONTROL))
        ledger.verify_control(text, CONTROL, complete=True)
        for malformed in (
            text + marker(CONTROL.marker("observed", "recovery")),
            text.replace(CONTROL.token, RUN),
            marker(CONTROL.marker("ready", "release")),
            "",
        ):
            with self.assertRaises(ledger.LedgerError):
                ledger.verify_control(malformed, CONTROL, complete=True)

    def test_retired_generic_modes_and_missing_v2_contract_never_open_hardware(
        self,
    ) -> None:
        from tests.peripherals.nrf52840 import direct_fixture, provider
        from tools.react_native import (
            run_android_adapter_transition,
            run_android_permission_transition,
        )

        for mode in (
            "rf-cancellation",
            "legacy-suite-runner",
            "continuous-watchdog",
        ):
            with (
                self.subTest(mode=mode),
                patch.object(direct_fixture, "DirectFixture") as fixture,
                patch.object(provider, "_port") as port,
            ):
                with self.assertRaisesRegex(NotImplementedError, "no v1 wire fallback"):
                    direct_fixture.serve(
                        "fake-port", Path("fake-profile"), coordination_mode=mode
                    )
                with self.assertRaisesRegex(NotImplementedError, "no v1 wire fallback"):
                    provider.direct_start(coordination_mode=mode)
                fixture.assert_not_called()
                port.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _contract, profile = write_v2_inputs(root)
            for module in (
                run_android_adapter_transition,
                run_android_permission_transition,
            ):
                with (
                    self.subTest(module=module.__name__),
                    patch.object(module, "EvidenceBundle") as bundle,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "missing fixture contract"
                    ):
                        module.run(
                            "synthetic-device",
                            root / "missing.apk",
                            root / "missing-contract.json",
                            profile,
                        )
                    bundle.assert_not_called()
            with (
                patch.object(
                    provider, "EXPECTED_CONTRACT", root / "missing-contract.json"
                ),
                patch.object(provider, "_port") as port,
            ):
                with self.assertRaisesRegex(
                    ValueError, "missing fixture contract.*build/export"
                ):
                    provider.direct_start()
                port.assert_not_called()

    def test_ios_entry_requires_exported_v2_contract_before_any_hardware_or_install(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(runner, "open_serial") as opened,
            patch.object(runner, "port_lease") as leased,
        ):
            with self.assertRaisesRegex(ValueError, "missing fixture contract"):
                runner.run(
                    "ios",
                    "explicit-device",
                    Path(directory) / "missing-contract.json",
                    Path(directory) / "missing-profile.json",
                    work=Path(directory),
                )
            opened.assert_not_called()
            leased.assert_not_called()
