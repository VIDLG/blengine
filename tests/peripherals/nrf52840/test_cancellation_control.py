"""Fake-only protocol, same-session evidence and cleanup tests; never access USB."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840.support.session_receipts import fake_attempt
from tests.support.rf_lab.markers import controlled_markers

from . import cancellation_control as control_api
from . import direct_fixture, fixture_session
from . import fault_control as fault_wire
from .btp_protocol import BtpFrame
from .btp_session import BtpSession
from .fixture_contract import (
    MULTIFUNCTION_CAPABILITIES,
    NOTIFICATION_OBSERVATION_CAPABILITY,
)
from .support.fault_emulator import PEER, Clock, Wire

SERVICE = "fd000000-0000-4000-8000-000000000000"


class CancellationControlTests(unittest.TestCase):
    def test_multifunction_preflight_accepts_only_named_variants_and_opt_in_needs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = fake_attempt(Path(tmp))
            plan = attempt.plan
            base = frozenset(MULTIFUNCTION_CAPABILITIES)
            observed = base | {NOTIFICATION_OBSERVATION_CAPABILITY}
            for capabilities in (base, observed):
                selected = replace(
                    plan, contract=replace(plan.contract, capabilities=capabilities)
                )
                self.assertIs(control_api.require_multifunction(selected), selected)
                self.assertIs(
                    control_api.require_multifunction(
                        selected, required_capabilities=base
                    ),
                    selected,
                )
                if capabilities == observed:
                    self.assertIs(
                        control_api.require_multifunction(
                            selected,
                            required_capabilities=frozenset(
                                {NOTIFICATION_OBSERVATION_CAPABILITY}
                            ),
                        ),
                        selected,
                    )
                else:
                    with self.assertRaisesRegex(
                        ValueError, "missing required capabilities"
                    ):
                        control_api.require_multifunction(
                            selected,
                            required_capabilities=frozenset(
                                {NOTIFICATION_OBSERVATION_CAPABILITY}
                            ),
                        )
                with self.assertRaisesRegex(
                    ValueError, "missing required capabilities"
                ):
                    control_api.require_multifunction(
                        selected, required_capabilities=frozenset({"unknown"})
                    )
                for extra in (
                    "unknown",
                    "notification-tx-observation-v2",
                    "notification-tx-observation-v1x",
                ):
                    with (
                        self.subTest(capabilities=capabilities, extra=extra),
                        self.assertRaises(ValueError),
                    ):
                        control_api.require_multifunction(
                            replace(
                                selected,
                                contract=replace(
                                    selected.contract,
                                    capabilities=capabilities | {extra},
                                ),
                            )
                        )
            for capability in base:
                with self.subTest(missing=capability), self.assertRaises(ValueError):
                    control_api.require_multifunction(
                        replace(
                            plan,
                            contract=replace(
                                plan.contract, capabilities=observed - {capability}
                            ),
                        )
                    )
            with self.assertRaises(ValueError):
                control_api.require_multifunction(None)
            with self.assertRaises(ValueError):
                control_api.require_multifunction(
                    replace(
                        plan, contract=replace(plan.contract, fixture_role="normal-rf")
                    )
                )

    def test_relay_rejects_wrong_duplicate_early_and_unconfirmed_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = fake_attempt(Path(tmp))
            control = attempt.control
            records = control_api.control_markers(control)
            text = lambda lines: "\n".join(
                "BLEHUB_NITRO_RUNTIME " + line for line in lines
            )
            for invalid in (
                records[1:2],
                records[:1] * 2,
                [records[0].replace(control.token, control.session)],
                records[:3],
            ):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    control_api.ControlRelay(attempt.root, control).observe(
                        text(invalid)
                    )
            relay = control_api.ControlRelay(attempt.root, control)
            relay.observe(text(records[:2]))
            control_api.write_document(
                attempt.directory / "signal-armed.json", asdict(control)
            )
            self.assertEqual(relay.pending(), "armed")
            relay.forwarded("armed")
            relay.observe(text(records[:4]))
            relay.forwarded("held")
            with self.assertRaisesRegex(ValueError, "exact queued"):
                relay.observe(text(records[:6]))
            terminal = (
                "rf:step:cancellation-terminal:passed:commandId=9028:error=cancelled"
            )
            relay.observe(text([*records[:5], terminal, records[5]]))
            self.assertTrue((attempt.directory / "request-release.json").exists())
            control_api.write_document(
                attempt.directory / "failure.json", {"error": "expired"}
            )
            with self.assertRaisesRegex(RuntimeError, "expired"):
                relay.pending()

    def test_relay_never_forwards_a_stale_physical_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = fake_attempt(Path(tmp))
            relay = control_api.ControlRelay(attempt.root, attempt.control)
            records = control_api.control_markers(attempt.control)
            relay.observe(
                "\n".join("BLEHUB_NITRO_RUNTIME " + line for line in records[:2])
            )
            control_api.write_document(
                attempt.directory / "signal-armed.json", asdict(attempt.control)
            )
            with (
                patch.object(
                    control_api.time,
                    "monotonic",
                    return_value=attempt.evidence["boundaries"][0]["monotonic"]
                    + control_api.RELEASE_BUDGET,
                ),
                self.assertRaisesRegex(TimeoutError, "budget"),
            ):
                relay.pending()
            self.assertEqual(relay.sent, [])

    def test_control_session_and_signal_token_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = fake_attempt(Path(tmp))
            control = attempt.control
            document = asdict(control)
            document["session"] = control.token
            control_api.write_document(
                attempt.root / control_api.CONTROL_NAME, document
            )
            with self.assertRaisesRegex(ValueError, "session drift"):
                control_api.verify_control(
                    attempt.root, attempt.plan, (os.getpid(), os.getpgid(0))
                )
            control_api.write_document(
                attempt.root / control_api.CONTROL_NAME, asdict(control)
            )
            relay = control_api.ControlRelay(attempt.root, control)
            document = asdict(control)
            document["token"] = control.session
            control_api.write_document(
                attempt.directory / "signal-armed.json", document
            )
            with self.assertRaisesRegex(ValueError, "foreign metadata"):
                relay.pending()

    def test_checker_requires_physical_receipt_and_exact_boundaries_not_js_only(
        self,
    ) -> None:
        from tests.support.rf_lab.markers import CANCELLATION_MARKERS, log

        with tempfile.TemporaryDirectory() as tmp:
            attempt = fake_attempt(Path(tmp))
            text = log(controlled_markers(CANCELLATION_MARKERS, attempt.control))
            control_api.check_physical_evidence(attempt.path, text, SERVICE)
            with self.assertRaisesRegex(ValueError, "not JS-only"):
                control_api.check_physical_evidence(None, text, SERVICE)
            for fault in (
                "status",
                "token",
                "pid",
                "resource",
                "sequence",
                "handle",
                "peer",
                "budget",
                "nan",
                "closed",
                "cleanup",
                "extra-key",
            ):
                evidence = json.loads(json.dumps(attempt.evidence))
                if fault == "status":
                    evidence["status"] = "failed"
                elif fault == "token":
                    evidence["control"]["token"] = attempt.control.session
                elif fault == "pid":
                    evidence["fixture_pid"] += 1
                elif fault == "resource":
                    evidence["resource_identity"] = "jlink:654321"
                elif fault == "sequence":
                    evidence["boundaries"][2]["ticket"]["sequence"] += 1
                elif fault == "handle":
                    evidence["boundaries"][2]["ticket"]["handle"] += 1
                elif fault == "peer":
                    evidence["boundaries"][2]["peer"][0] = "665544332211"
                elif fault == "budget":
                    evidence["boundaries"][4]["monotonic"] = (
                        evidence["boundaries"][0]["monotonic"] + 7
                    )
                    evidence["boundaries"][5]["monotonic"] = (
                        evidence["boundaries"][0]["monotonic"] + 8
                    )
                elif fault == "nan":
                    evidence["boundaries"][2]["monotonic"] = float("nan")
                elif fault == "closed":
                    evidence["boundaries"][5]["peer_count"] = 1
                elif fault == "cleanup":
                    evidence["cleanup_errors"] = ["abort failed"]
                else:
                    evidence["unexpected"] = True
                control_api.write_document(attempt.path, evidence)
                with (
                    self.subTest(fault=fault),
                    self.assertRaises((ValueError, RuntimeError)),
                ):
                    control_api.check_physical_evidence(attempt.path, text, SERVICE)
            control_api.write_document(attempt.path, attempt.evidence)
            for invalid in (
                text.replace(attempt.control.token, attempt.control.session),
                text
                + "\nBLEHUB_NITRO_RUNTIME "
                + attempt.control.marker("observed", "held"),
                text.replace(
                    "commandId=9028|9027|9027|9029|9029",
                    "commandId=9027|9027|9028|9029|9029",
                ),
                text.replace(
                    "value=none|00|none|00|none", "value=none|00|none|10|none"
                ),
            ):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    control_api.check_physical_evidence(attempt.path, invalid, SERVICE)

    def test_provider_uses_existing_session_and_provider_handle_and_cleans_up(
        self,
    ) -> None:
        for fault in (
            None,
            "never-held",
            "generation",
            "expiry",
            "budget",
            "peer",
            "abort-cleanup",
        ):
            with self.subTest(fault=fault):
                self._provider_case(fault)

    def test_provider_rejects_wrong_native_permissions_before_arm(self) -> None:
        for fault in ("permissions-03", "permissions-41", "permissions-c3"):
            with self.subTest(fault=fault):
                self._provider_case(fault)

    def test_provider_close_requires_exact_gap_disconnect_and_timely_status(
        self,
    ) -> None:
        for fault in (
            "close-foreign",
            "close-truncated",
            "close-extra",
            "close-index",
            "close-index-type",
            "close-address-type",
            "close-peer-type",
            "close-service-type",
            "close-opcode-type",
            "close-payload-type",
            "close-duplicate",
            "close-connection",
            "close-cleared-without-event",
            "close-status-before-deadline",
            "close-status-at-deadline",
            "close-status-after-deadline",
        ):
            with self.subTest(fault=fault):
                self._provider_case(fault)

    def test_provider_cleanup_checks_deadline_after_inactive_status_returns(
        self,
    ) -> None:
        for fault in (
            "cleanup-status-before-deadline",
            "cleanup-status-at-deadline",
            "cleanup-status-after-deadline",
        ):
            with self.subTest(fault=fault):
                self._provider_case(fault)

    def _provider_case(self, fault: str | None) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = fake_attempt(Path(tmp))
            clock = Clock()
            delayed = False

            original_status = fault_wire.FaultController.status

            def inspected_status(controller, ticket, **kwargs):
                nonlocal delayed
                result = original_status(controller, ticket, **kwargs)
                if (
                    fault
                    and "status-" in fault
                    and wire.phase is fault_wire.Phase.RETIRED
                    and not delayed
                    and (wire.aborting or (attempt.directory / "done").exists())
                ):
                    clock.now += (7 if wire.aborting else 3) + (
                        -0.01 if "before" in fault else 0.1 if "after" in fault else 0
                    )
                    delayed = True
                return result

            wire = Wire(
                clock,
                "status-drift"
                if fault == "generation"
                else "cleanup-failure"
                if fault == "abort-cleanup"
                else "",
            )
            wire.gap(direct_fixture.GAP_DEVICE_CONNECTED)
            fixture = object.__new__(direct_fixture.DirectFixture)
            fixture._fixture_session = attempt.plan
            fixture._profile = fixture_session.load_session_profile(
                attempt.plan.contract, attempt.plan.profile_path
            )
            fixture._session = BtpSession(wire)
            # The provider has already observed the connection before App ARM-ready.
            fault_wire.FaultController(
                fixture._session, attempt.plan.session_id
            ).get_limits()
            fixture._transport, fixture._gatt = Mock(), Mock()
            fixture._stop_requested, fixture._connected_peers = False, set()
            native_permissions = {
                "permissions-03": 0x03,
                "permissions-41": 0x41,
                "permissions-c3": 0xC3,
            }.get(fault, 0x43)
            rw = fixture._profile.characteristics[0]
            fixture._gatt.get_attributes.return_value = [
                SimpleNamespace(handle=51, permissions=native_permissions, uuid=rw.uuid)
            ]
            fixture._gatt.get_attribute_value.return_value = SimpleNamespace(
                att_response=0, value=b"\x00"
            )
            real_write = control_api.write_document

            def publish(path: Path, document: dict) -> None:
                real_write(path, document)
                if path.name == "signal-armed.json":
                    real_write(
                        attempt.directory / "request-read-dispatched.json",
                        asdict(attempt.control),
                    )
                    if fault not in ("never-held", "abort-cleanup") and not (
                        fault or ""
                    ).startswith("cleanup-status-"):
                        wire.capture_read()
                    if fault == "peer":
                        fixture._connected_peers.add(("665544332211", 1))
                    if fault == "budget":
                        clock.now += 8
                elif path.name == "signal-held.json":
                    real_write(
                        attempt.directory / "request-release.json",
                        asdict(attempt.control),
                    )
                    if fault == "expiry":
                        clock.now += 20
                elif path.name == "signal-released.json":
                    (attempt.directory / "done").touch()
                    wire.gap(direct_fixture.GAP_DEVICE_DISCONNECTED)
                    event = wire.frames.pop()
                    service, opcode, index, payload = (
                        event.service,
                        event.opcode,
                        event.index,
                        event.payload,
                    )
                    if fault == "close-foreign":
                        payload = bytes.fromhex("01112233445566")
                    elif fault == "close-truncated":
                        payload = payload[:-1]
                    elif fault == "close-extra":
                        payload += b"\x00"
                    elif fault == "close-index":
                        index = 1
                    elif fault == "close-index-type":
                        index = False
                    elif fault == "close-address-type":
                        payload = b"\x04" + payload[1:]
                    elif fault == "close-peer-type":
                        payload = b"\x00" + payload[1:]
                    elif fault == "close-service-type":
                        service = True
                    elif fault == "close-opcode-type":
                        opcode = float(opcode)
                    elif fault == "close-payload-type":
                        payload = list(payload)
                    elif fault == "close-connection":
                        opcode = direct_fixture.GAP_DEVICE_CONNECTED
                        payload += bytes(6)
                    if fault == "close-cleared-without-event":
                        fixture._connected_peers.clear()
                    else:
                        event = BtpFrame(service, opcode, index, payload)
                        wire.frames.append(event)
                        if fault == "close-duplicate":
                            wire.frames.append(event)

            real_write(attempt.directory / "request-arm.json", asdict(attempt.control))
            with (
                patch.object(control_api, "write_document", side_effect=publish),
                patch.object(
                    fault_wire.FaultController, "status", new=inspected_status
                ),
                patch.object(
                    direct_fixture.time, "monotonic", side_effect=clock.monotonic
                ),
                patch.object(direct_fixture.time, "sleep", side_effect=clock.sleep),
            ):
                if fault in (None, "close-status-before-deadline"):
                    fixture.run_cancellation_coordination(
                        attempt.root, {"read-write": 51}
                    )
                    evidence = control_api.read_document(attempt.path)
                    self.assertEqual(evidence["status"], "passed")
                    receipt = fault_wire.decode_status(
                        bytes.fromhex(evidence["boundaries"][-1]["status_wire"])
                    )
                    self.assertTrue(fault_wire.read_completed(receipt))
                    self.assertEqual(
                        receipt.outcome, fault_wire.Outcome.NORMAL_NO_COMMIT
                    )
                else:
                    with self.assertRaises((RuntimeError, ValueError, TimeoutError)):
                        fixture.run_cancellation_coordination(
                            attempt.root, {"read-write": 51}
                        )
                    evidence = control_api.read_document(attempt.path)
                    self.assertEqual(evidence["status"], "failed")
                    self.assertTrue((attempt.directory / "failure.json").exists())
                    if fault == "abort-cleanup" or fault in (
                        "cleanup-status-at-deadline",
                        "cleanup-status-after-deadline",
                    ):
                        self.assertTrue(evidence["cleanup_errors"])
                    if (fault or "").startswith("close-"):
                        self.assertNotIn(
                            "closed", [b["boundary"] for b in evidence["boundaries"]]
                        )
            commands = wire.commands
            self.assertTrue(all(f.service == f.index == 255 for f in commands))
            if (fault or "").startswith("permissions-"):
                self.assertFalse(
                    any(f.opcode == fault_wire.Opcode.ARM_READ for f in commands)
                )
            else:
                arm = next(
                    f for f in commands if f.opcode == fault_wire.Opcode.ARM_READ
                )
                request = fault_wire.ArmRequest.decode(
                    fault_wire.Opcode.ARM_READ, arm.payload
                )
                self.assertEqual(
                    (request.before, request.common.peer, request.common.owner_nonce),
                    (b"\x00", PEER, fault_wire.owner_nonce(attempt.plan.session_id)),
                )
            mutations = [
                f
                for f in commands
                if f.opcode in (fault_wire.Opcode.RESOLVE, fault_wire.Opcode.ABORT)
            ]
            if (fault or "").startswith("close-") or fault is None:
                self.assertEqual(
                    [f.opcode for f in mutations], [fault_wire.Opcode.RESOLVE]
                )
            fixture._gatt.reset_server.assert_not_called()
            fixture._gatt.set_value.assert_not_called()


if __name__ == "__main__":
    unittest.main()
