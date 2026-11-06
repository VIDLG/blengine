"""Peripheral-layer synthetic tests only: no USB, OS control, firmware or RF proof."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest.mock import patch

from . import direct_fixture, fixture_session
from . import ios_adapter_startup as api
from .btp_protocol import BtpFrame
from .btp_session import BtpSession
from .fault_control import Opcode, Phase
from .gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED
from .profile import load_profile
from .support.direct_fixture_resources import bind_btp_session, resources
from .support.read_fault_emulator import PEER, Clock, Wire
from .support.session_receipts import fake_attempt


class Harness:
    """Real DirectFixture lifecycle, receipt validation and BTP on a fake wire."""

    def __init__(self, root: Path) -> None:
        attempt = fake_attempt(root)
        self.plan, self.root = attempt.plan, attempt.root
        self.receipt = self.plan.receipt_path.read_bytes()
        self.plan.receipt_path.unlink()
        self.clock = Clock()
        self.wire = Wire(self.clock, "")
        self.session = BtpSession(self.wire)
        self.steps: list[str] = []
        self.stack = ExitStack()
        self.owner = api.IosAdapterStartup(self.plan, self.root)
        self.handles = {
            c.role: 51 + i
            for i, c in enumerate(load_profile(self.plan.profile_path).characteristics)
        }
        self.off = {
            "schema_version": 1,
            "run_id": self.plan.session_id,
            "native_bootstrap": {
                "run_id": self.plan.session_id,
                "adapter_mode": "startup-off",
                "pid": 42,
                "boot_id": "22222222-2222-4222-8222-222222222222",
                "bundle_id": api.BUNDLE_ID,
                "executable": "file:///synthetic.app/BleHubNitroHost",
                "source": "launch",
            },
            "runtime_nonce": "abc-123-def",
            "journal_sha256": "12" * 32,
            "resources": dict.fromkeys(api.RESOURCE_FIELDS, 0),
        }
        self.runtime = {
            "schema_version": 1,
            "platform": "ios",
            "scenario": "adapter-transition",
            "adapter_mode": "startup-off",
            "runtime_status": "passed",
            "run_id": self.plan.session_id,
            "service_uuid": self.plan.service_uuid,
            "native_bootstrap": copy.deepcopy(self.off["native_bootstrap"]),
            "runtime_nonce": self.off["runtime_nonce"],
            "journal_sha256": "34" * 32,
            "closed_resources": {
                "off": dict(self.off["resources"]),
                "on": dict(self.off["resources"]),
            },
        }

    def __enter__(self) -> Self:
        self.stack.enter_context(
            patch.object(api.time, "monotonic", self.clock.monotonic)
        )
        # Constructor deadlines use this same synthetic clock, not real monotonic.
        self.owner.preparing_deadline = self.clock.now + api.PREPARING_SECONDS
        self.owner.whole_deadline = self.clock.now + api.WHOLE_SECONDS
        self.resources = self.stack.enter_context(resources(self.steps))
        bind_btp_session(self.resources, self.session)
        self.resources.session.pop_event.side_effect = self.session.pop_event
        self.resources.session.poll_event.side_effect = self.session.poll_event
        direct_fixture.load_profile.side_effect = None
        direct_fixture.load_profile.return_value = load_profile(self.plan.profile_path)

        def attest(*args: object, **kwargs: object) -> None:
            self.steps.append("attest")
            self.plan.receipt_path.write_bytes(self.receipt)

        self.attest = self.stack.enter_context(
            patch.object(fixture_session, "attest_session", side_effect=attest)
        )
        self.doctor = self.stack.enter_context(
            patch.object(direct_fixture, "NrfDoctor")
        )
        self.doctor.return_value.ensure_services.side_effect = lambda: (
            self.steps.append("services")
        )
        self.doctor.return_value.run.side_effect = lambda _: (
            self.steps.append("doctor"),
            SimpleNamespace(role_handles=self.handles),
        )[1]
        self.resources.gap.start_advertising.side_effect = lambda *a: self.steps.append(
            "advertise"
        )

        def disconnect(address: str, kind: int) -> None:
            if self.owner.links.peer != (address, kind):
                raise AssertionError("foreign peer mutation")
            self.steps.append("disconnect")
            self.wire.gap(
                GAP_DEVICE_DISCONNECTED, bytes([kind]) + bytes.fromhex(address)[::-1]
            )

        self.resources.gap.disconnect.side_effect = disconnect
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self.owner.finish(None)
        finally:
            self.stack.close()

    def pair(self) -> None:
        self.wire.gap(GAP_DEVICE_CONNECTED)
        self.wire.gap(GAP_DEVICE_DISCONNECTED)

    def passed(self) -> dict:
        self.owner.start()
        self.ready = self.owner.open_on(self.off)
        self.pair()
        return self.owner.complete(self.runtime)

    def physical(self) -> dict:
        return json.loads((self.root / api.PHYSICAL_NAME).read_bytes())

    def verify(self, physical: dict, **changes: dict) -> dict:
        args = {
            "request": json.loads(self.plan.request_path.read_bytes()),
            "receipt": json.loads(self.plan.receipt_path.read_bytes()),
            "receipt_sha256": hashlib.sha256(
                self.plan.receipt_path.read_bytes()
            ).hexdigest(),
            "runtime": self.runtime,
            "readiness": self.ready,
        }
        args.update(changes)
        return api.verify_physical(physical, **args)


class IosAdapterStartupTests(unittest.TestCase):
    def test_one_inprocess_owner_exact_pair_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, Harness(Path(tmp)) as h:
            physical = h.passed()
            self.assertEqual(h.verify(physical), physical)
            self.assertEqual(h.owner.finish(None), [])
            self.assertEqual(h.physical(), physical)
            self.assertEqual(h.steps.count("acquire"), 1)
            self.assertEqual(h.steps.count("open"), 1)
            self.assertEqual(h.steps.count("session"), 1)
            self.assertEqual(h.steps.count("release"), 1)
            self.assertLess(h.steps.index("attest"), h.steps.index("services"))
            self.assertLess(h.steps.index("services"), h.steps.index("doctor"))
            self.assertEqual(h.steps.count("advertise"), 1)
            self.assertNotIn(
                "disconnect", h.steps
            )  # Cleanup did not produce the passing pair.
            h.resources.gatt.reset_server.assert_not_called()
            h.resources.gatt.set_value.assert_not_called()
            self.assertEqual({c.opcode for c in h.wire.commands}, {Opcode.GET_LIMITS})
            self.assertEqual(set(h.ready), api.READY_FIELDS)
            self.assertEqual(physical["fixture_pid"], os.getpid())
            self.assertEqual(
                physical["cleanup"],
                {"advertising_stopped": True, "hold_inactive": True, "peer_count": 0},
            )

    def test_failures_before_and_during_direct_initialization_release(self) -> None:
        for stage in ("constructor", "attest", "doctor"):
            with (
                self.subTest(stage=stage),
                tempfile.TemporaryDirectory() as tmp,
                Harness(Path(tmp)) as h,
            ):
                if stage == "constructor":
                    h.stack.enter_context(
                        patch.object(
                            direct_fixture,
                            "load_profile",
                            side_effect=RuntimeError("init failure"),
                        )
                    )
                elif stage == "attest":
                    h.attest.side_effect = RuntimeError("init failure")
                else:
                    h.doctor.return_value.run.side_effect = RuntimeError("init failure")
                with self.assertRaisesRegex(RuntimeError, "init failure"):
                    h.owner.start()
                self.assertTrue(h.owner.finished)
                self.assertEqual(h.steps.count("release"), 1)
                self.assertEqual(h.physical()["status"], "failed")
                self.assertIn("init failure", h.physical()["primary_error"])
                self.assertNotIn("disconnect", h.steps)
                h.resources.gatt.reset_server.assert_not_called()
                if stage != "doctor":
                    self.assertNotIn("advertising-stop", h.steps)
                    self.assertNotIn("doctor", h.steps)

    def test_fresh_session_rejects_existing_receipt_before_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, Harness(Path(tmp)) as h:
            h.plan.receipt_path.write_bytes(h.receipt)
            with self.assertRaisesRegex(ValueError, "fresh installed-image receipt"):
                api.IosAdapterStartup(h.plan, h.root)
            self.assertNotIn("open", h.steps)

    def test_busy_or_unowned_peer_blocks_provisioning(self) -> None:
        for failure in ("busy", "peer"):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as tmp,
                Harness(Path(tmp)) as h,
            ):
                if failure == "busy":
                    h.wire.phase = Phase.HELD
                else:
                    h.wire.gap(GAP_DEVICE_CONNECTED)
                with self.assertRaises((RuntimeError, ValueError)):
                    h.owner.start()
                self.assertNotIn("doctor", h.steps)
                self.assertNotIn("advertise", h.steps)
                self.assertNotIn("advertising-stop", h.steps)
                self.assertNotIn("disconnect", h.steps)
                self.assertEqual(h.physical()["status"], "failed")

    def test_off_exact_schema_and_identity_fail_closed(self) -> None:
        mutations = (
            lambda d: d.update(extra=1),
            lambda d: d.update(schema_version=True),
            lambda d: d.update(run_id="A" * 36),
            lambda d: d.update(journal_sha256="a" * 63),
            lambda d: d.update(runtime_nonce=""),
            lambda d: d["resources"].pop("watchdogs"),
            lambda d: d["resources"].update(watchdogs=False),
            lambda d: d["resources"].update(watchdogs=1),
            lambda d: d["native_bootstrap"].update(bundle_id="com.blehubnitrohost"),
            lambda d: d["native_bootstrap"].update(adapter_mode="active-loss"),
            lambda d: d["native_bootstrap"].update(pid=True),
            lambda d: d["native_bootstrap"].update(run_id="foreign"),
        )
        for mutate in mutations:
            with (
                self.subTest(mutation=mutate),
                tempfile.TemporaryDirectory() as tmp,
                Harness(Path(tmp)) as h,
            ):
                h.owner.start()
                off = copy.deepcopy(h.off)
                mutate(off)
                with self.assertRaises(ValueError):
                    h.owner.open_on(off)
                self.assertFalse(h.owner.links.allow_connection)
                self.assertFalse((h.root / api.READY_NAME).exists())

    def test_queued_peer_before_on_is_never_retroactively_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, Harness(Path(tmp)) as h:
            h.owner.start()
            h.wire.gap(GAP_DEVICE_CONNECTED)
            with self.assertRaisesRegex(RuntimeError, "early GAP connection"):
                h.owner.open_on(h.off)
            stops = h.steps.count("advertising-stop")
            errors = h.owner.finish(RuntimeError("parent failure"))
            self.assertTrue(errors)
            self.assertEqual(h.steps.count("advertising-stop"), stops)
            self.assertNotIn("disconnect", h.steps)
            self.assertIsNone(h.physical()["cleanup"]["peer_count"])
            self.assertIsNone(h.owner.links.peer)
            self.assertEqual(h.physical()["connections"], 0)

    def test_guard_drains_pre_on_gap_without_caller_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, Harness(Path(tmp)) as h:
            h.owner.start()
            h.wire.gap(GAP_DEVICE_DISCONNECTED)
            with self.assertRaisesRegex(RuntimeError, "early GAP disconnect"):
                h.owner.guard()
            self.assertEqual(h.owner.finish(None), h.owner.finish(None))
            self.assertNotIn("disconnect", h.steps)

    def test_malformed_duplicate_foreign_and_backlog_are_sticky(self) -> None:
        cases = ("index", "payload", "duplicate", "foreign", "backlog")
        for case in cases:
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as tmp,
                Harness(Path(tmp)) as h,
            ):
                h.owner.start()
                h.owner.open_on(h.off)
                if case == "backlog":
                    for _ in range(256):
                        h.session._dispatcher._events.append(BtpFrame(2, 128, 0, b""))
                else:
                    h.wire.gap(GAP_DEVICE_CONNECTED)
                    h.owner.guard()
                    if case == "duplicate":
                        h.wire.gap(GAP_DEVICE_CONNECTED)
                    elif case == "foreign":
                        h.wire.gap(
                            GAP_DEVICE_DISCONNECTED, bytes.fromhex("01010203040506")
                        )
                    else:
                        h.wire.frames.append(
                            BtpFrame(
                                1,
                                GAP_DEVICE_DISCONNECTED,
                                1 if case == "index" else 0,
                                PEER if case == "index" else PEER[:-1],
                            )
                        )
                with self.assertRaises(RuntimeError):
                    h.owner.guard()
                h.owner.finish(None)
                self.assertNotIn("disconnect", h.steps)
                self.assertIsNone(h.physical()["cleanup"]["peer_count"])

    def test_authorized_owned_peer_cleanup_is_failure_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, Harness(Path(tmp)) as h:
            h.owner.start()
            h.owner.open_on(h.off)
            h.wire.gap(GAP_DEVICE_CONNECTED)
            with self.assertRaisesRegex(ValueError, "exact GAP pair"):
                h.owner.complete(h.runtime)
            self.assertEqual(h.owner.finish(RuntimeError("parent timeout")), [])
            physical = h.physical()
            self.assertEqual(physical["status"], "failed")
            self.assertIn("exact GAP pair", physical["primary_error"])
            self.assertEqual(h.steps.count("disconnect"), 1)
            self.assertIn(
                "cleanup-owned-peer-disconnect",
                [b["boundary"] for b in physical["boundaries"]],
            )
            self.assertEqual(physical["cleanup"]["peer_count"], 0)
            self.assertEqual(physical["disconnections"], 1)

    def test_missing_pair_and_failed_runtime_cannot_pass(self) -> None:
        for case in ("no-gap", "failed", "nonce", "boot", "resources", "service"):
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as tmp,
                Harness(Path(tmp)) as h,
            ):
                h.owner.start()
                h.owner.open_on(h.off)
                if case != "no-gap":
                    h.pair()
                if case == "failed":
                    h.runtime["runtime_status"] = "failed"
                elif case == "nonce":
                    h.runtime["runtime_nonce"] = "other-123-nonce"
                elif case == "boot":
                    h.runtime["native_bootstrap"]["pid"] += 1
                elif case == "resources":
                    h.runtime["closed_resources"]["on"]["watchdogs"] = 1
                elif case == "service":
                    h.runtime["service_uuid"] = "foreign"
                with self.assertRaises(ValueError):
                    h.owner.complete(h.runtime)
                h.owner.finish(None)
                self.assertEqual(h.physical()["status"], "failed")

    def test_authority_receipt_identity_lease_and_deadlines(self) -> None:
        for case in (
            "receipt",
            "process",
            "lease",
            "preparing",
            "whole",
            "abort",
            "request",
        ):
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as tmp,
                Harness(Path(tmp)) as h,
            ):
                h.owner.start()
                if case == "whole":
                    h.owner.open_on(h.off)
                    h.clock.now = h.owner.whole_deadline
                elif case == "receipt":
                    h.plan.receipt_path.write_bytes(h.receipt + b"\n")
                elif case == "process":
                    h.owner.process = (1, 2)
                elif case == "lease":
                    h.resources.owner.require.side_effect = RuntimeError("lease lost")
                elif case == "preparing":
                    h.clock.now = h.owner.preparing_deadline
                elif case == "abort":
                    (h.root / "abort").touch()
                else:
                    h.plan.request_path.chmod(0o600)
                    h.plan.request_path.write_bytes(
                        h.plan.request_path.read_bytes().replace(
                            b'"schema_version": 1', b'"schema_version": 2'
                        )
                    )
                with self.assertRaises((ValueError, RuntimeError, TimeoutError)):
                    h.owner.guard()
                stops = h.steps.count("advertising-stop")
                h.owner.finish(None)
                if case in ("receipt", "process", "lease", "request"):
                    self.assertEqual(h.steps.count("advertising-stop"), stops)
                self.assertEqual(h.physical()["status"], "failed")
                self.assertEqual(h.steps.count("release"), 1)

    def test_readiness_immutable_and_callers_do_not_alias_owner_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, Harness(Path(tmp)) as h:
            h.owner.start()
            ready = h.owner.open_on(h.off)
            ready["native_bootstrap"]["pid"] = 99
            h.off["resources"]["watchdogs"] = 99
            self.assertEqual(h.owner.readiness["native_bootstrap"]["pid"], 42)
            self.assertEqual(h.owner.off["resources"]["watchdogs"], 0)
            path = h.root / api.READY_NAME
            path.chmod(0o600)
            path.write_bytes(path.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "immutable readiness"):
                h.owner.guard()
            h.owner.finish(None)
            self.assertEqual(h.physical()["status"], "failed")

    def test_primary_and_cleanup_errors_remain_separate_and_resources_close(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp, Harness(Path(tmp)) as h:
            h.owner.start()
            h.resources.gap.stop_advertising.side_effect = RuntimeError(
                "no advertising ACK"
            )
            errors = h.owner.finish(KeyboardInterrupt("parent cancelled"))
            self.assertTrue(any("no advertising ACK" in e for e in errors))
            self.assertEqual(
                h.physical()["primary_error"], "KeyboardInterrupt: parent cancelled"
            )
            self.assertFalse(h.physical()["cleanup"]["advertising_stopped"])
            self.assertEqual(h.steps.count("release"), 1)
            h.resources.gatt.reset_server.assert_not_called()

    def test_resource_close_failure_downgrades_previously_completed_physical(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp, Harness(Path(tmp)) as h:
            h.passed()
            close = h.owner.fixture.stop

            def failing_close() -> None:
                close()
                raise RuntimeError("lease close failed")

            with patch.object(h.owner.fixture, "stop", side_effect=failing_close):
                errors = h.owner.finish(None)
            self.assertTrue(any("lease close failed" in error for error in errors))
            self.assertEqual(h.physical()["status"], "failed")
            self.assertIsNone(h.physical()["primary_error"])
            self.assertEqual(h.steps.count("release"), 1)
            with self.assertRaises(ValueError):
                h.verify(h.physical())

    def test_final_extra_peer_never_becomes_a_cleanup_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, Harness(Path(tmp)) as h:
            h.owner.start()
            h.owner.open_on(h.off)
            h.pair()

            def extra_peer() -> None:
                h.steps.append("advertising-stop")
                h.wire.gap(GAP_DEVICE_CONNECTED)

            h.resources.gap.stop_advertising.side_effect = extra_peer
            with self.assertRaisesRegex(ValueError, "physical cleanup failed"):
                h.owner.complete(h.runtime)
            h.owner.finish(None)
            self.assertEqual(h.physical()["status"], "failed")
            self.assertIsNone(h.physical()["cleanup"]["peer_count"])
            self.assertNotIn("disconnect", h.steps)

    def test_no_cleanup_mutation_on_foreign_fault_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, Harness(Path(tmp)) as h:
            h.owner.start()
            stops = h.steps.count("advertising-stop")
            h.wire.phase = Phase.HELD
            errors = h.owner.finish(RuntimeError("primary"))
            self.assertTrue(errors)
            self.assertEqual(h.steps.count("advertising-stop"), stops)
            self.assertNotIn("disconnect", h.steps)
            self.assertFalse(h.physical()["cleanup"]["hold_inactive"])
            self.assertEqual({c.opcode for c in h.wire.commands}, {Opcode.GET_LIMITS})

    def test_offline_replay_rejects_cross_attempt_and_forged_success(self) -> None:
        mutations = (
            lambda d: d.update(run_id="foreign"),
            lambda d: d.update(fixture_pid=True),
            lambda d: d.update(receipt_sha256="ff" * 32),
            lambda d: d.update(contract_sha256="ff" * 32),
            lambda d: d.update(status="failed"),
            lambda d: d.update(primary_error="timeout"),
            lambda d: d["cleanup_errors"].append("cleanup failed"),
            lambda d: d["cleanup"].update(hold_inactive=False),
            lambda d: d["resources"]["on"].update(watchdogs=True),
            lambda d: d["resources"]["on"].update(watchdogs=False),
            lambda d: d["cleanup"].update(peer_count=False),
            lambda d: d["cleanup"].update(advertising_stopped=1),
            lambda d: d["native_bootstrap"].update(pid=42.0),
            lambda d: d["off"].update(journal_sha256="ee" * 32),
            lambda d: d["readiness"].update(runtime_nonce="other-123-nonce"),
            lambda d: d.update(runtime_journal_sha256="ff" * 32),
            lambda d: d["events"].pop(),
            lambda d: d.update(disconnections=True),
            lambda d: d["events"][0].update(monotonic=0),
            lambda d: d["events"][1].update(peer=["010203040506", 1]),
            lambda d: d["events"][1].update(payload_hex="ff" * 7),
            lambda d: d["boundaries"].insert(
                4, {"boundary": "cleanup-owned-peer-disconnect", "monotonic": 100}
            ),
            lambda d: d["boundaries"][-1].update(peer_count=None),
            lambda d: d["hold_status"].update(engine_flags=4),
            lambda d: d.update(handles={"read-write": True}),
        )
        with tempfile.TemporaryDirectory() as tmp, Harness(Path(tmp)) as h:
            physical = h.passed()
            for mutate in mutations:
                with self.subTest(mutation=mutate):
                    changed = copy.deepcopy(physical)
                    mutate(changed)
                    with self.assertRaises(ValueError):
                        h.verify(changed)
            for key in ("receipt_sha256", "runtime", "readiness"):
                changed = copy.deepcopy(
                    {
                        "receipt_sha256": "00" * 32,
                        "runtime": h.runtime,
                        "readiness": h.ready,
                    }[key]
                )
                if key == "runtime":
                    changed["runtime_nonce"] = "foreign-123-nonce"
                elif key == "readiness":
                    changed["off_journal_sha256"] = "ff" * 32
                with self.subTest(input=key), self.assertRaises(ValueError):
                    h.verify(physical, **{key: changed})


if __name__ == "__main__":
    unittest.main()
