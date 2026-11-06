"""Hardware-free tests for the native BLeng iOS system-loss host contract."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from tools.apple import system_loss_host as api
from tools.react_native.ios_radio_lease import NATIVE_SYSTEM_LOSS_SCOPE, IosRadioLease

RUN = "11111111-2222-4333-8444-555555555555"
IDENTITY = {
    "pid": 321,
    "boot_id": "22222222-2222-4333-8444-555555555555",
    "owner_id": "33333333-3333-4333-8444-555555555555",
    "backend_id": "44444444-4444-4333-8444-555555555555",
}
BINDING = {
    "control_sha256": "11" * 32,
    "binding_sha256": "22" * 32,
    "witness_sha256": "33" * 32,
}


@dataclass
class FakeRadio:
    value: str = "1"
    off_calls: int = 0
    on_calls: int = 0
    fail_off: bool = False
    fail_on: bool = False

    def read(self) -> str:
        return self.value

    def set_off(self) -> None:
        self.off_calls += 1
        if self.fail_off:
            raise RuntimeError("UI OFF failed")
        self.value = "0"

    def set_on(self) -> None:
        self.on_calls += 1
        if self.fail_on:
            raise RuntimeError("UI ON failed")
        self.value = "1"


def provider_ready() -> dict[str, object]:
    return {
        "schema_version": 1,
        "state": "off-ready",
        "radio_authorized": False,
        **BINDING,
    }


def disconnect_receipt() -> dict[str, object]:
    return {
        "schema_version": 1,
        "state": "disconnected-retired",
        "outcome": "disconnected-uncommitted",
        **BINDING,
    }


class SystemLossHostTests(unittest.TestCase):
    def make(
        self, *, radio: FakeRadio | None = None
    ) -> tuple[api.SystemLossHost, FakeRadio]:
        clock = iter((10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7))
        selected = radio or FakeRadio()
        host = api.SystemLossHost(
            run_id=RUN,
            case="response-write-queued-ccc-enable",
            identity=IDENTITY,
            switch=selected,
            now=lambda: next(clock, 10.7),
        )
        host.bind_pending(
            **BINDING,
            old_command_ids=["18446744073709551614", "9007199254740993"],
            old_link={
                "connection_id": "41",
                "gatt_session_id": "67",
                "subscription_id": "73",
            },
            off_budget_seconds=8,
        )
        return host, selected

    def ready(self) -> api.SystemLossHost:
        host, _ = self.make()
        host.authorize_off(provider_ready())
        host.perform_off()
        host.record_disconnect(disconnect_receipt())
        host.restore_on()
        return host

    def test_file_radio_switch_refreshes_ledger_while_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initial = {
                "schema_version": 1,
                "scope": "ios-bleng-system-loss-ui",
                "run_id": RUN,
                "status": "initial-on",
                "mutation_attempted": False,
                "restored": False,
                "actions": [
                    {"action": "initial-on", "elapsed_seconds": 0, "switch_value": "1"}
                ],
            }
            off = {
                **initial,
                "status": "off-readback",
                "mutation_attempted": True,
                "actions": initial["actions"]
                + [{"action": "off", "elapsed_seconds": 1, "switch_value": "0"}],
            }
            (root / "system-loss-ui.json").write_text(
                json.dumps(initial), encoding="utf-8"
            )
            refreshes = 0

            def refresh() -> None:
                nonlocal refreshes
                refreshes += 1
                if refreshes == 2:
                    (root / "system-loss-ui.json").write_text(
                        json.dumps(off), encoding="utf-8"
                    )

            switch = api.FileRadioSwitch(
                root,
                run_id=RUN,
                timeout_seconds=1,
                sleep=lambda _: None,
                refresh=refresh,
            )
            switch.set_off()
            self.assertGreaterEqual(refreshes, 2)

    def test_file_radio_switch_uses_only_ui_ledger_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            switch = api.FileRadioSwitch(
                root, run_id=RUN, timeout_seconds=1, sleep=lambda _: None
            )
            initial = {
                "schema_version": 1,
                "scope": "ios-bleng-system-loss-ui",
                "run_id": RUN,
                "status": "initial-on",
                "mutation_attempted": False,
                "restored": False,
                "actions": [
                    {"action": "initial-on", "elapsed_seconds": 0, "switch_value": "1"}
                ],
            }
            (root / "system-loss-ui.json").write_text(
                json.dumps(initial), encoding="utf-8"
            )
            self.assertEqual(switch.read(), "1")
            off = {
                **initial,
                "status": "off-readback",
                "mutation_attempted": True,
                "actions": initial["actions"]
                + [{"action": "off", "elapsed_seconds": 1, "switch_value": "0"}],
            }
            (root / "system-loss-ui.json").write_text(json.dumps(off), encoding="utf-8")
            switch.set_off()
            self.assertEqual(
                json.loads((root / "off-authorized").read_text())["action"],
                "off-authorized",
            )
            restored = {
                **off,
                "status": "restored",
                "restored": True,
                "actions": off["actions"]
                + [
                    {"action": "restored-on", "elapsed_seconds": 2, "switch_value": "1"}
                ],
            }
            (root / "system-loss-ui.json").write_text(
                json.dumps(restored), encoding="utf-8"
            )
            switch.set_on()
            self.assertEqual(
                json.loads((root / "restore-on-authorized").read_text())["action"],
                "restore-on-authorized",
            )

    def test_loss_and_radio_authorization_are_separate_phases(self) -> None:
        host, _ = self.make()
        command = host.loss_authorization_command()
        self.assertEqual(command["action"], "authorize-loss")
        with self.assertRaises(api.SystemLossHostError):
            host.authorization_command()
        host.authorize_off(provider_ready())
        command = host.authorization_command()
        self.assertEqual(command["action"], "authorize-loss")
        host.perform_off()
        host.record_disconnect(disconnect_receipt())
        with self.assertRaises(api.SystemLossHostError):
            host.restoration_command("44" * 32)
        host.restore_on()
        restore = host.restoration_command("44" * 32)
        self.assertEqual(restore["action"], "restore-on")
        self.assertEqual(restore["terminals_sha256"], "44" * 32)
        self.assertEqual(len(restore["disconnect_receipt_sha256"]), 64)

    def test_success_is_one_off_one_on_and_same_owner_only(self) -> None:
        host = self.ready()
        host.record_recovery(
            identity=IDENTITY,
            old_terminals=[
                {"command_id": "18446744073709551614", "result": "connection-lost"},
                {"command_id": "9007199254740993", "result": "operation-aborted"},
            ],
            new_link={
                "connection_id": "101",
                "gatt_session_id": "102",
                "subscription_id": "103",
            },
        )
        document = host.finish()
        self.assertEqual(document["state"], "finished")
        self.assertEqual(document["off_count"], 1)
        self.assertEqual(document["on_count"], 1)
        self.assertEqual(document["mutation_attempted"], True)

    def test_fresh_owner_recovery_requires_new_owner_backend_but_same_process(
        self,
    ) -> None:
        host = self.ready()
        new_identity = {
            **IDENTITY,
            "owner_id": "55555555-5555-4333-8444-555555555555",
            "backend_id": "66666666-6666-4333-8444-666666666666",
        }
        document = host.record_fresh_owner_recovery(
            old_identity=IDENTITY,
            new_identity=new_identity,
            old_terminals=[
                {"command_id": "18446744073709551614", "result": "connection-lost"},
                {"command_id": "9007199254740993", "result": "operation-aborted"},
            ],
            new_link={
                "connection_id": "101",
                "gatt_session_id": "102",
                "subscription_id": "103",
            },
        )
        self.assertEqual(document["state"], "fresh-recovered")
        self.assertEqual(document["recovery"]["recovery_mode"], "fresh-owner")
        finished = host.finish()
        self.assertEqual(finished["state"], "finished")
        self.assertFalse(host.lease_control("55" * 32)["same_session"])
        with self.assertRaises(api.SystemLossHostError):
            host.record_fresh_owner_recovery(
                old_identity=IDENTITY,
                new_identity={**new_identity, "pid": 999},
                old_terminals=[
                    {"command_id": "18446744073709551614", "result": "connection-lost"},
                    {"command_id": "9007199254740993", "result": "operation-aborted"},
                ],
                new_link={
                    "connection_id": "104",
                    "gatt_session_id": "105",
                    "subscription_id": "106",
                },
            )

    def test_platform_timeout_is_terminal_but_not_a_recovery_pass(self) -> None:
        host = self.ready()
        reason = "no-advertisement-callback-after-fresh-owner"
        binding = host.document()["binding"]
        diagnostic = {
            "schema_version": 1,
            "state": "recovery-diagnostic",
            "session_id": RUN,
            "control_sha256": binding["control_sha256"],
            "witness_sha256": binding["witness_sha256"],
            "owner_identity": IDENTITY,
            "recovery_outcome": "platform-timeout",
            "recovery_failure_reason": reason,
        }
        document = host.record_recovery_terminal(
            outcome="platform-timeout", reason=reason, diagnostic=diagnostic
        )
        self.assertEqual(document["state"], "platform-timeout")
        self.assertEqual(document["recovery"]["recovery_outcome"], "platform-timeout")
        self.assertTrue(host.lease_control("55" * 32)["restored"])
        self.assertFalse(host.lease_control("55" * 32)["status"] == "passed")
        with self.assertRaises(api.SystemLossHostError):
            host.finish()
        host.fail("runner observed the expected non-pass terminal")
        self.assertEqual(host.state, "platform-timeout")
        control = host.lease_control("55" * 32)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lease = IosRadioLease(
                "a" * 40,
                RUN,
                root / "lease.json",
                lock_root=root / "locks",
                scope=NATIVE_SYSTEM_LOSS_SCOPE,
            )
            lease.acquire()
            raw = (json.dumps(control, sort_keys=True) + "\n").encode()
            self.assertEqual(lease.finish(control, hashlib.sha256(raw).hexdigest()), [])
            self.assertFalse(lease.lock.exists())

    def test_platform_timeout_rejects_foreign_or_mismatched_diagnostic(self) -> None:
        host = self.ready()
        binding = host.document()["binding"]
        diagnostic = {
            "schema_version": 1,
            "state": "recovery-diagnostic",
            "session_id": RUN,
            "control_sha256": binding["control_sha256"],
            "witness_sha256": binding["witness_sha256"],
            "owner_identity": {**IDENTITY, "pid": 999},
            "recovery_outcome": "platform-timeout",
            "recovery_failure_reason": "no-advertisement-callback-after-fresh-owner",
        }
        with self.assertRaises(api.SystemLossHostError):
            host.record_recovery_terminal(
                outcome="platform-timeout",
                reason="no-advertisement-callback-after-fresh-owner",
                diagnostic=diagnostic,
            )
        self.assertEqual(host.state, "restored-on")

    def test_provider_cannot_authorize_stale_or_radio_authorized_input(self) -> None:
        host, _ = self.make()
        for mutation in (
            {**provider_ready(), "radio_authorized": True},
            {**provider_ready(), "witness_sha256": "ff" * 32},
            {**provider_ready(), "state": "held-pending"},
        ):
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(api.SystemLossHostError),
            ):
                host.authorize_off(mutation)
        self.assertEqual(host.state, "pending")

    def test_off_is_not_retried_and_failed_off_is_not_pass(self) -> None:
        radio = FakeRadio(fail_off=True)
        host, _ = self.make(radio=radio)
        host.authorize_off(provider_ready())
        with self.assertRaises(api.SystemLossHostError):
            host.perform_off()
        self.assertEqual(radio.off_calls, 1)
        self.assertEqual(host.state, "radio-unknown")
        with self.assertRaises(api.SystemLossHostError):
            host.perform_off()
        self.assertEqual(radio.off_calls, 1)

    def test_uncertain_off_allows_one_restore_but_never_claims_recovery(self) -> None:
        radio = FakeRadio(fail_off=True)
        host, _ = self.make(radio=radio)
        host.authorize_off(provider_ready())
        with self.assertRaises(api.SystemLossHostError):
            host.perform_off()
        host.restore_on()
        self.assertEqual(host.state, "restored-on")
        with self.assertRaises(api.SystemLossHostError):
            host.finish()
        self.assertEqual(radio.on_calls, 1)

    def test_disconnect_requires_exact_retained_uncommitted_receipt(self) -> None:
        host, _ = self.make()
        host.authorize_off(provider_ready())
        host.perform_off()
        for mutation in (
            {**disconnect_receipt(), "outcome": "injected-reject"},
            {**disconnect_receipt(), "binding_sha256": "ff" * 32},
            {**disconnect_receipt(), "state": "off-ready"},
        ):
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(api.SystemLossHostError),
            ):
                host.record_disconnect(mutation)
        self.assertEqual(host.state, "radio-off")

    def test_old_operations_need_one_terminal_each_and_new_ids(self) -> None:
        host = self.ready()
        with self.assertRaises(api.SystemLossHostError):
            host.record_recovery(
                identity=IDENTITY,
                old_terminals=[
                    {"command_id": "18446744073709551614", "result": "connection-lost"}
                ],
                new_link={
                    "connection_id": "101",
                    "gatt_session_id": "102",
                    "subscription_id": "103",
                },
            )
        with self.assertRaises(api.SystemLossHostError):
            host.record_recovery(
                identity={
                    **IDENTITY,
                    "owner_id": "55555555-5555-4333-8444-555555555555",
                },
                old_terminals=[
                    {"command_id": "18446744073709551614", "result": "connection-lost"},
                    {"command_id": "9007199254740993", "result": "operation-aborted"},
                ],
                new_link={
                    "connection_id": "101",
                    "gatt_session_id": "102",
                    "subscription_id": "103",
                },
            )
        with self.assertRaises(api.SystemLossHostError):
            host.record_recovery(
                identity=IDENTITY,
                old_terminals=[
                    {"command_id": "18446744073709551614", "result": "connection-lost"},
                    {"command_id": "9007199254740993", "result": "operation-aborted"},
                ],
                new_link={
                    "connection_id": "41",
                    "gatt_session_id": "102",
                    "subscription_id": "103",
                },
            )

    def test_on_failure_is_quarantined_and_not_retried(self) -> None:
        radio = FakeRadio(fail_on=True)
        host, _ = self.make(radio=radio)
        host.authorize_off(provider_ready())
        host.perform_off()
        host.record_disconnect(disconnect_receipt())
        with self.assertRaises(api.SystemLossHostError):
            host.restore_on()
        self.assertEqual(host.state, "quarantined")
        self.assertEqual(radio.on_calls, 1)
        with self.assertRaises(api.SystemLossHostError):
            host.restore_on()
        self.assertEqual(radio.on_calls, 1)

    def test_native_lease_consumes_the_same_host_restoration_control(self) -> None:
        host = self.ready()
        host.record_recovery(
            identity=IDENTITY,
            old_terminals=[
                {"command_id": "18446744073709551614", "result": "connection-lost"},
                {"command_id": "9007199254740993", "result": "operation-aborted"},
            ],
            new_link={
                "connection_id": "101",
                "gatt_session_id": "102",
                "subscription_id": "103",
            },
        )
        host.finish()
        control = host.lease_control("55" * 32)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lease = IosRadioLease(
                "a" * 40,
                RUN,
                root / "lease.json",
                lock_root=root / "locks",
                scope=NATIVE_SYSTEM_LOSS_SCOPE,
            )
            lease.acquire()
            raw = (json.dumps(control, sort_keys=True) + "\n").encode()
            source = root / "control.json"
            source.write_bytes(raw)
            self.assertEqual(lease.finish(control, hashlib.sha256(raw).hexdigest()), [])
            self.assertFalse(lease.lock.exists())

    def test_json_is_canonical_and_deterministic(self) -> None:
        host = self.ready()
        first = host.json_bytes()
        self.assertEqual(first, host.json_bytes())
        self.assertEqual(host.digest(), __import__("hashlib").sha256(first).hexdigest())
        self.assertNotIn(b" ", first)


if __name__ == "__main__":
    unittest.main()
