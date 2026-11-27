from __future__ import annotations

import json
import os
import struct
import tempfile
import time
import unittest
from builtins import BaseExceptionGroup
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from tests.peripherals.nrf52840 import (
    direct_fixture,
    doctor,
    fixture_session,
    resource_lease,
)
from tests.peripherals.nrf52840.btp_commands import GAP, BtpCommandError
from tests.peripherals.nrf52840.btp_protocol import BtpFrame, BtpSessionError
from tests.peripherals.nrf52840.btp_session import BtpSession
from tests.peripherals.nrf52840.btp_transport import BtpTransportError, FramedTransport
from tests.peripherals.nrf52840.fault_control import FaultWireError
from tests.peripherals.nrf52840.fixture_attestation import parse_attestation
from tests.peripherals.nrf52840.fixture_attestation_wire import encode_attestation
from tests.peripherals.nrf52840.gap_commands import (
    CONTROLLER_INFO,
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
    GapCommands,
)
from tests.peripherals.nrf52840.profile import load_profile
from tests.peripherals.nrf52840.resource_lease import ResourceLeaseError
from tests.peripherals.nrf52840.serial_transport import SerialByteTransport
from tests.peripherals.nrf52840.support.direct_fixture_resources import (
    ServiceWire,
    resources,
)

ADVERTISING = 1 << 10  # Zephyr tests/bluetooth/tester/src/btp/btp_gap.h


class PassiveWire(ServiceWire):
    def __init__(self) -> None:
        super().__init__([])
        self.supported = ADVERTISING
        self.current = ADVERTISING
        self.stop_error: BaseException | None = None
        self.stop_status: bytes | None = None
        self.info_payload: bytes | None = None
        self.on_stop = lambda: None
        self.on_limits = lambda: None
        self.peer = False
        self.reboot = False
        self.keep_advertising = False

    def send(self, frame: BtpFrame) -> None:
        if frame.service == GAP and frame.opcode in (3, 5, 6, 8, 10, 11):
            self.sent.append(frame)
            if frame.opcode == 11:
                self.events.append("advertising-stop")
                if self.stop_error is not None:
                    raise self.stop_error
                if self.stop_status is not None:
                    self.frames.append(BtpFrame(GAP, 0, 0, self.stop_status))
                    return
                if not self.keep_advertising:
                    self.current &= ~ADVERTISING
                self.on_stop()
            elif frame.opcode == 10:
                self.current |= ADVERTISING
            if frame.opcode == 3:
                payload = self.info_payload
                if payload is None:
                    payload = CONTROLLER_INFO.pack(
                        bytes(6),
                        self.supported,
                        self.current,
                        bytes(3),
                        bytes(249),
                        bytes(11),
                    )
            else:
                payload = struct.pack("<I", self.current)
            self.frames.append(BtpFrame(GAP, frame.opcode, 0, payload))
            return
        super().send(frame)
        if (frame.service, frame.opcode) == (255, 0x10):
            self.on_limits()
            response = self.frames.pop()
            payload = bytearray(response.payload)
            if self.peer and len(payload) == 61:
                fields = list(struct.unpack_from("<BBBBBHIIIIB7sIII", payload, 18))
                fields[10:] = [1, b"\x00\x01\x02\x03\x04\x05\x06", 1, 1, 1]
                struct.pack_into("<BBBBBHIIIIB7sIII", payload, 18, *fields)
            if self.reboot:
                payload[2:18] = b"z" * 16
            self.frames.append(
                BtpFrame(
                    response.service, response.opcode, response.index, bytes(payload)
                )
            )


class PassiveCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        firmware = Path(__file__).parent / "firmware"
        profile_path = firmware / "profiles/multifunction-rf-v2-b.json"
        profile = load_profile(profile_path)
        declaration = json.loads(
            (firmware / "images/multifunction-rf-v2-b.json").read_bytes()
        )
        declaration.pop("settings")
        declaration.update(
            image_sha256="ab" * 32,
            service_uuid=profile.service_uuid,
            local_name=profile.local_name,
        )
        contract = root / "contract.json"
        contract.write_text(json.dumps(declaration), encoding="utf-8")
        with patch.object(
            fixture_session, "identity_for_port", return_value="jlink:123456"
        ):
            self.plan = fixture_session.prepare_session(
                contract,
                profile_path,
                root / "attempt",
                port="test-port",
                service_uuid=profile.service_uuid,
            )
        declaration.pop("fixture_role")
        self.wire = PassiveWire()
        self.wire.attestation = encode_attestation(parse_attestation(declaration))
        self.resources = self.enterContext(resources(self.wire.events))
        self.lease_error: BaseException | None = None
        self.session_error: BaseException | None = None
        lease_scope = self.resources.port_lease.side_effect
        session_close = BtpSession.close

        @contextmanager
        def lease(*args, **kwargs):
            with lease_scope(*args, **kwargs) as owner:
                yield owner
            if self.lease_error is not None:
                raise self.lease_error

        def close_session(session):
            session_close(session)
            if self.session_error is not None:
                raise self.session_error

        self.resources.port_lease.side_effect = lease
        self.enterContext(patch.object(BtpSession, "close", close_session))
        # Use the real session, framing, command wrappers and SerialByteTransport.
        # Only the actual serial port and physical lease are synthetic.
        self.port = Mock(timeout=1.0, write_timeout=1.0)
        self.port.in_waiting = 0
        self.pending: deque[int] = deque()
        self.clock = None
        self.read_delay = 0.0
        self.port.write.side_effect = self.write
        self.port.read.side_effect = self.read
        self.port.close.side_effect = lambda: self.resources.step("serial-close")
        self.serial = SerialByteTransport(self.port)
        for name, value in (
            ("BtpSession", BtpSession),
            ("FramedTransport", FramedTransport),
            ("GapCommands", GapCommands),
            ("load_profile", load_profile),
        ):
            self.enterContext(patch.object(direct_fixture, name, value))
        self.enterContext(
            patch.object(direct_fixture, "open_serial", return_value=self.serial)
        )
        self.provision = self.enterContext(
            patch.object(doctor, "provision", return_value={"read-write": 51})
        )
        self.fixture = direct_fixture.DirectFixture(
            self.plan.port, self.plan.profile_path, fixture_session=self.plan
        )
        # Assertion failures must never fall back to legacy destructive cleanup.
        self.addCleanup(self.fixture._resources.close)

    def write(self, data: bytes) -> int:
        self.resources.step("serial-write")
        self.wire.send(BtpFrame.decode(data))
        return len(data)

    def read(self, size: int) -> bytes:
        self.resources.step("serial-read")
        if self.clock is not None:
            self.clock[0] += min(self.read_delay, self.port.timeout)
        if not self.pending:
            if not self.wire.frames:
                # A real serial read with a timeout returns empty when the tester is idle.
                return b""
            self.pending.extend(self.wire.frames.popleft().encode())
        return bytes(
            self.pending.popleft() for _ in range(min(size, len(self.pending)))
        )

    def start(self) -> None:
        self.fixture.start()
        fixture_session.verify_receipt(
            self.plan, fixture_identity=(os.getpid(), os.getpgid(0))
        )
        self.wire.sent.clear()
        self.wire.events[:] = ["acquire"]

    def close(self):
        return self.fixture.stop_advertising_and_close(deadline=time.monotonic() + 5)

    def assert_closed_once(self, *, stops: int) -> None:
        before = list(self.wire.sent)
        self.fixture.stop()
        self.fixture.stop()
        self.assertEqual(before, self.wire.sent)
        self.assertEqual(
            sum(f.service == GAP and f.opcode == 11 for f in before), stops
        )
        self.assertTrue(
            all(
                (f.service, f.opcode) in {(255, 0x10), (GAP, 11), (GAP, 3)}
                for f in before
            )
        )
        self.resources.gatt.reset_server.assert_not_called()
        self.port.close.assert_called_once()
        self.assertEqual(self.wire.events.count("release"), 1)
        self.assertEqual(self.wire.events[-1], "release")
        with self.assertRaisesRegex(RuntimeError, "already.*stop|already.*clos"):
            self.close()
        self.assertEqual(before, self.wire.sent)

    def assert_resource_only_closed(self) -> None:
        before = list(self.wire.sent)
        self.assertIsNone(self.fixture.close_resources())
        self.fixture.stop()
        self.fixture.stop()
        with self.assertRaisesRegex(RuntimeError, "already.*attempted|already.*stop"):
            self.fixture.start()
        self.assertEqual(self.wire.sent, before)
        self.port.close.assert_called_once()
        self.assertEqual(self.wire.events.count("release"), 1)
        self.assertEqual(self.wire.events[-1], "release")
        self.resources.gatt.reset_server.assert_not_called()
        self.assertFalse(self.fixture._started)
        self.assertFalse(self.fixture._mutation_allowed)

    def refuse_start_deadline(self, deadline: object) -> None:
        with self.assertRaises(ValueError):
            self.fixture.start(deadline=deadline)
        self.port.write.assert_not_called()
        self.port.read.assert_not_called()
        self.assert_resource_only_closed()

    def test_start_nan_deadline_is_rejected_before_io(self) -> None:
        self.refuse_start_deadline(float("nan"))

    def test_start_infinite_deadline_is_rejected_before_io(self) -> None:
        self.refuse_start_deadline(float("inf"))

    def test_start_negative_infinite_deadline_is_rejected_before_io(self) -> None:
        self.refuse_start_deadline(float("-inf"))

    def test_start_bool_deadline_is_rejected_before_io(self) -> None:
        self.refuse_start_deadline(True)

    def test_start_false_deadline_is_rejected_before_io(self) -> None:
        self.refuse_start_deadline(False)

    def test_start_nonnumeric_deadline_is_rejected_before_io(self) -> None:
        self.refuse_start_deadline("100.0")

    def test_start_expired_deadline_is_sticky_and_never_touches_wire(self) -> None:
        with self.assertRaises(TimeoutError):
            self.fixture.start(deadline=time.monotonic() - 1)
        self.port.write.assert_not_called()
        self.port.read.assert_not_called()
        self.assert_resource_only_closed()

    def test_start_late_hook_cannot_provision_or_send_later_mutations(self) -> None:
        self.clock = [100.0]

        def late_hook() -> None:
            self.clock[0] = 101.0

        with (
            patch.object(
                direct_fixture.time, "monotonic", side_effect=lambda: self.clock[0]
            ),
            self.assertRaises(TimeoutError),
        ):
            self.fixture.start(deadline=101.0, before_mutations=late_hook)
        self.provision.assert_not_called()
        self.assertFalse(
            any(f.service == GAP and f.opcode == 11 for f in self.wire.sent)
        )
        self.assert_resource_only_closed()

    def test_start_fragmented_reads_share_one_absolute_deadline(self) -> None:
        self.clock = [100.0]
        self.read_delay = 0.01
        with (
            patch.object(
                direct_fixture.time, "monotonic", side_effect=lambda: self.clock[0]
            ),
            self.assertRaises(TimeoutError),
        ):
            self.fixture.start(deadline=100.05)
        self.assertGreater(self.port.read.call_count, 1)
        self.assertLessEqual(self.port.write_timeout, 0.051)
        self.assertLessEqual(self.clock[0], 100.051)
        self.assertEqual(len(self.wire.sent), 1)
        self.provision.assert_not_called()
        self.assert_resource_only_closed()

    def test_start_late_write_stops_before_read_or_next_command(self) -> None:
        self.clock = [100.0]

        def late_write(data: bytes) -> int:
            self.assertLessEqual(self.port.write_timeout, 0.051)
            result = self.write(data)
            self.clock[0] = 100.05
            return result

        self.port.write.side_effect = late_write
        with (
            patch.object(
                direct_fixture.time, "monotonic", side_effect=lambda: self.clock[0]
            ),
            self.assertRaises(TimeoutError),
        ):
            self.fixture.start(deadline=100.05)
        self.port.write.assert_called_once()
        self.port.read.assert_not_called()
        self.assert_resource_only_closed()

    def test_start_short_write_is_not_retried_or_followed_by_teardown_commands(
        self,
    ) -> None:
        self.port.write.side_effect = lambda data: len(data) - 1
        with self.assertRaisesRegex(BtpTransportError, "serial write was short"):
            self.fixture.start(deadline=time.monotonic() + 5)
        self.port.write.assert_called_once()
        self.port.read.assert_not_called()
        self.assert_resource_only_closed()

    def test_start_timeout_keeps_automatic_close_failure_and_releases_lease(
        self,
    ) -> None:
        secondary = RuntimeError("serial close failed")
        self.port.close.side_effect = secondary
        self.clock = [100.0]
        self.read_delay = 0.01
        with (
            patch.object(
                direct_fixture.time, "monotonic", side_effect=lambda: self.clock[0]
            ),
            self.assertRaises(BaseExceptionGroup) as caught,
        ):
            self.fixture.start(deadline=100.05)
        self.assertEqual(len(caught.exception.exceptions), 2)
        self.assertIsInstance(caught.exception.exceptions[0], TimeoutError)
        self.assertIs(caught.exception.exceptions[1], secondary)
        self.assert_resource_only_closed()

    def test_start_interruption_keeps_all_independent_cleanup_errors(self) -> None:
        primary = KeyboardInterrupt("startup interrupted")
        serial_error = RuntimeError("serial close failed")
        self.port.write.side_effect = primary
        self.port.close.side_effect = serial_error
        self.session_error = KeyboardInterrupt("session close interrupted")
        self.lease_error = ResourceLeaseError("lease release failed")
        with self.assertRaises(BaseExceptionGroup) as caught:
            self.fixture.start(deadline=time.monotonic() + 5)
        self.assertEqual(
            caught.exception.exceptions,
            (primary, serial_error, self.session_error, self.lease_error),
        )
        self.assert_resource_only_closed()

    def test_start_provision_failure_never_falls_back_to_legacy_stop(self) -> None:
        primary = RuntimeError("profile provision failed")
        self.provision.side_effect = primary
        with self.assertRaises(RuntimeError) as caught:
            self.fixture.start(deadline=time.monotonic() + 5)
        self.assertIs(caught.exception, primary)
        self.assertEqual(
            sum(f.service == GAP and f.opcode == 11 for f in self.wire.sent), 1
        )
        self.assert_resource_only_closed()

    def test_start_late_provision_does_not_send_next_mutation(self) -> None:
        self.clock = [100.0]

        def late_provision(*args) -> dict[str, int]:
            self.clock[0] = 101.0
            return {"read-write": 51}

        self.provision.side_effect = late_provision
        with (
            patch.object(
                direct_fixture.time, "monotonic", side_effect=lambda: self.clock[0]
            ),
            self.assertRaises(TimeoutError),
        ):
            self.fixture.start(deadline=101.0)
        self.assertEqual(
            sum(f.service == GAP and f.opcode == 11 for f in self.wire.sent), 1
        )
        self.assert_resource_only_closed()

    def test_start_late_advertising_response_cannot_return_success_or_stop_again(
        self,
    ) -> None:
        self.clock = [100.0]
        write = self.write

        def late_advertising(data: bytes) -> int:
            result = write(data)
            frame = BtpFrame.decode(data)
            if frame.service == GAP and frame.opcode == 10:
                self.clock[0] = 101.0
            return result

        self.port.write.side_effect = late_advertising
        with (
            patch.object(
                direct_fixture.time, "monotonic", side_effect=lambda: self.clock[0]
            ),
            self.assertRaises(TimeoutError),
        ):
            self.fixture.start(deadline=101.0)
        self.assertEqual(
            (self.wire.sent[-1].service, self.wire.sent[-1].opcode), (GAP, 10)
        )
        self.assertEqual(self.wire.current & ADVERTISING, ADVERTISING)
        self.assert_resource_only_closed()

    def test_start_eof_after_hook_preserves_primary_and_all_cleanup_failures(
        self,
    ) -> None:
        primary = EOFError("startup stream lost after admission hook")
        serial_error = RuntimeError("serial close failed")

        def hook() -> None:
            self.port.read.side_effect = primary
            self.port.close.side_effect = serial_error
            self.session_error = RuntimeError("session close failed")
            self.lease_error = ResourceLeaseError("lease release failed")

        with self.assertRaises(BaseExceptionGroup) as caught:
            self.fixture.start(deadline=time.monotonic() + 5, before_mutations=hook)
        self.assertEqual(
            caught.exception.exceptions,
            (primary, serial_error, self.session_error, self.lease_error),
        )
        self.assertEqual(
            sum(f.service == GAP and f.opcode == 11 for f in self.wire.sent), 1
        )
        self.provision.assert_not_called()
        self.assert_resource_only_closed()

    def test_start_broker_check_cannot_grant_an_expired_write_budget(self) -> None:
        self.clock = [100.0]

        def late_guard(*args) -> None:
            self.clock[0] = 101.0

        self.resources.owner.require.side_effect = late_guard
        with (
            patch.object(
                direct_fixture.time, "monotonic", side_effect=lambda: self.clock[0]
            ),
            self.assertRaises(TimeoutError),
        ):
            self.fixture.start(deadline=101.0)
        self.port.write.assert_not_called()
        self.port.read.assert_not_called()
        self.assert_resource_only_closed()

    def successful_deadline_handoff(self, write_timeout: float | None) -> None:
        self.port.write_timeout = write_timeout
        self.clock = [100.0]
        with patch.object(
            direct_fixture.time, "monotonic", side_effect=lambda: self.clock[0]
        ):
            self.assertEqual(self.fixture.start(deadline=100.5), {"read-write": 51})
            self.assertEqual(self.port.write_timeout, write_timeout)
            self.clock[0] = 110.0
            self.fixture.require_active_session(self.plan)
            # A real post-handoff command must not retain the expired startup budget.
            self.assertEqual(
                len(self.fixture._gap.read_controller_info()), CONTROLLER_INFO.size
            )
            self.wire.sent.clear()
            self.close()
        self.assert_closed_once(stops=1)

    def test_start_success_restores_finite_write_timeout_for_handoff(self) -> None:
        self.successful_deadline_handoff(1.0)

    def test_start_success_restores_unbounded_write_timeout_for_handoff(self) -> None:
        self.successful_deadline_handoff(None)

    def test_close_resources_unstarted_never_issues_commands(self) -> None:
        self.assertIsNone(self.fixture.close_resources())
        self.port.write.assert_not_called()
        self.port.read.assert_not_called()
        self.assert_resource_only_closed()

    def test_close_resources_started_does_not_claim_or_change_advertising(self) -> None:
        self.start()
        self.assertEqual(self.wire.current & ADVERTISING, ADVERTISING)
        self.assertIsNone(self.fixture.close_resources())
        self.assertEqual(self.wire.sent, [])
        self.assertEqual(self.wire.current & ADVERTISING, ADVERTISING)
        self.assert_resource_only_closed()
        with self.assertRaisesRegex(RuntimeError, "already.*stop|already.*clos"):
            self.close()

    def test_close_resources_preserves_all_errors_and_is_one_shot(self) -> None:
        self.start()
        serial_error = RuntimeError("serial close failed")
        self.port.close.side_effect = serial_error
        self.session_error = KeyboardInterrupt("session close interrupted")
        self.lease_error = ResourceLeaseError("lease release failed")
        with self.assertRaises(BaseExceptionGroup) as caught:
            self.fixture.close_resources()
        self.assertEqual(
            caught.exception.exceptions,
            (serial_error, self.session_error, self.lease_error),
        )
        self.assertEqual(self.wire.sent, [])
        self.assert_resource_only_closed()

    def test_same_live_session_readback_receipt_and_one_shot_close(self) -> None:
        self.start()
        before = self.plan.receipt_path.read_bytes()
        receipt = self.close()
        self.assertEqual(receipt.session_id, self.plan.session_id)
        self.assertEqual(receipt.resource_identity, self.plan.resource_identity)
        self.assertEqual(receipt.boot_nonce, bytes(range(1, 17)).hex())
        self.assertTrue(receipt.advertising_inactive)
        self.assertTrue(receipt.resources_closed)
        self.assertEqual(
            receipt.proof_method, "btp-gap-controller-info-advertising-bit-clear"
        )
        self.assertEqual(receipt.current_settings & ADVERTISING, 0)
        self.assertEqual(self.plan.receipt_path.read_bytes(), before)
        self.assertEqual(
            [(f.service, f.opcode) for f in self.wire.sent],
            [(255, 0x10), (1, 11), (1, 3), (255, 0x10)],
        )
        self.assert_closed_once(stops=1)

    def test_unstarted_even_with_receipt_is_refused_and_closed(self) -> None:
        self.plan.receipt_path.write_text("historical receipt", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "active|attested"):
            self.close()
        self.assert_closed_once(stops=0)

    def test_missing_attested_plan_is_refused(self) -> None:
        self.start()
        self.fixture._fixture_session = None
        with self.assertRaisesRegex(RuntimeError, "attested"):
            self.close()
        self.assert_closed_once(stops=0)

    def test_session_replacement_is_not_authorized_by_old_attestation(self) -> None:
        self.start()
        self.fixture._session = Mock()
        with self.assertRaisesRegex(RuntimeError, "attested|session"):
            self.close()
        self.assert_closed_once(stops=0)

    def test_lost_lease_never_sends_stop(self) -> None:
        self.start()
        self.resources.owner.require.side_effect = ResourceLeaseError("broker lost")
        with self.assertRaisesRegex(ResourceLeaseError, "broker lost"):
            self.close()
        self.assert_closed_once(stops=0)

    def test_live_peer_is_refused_without_disconnect(self) -> None:
        self.start()
        self.wire.peer = True
        with self.assertRaisesRegex(RuntimeError, "peer"):
            self.close()
        self.assert_closed_once(stops=0)

    def test_unknown_fault_state_is_refused(self) -> None:
        self.start()
        self.wire.fault = "unknown"
        with self.assertRaises(BtpCommandError):
            self.close()
        self.assert_closed_once(stops=0)

    def test_busy_engine_is_refused(self) -> None:
        self.start()
        self.wire.fault = "foreign"
        with self.assertRaisesRegex(FaultWireError, "busy|quarantined"):
            self.close()
        self.assert_closed_once(stops=0)

    def test_boot_changed_since_start_is_refused(self) -> None:
        self.start()
        self.wire.reboot = True
        with self.assertRaisesRegex(RuntimeError, "boot"):
            self.close()
        self.assert_closed_once(stops=0)

    def test_peer_racing_with_stop_is_failure_not_cleanup_pass(self) -> None:
        self.start()
        self.wire.on_stop = lambda: setattr(self.wire, "peer", True)
        with self.assertRaisesRegex(RuntimeError, "peer"):
            self.close()
        self.assert_closed_once(stops=1)

    def test_malformed_queued_peer_event_is_not_ignored(self) -> None:
        self.start()
        self.wire.frames.append(BtpFrame(GAP, 0x82, 0, b""))
        with self.assertRaisesRegex(RuntimeError, "peer|GAP"):
            self.close()
        self.assert_closed_once(stops=0)

    def test_settle_drains_a_peer_pair_the_scene_never_read(self) -> None:
        """A central that connected and left through another stack leaves two events.

        Nothing in this process ever read the queue, so `observed_peers()` is empty even
        though both GAP events are still pending. Settling pumps the queue first, which
        is the only order that can notice them, and then the one-shot close runs.
        """
        self.start()
        address = bytes([1]) + bytes(6)
        self.wire.frames.append(BtpFrame(GAP, GAP_DEVICE_CONNECTED, 0, address))
        self.wire.frames.append(BtpFrame(GAP, GAP_DEVICE_DISCONNECTED, 0, address))
        self.fixture.settle_no_peer(deadline=time.monotonic() + 5)
        self.assertEqual(self.fixture.observed_peers(), frozenset())
        receipt = self.close()
        self.assertIsInstance(receipt, direct_fixture.AdvertisingCloseReceipt)
        self.assert_closed_once(stops=1)

    def test_settle_clears_a_cached_peer_whose_disconnect_was_queued(self) -> None:
        """The cached peer is evidence this owner has not read its own queue yet."""
        self.start()
        address = bytes([1]) + bytes(6)
        self.fixture._connected_peers.add((address[1:7][::-1].hex(), address[0]))
        self.wire.frames.append(BtpFrame(GAP, GAP_DEVICE_DISCONNECTED, 0, address))
        self.fixture.settle_no_peer(deadline=time.monotonic() + 5)
        self.assertEqual(self.fixture.observed_peers(), frozenset())
        self.close()
        self.assert_closed_once(stops=1)

    def test_settle_refuses_to_wait_out_a_peer_that_stays_connected(self) -> None:
        """Settling is a wait for a fact, never a way to force a close through."""
        self.start()
        address = bytes([1]) + bytes(6)
        self.wire.peer = True
        self.wire.frames.append(BtpFrame(GAP, GAP_DEVICE_CONNECTED, 0, address))
        with self.assertRaisesRegex(RuntimeError, "peer never disconnected"):
            self.fixture.settle_no_peer(deadline=time.monotonic() + 5)
        with self.assertRaisesRegex(RuntimeError, "peer"):
            self.close()
        self.assert_closed_once(stops=0)

    def test_btp_rejection_is_not_swallowed(self) -> None:
        self.start()
        self.wire.stop_status = b"\x01"
        with self.assertRaisesRegex(BtpCommandError, "status 0x01"):
            self.close()
        self.assert_closed_once(stops=1)

    def test_unsupported_readback_is_explicit_gap(self) -> None:
        self.start()
        self.wire.supported = 0
        with self.assertRaisesRegex(
            direct_fixture.AdvertisingStateUnverified,
            "gap.*ADVERTISING|ADVERTISING.*gap",
        ):
            self.close()
        self.assert_closed_once(stops=1)

    def test_active_readback_is_failure(self) -> None:
        self.start()
        self.wire.keep_advertising = True
        with self.assertRaisesRegex(RuntimeError, "still advertising"):
            self.close()
        self.assert_closed_once(stops=1)

    def test_malformed_readback_is_failure(self) -> None:
        self.start()
        self.wire.info_payload = bytes(CONTROLLER_INFO.size + 1)
        with self.assertRaisesRegex(BtpCommandError, "length"):
            self.close()
        self.assert_closed_once(stops=1)

    def test_expired_deadline_closes_without_wire(self) -> None:
        self.start()
        with self.assertRaises(TimeoutError):
            self.fixture.stop_advertising_and_close(deadline=time.monotonic() - 1)
        self.assert_closed_once(stops=0)

    def test_fragmented_reads_share_deadline_and_bound_write_timeout(self) -> None:
        self.start()
        self.clock = [100.0]
        self.read_delay = 0.01
        with (
            patch.object(
                direct_fixture.time, "monotonic", side_effect=lambda: self.clock[0]
            ),
            self.assertRaises(TimeoutError),
        ):
            self.fixture.stop_advertising_and_close(deadline=100.05)
        self.assertLessEqual(self.port.write_timeout, 0.051)
        self.assertLessEqual(self.clock[0], 100.051)
        self.assert_closed_once(stops=0)

    def test_primary_and_all_resource_errors_survive_independent_release(self) -> None:
        self.start()
        primary = BtpSessionError("stop failed")
        serial_error = RuntimeError("serial close failed")
        self.lease_error = ResourceLeaseError("lease release failed")
        self.session_error = RuntimeError("session close failed")
        self.wire.stop_error = primary
        self.port.close.side_effect = serial_error
        with self.assertRaises(BaseExceptionGroup) as caught:
            self.close()
        self.assertEqual(
            caught.exception.exceptions,
            (primary, serial_error, self.session_error, self.lease_error),
        )
        self.assert_closed_once(stops=1)

    def test_timeout_primary_survives_automatic_session_close_failure(self) -> None:
        self.start()
        secondary = RuntimeError("serial close failed")
        self.port.close.side_effect = secondary
        self.clock = [100.0]
        self.read_delay = 0.01
        with (
            patch.object(
                direct_fixture.time, "monotonic", side_effect=lambda: self.clock[0]
            ),
            self.assertRaises(BaseExceptionGroup) as caught,
        ):
            self.fixture.stop_advertising_and_close(deadline=100.05)
        self.assertEqual(len(caught.exception.exceptions), 2)
        self.assertIsInstance(caught.exception.exceptions[0], TimeoutError)
        self.assertIs(caught.exception.exceptions[1], secondary)
        self.assert_closed_once(stops=0)

    def test_eof_propagates_and_releases_without_fallback_mutation(self) -> None:
        self.start()
        self.port.read.side_effect = lambda _: b""
        with self.assertRaises(EOFError):
            self.close()
        self.assert_closed_once(stops=0)

    def test_resource_failure_after_valid_readback_does_not_return_receipt(
        self,
    ) -> None:
        self.start()
        self.lease_error = ResourceLeaseError("release failed after readback")
        with self.assertRaises(ResourceLeaseError) as caught:
            self.close()
        self.assertIs(caught.exception, self.lease_error)
        self.assert_closed_once(stops=1)

    def test_boot_racing_with_stop_does_not_return_receipt(self) -> None:
        self.start()
        self.wire.on_stop = lambda: setattr(self.wire, "reboot", True)
        with self.assertRaisesRegex(RuntimeError, "boot"):
            self.close()
        self.assert_closed_once(stops=1)

    def test_lease_loss_during_no_peer_snapshot_blocks_stop(self) -> None:
        self.start()
        self.wire.on_limits = lambda: setattr(
            self.resources.owner.require,
            "side_effect",
            ResourceLeaseError("broker lost"),
        )
        with self.assertRaisesRegex(ResourceLeaseError, "broker lost"):
            self.close()
        self.assert_closed_once(stops=0)

    def test_nonfinite_deadline_refuses_wire_and_releases(self) -> None:
        self.start()
        with self.assertRaisesRegex(ValueError, "finite"):
            self.fixture.stop_advertising_and_close(deadline=float("inf"))
        self.assert_closed_once(stops=0)

    def test_cached_peer_refuses_wire_mutation_even_when_firmware_reports_none(
        self,
    ) -> None:
        self.start()
        self.fixture._connected_peers.add(("010203040506", 0))
        with self.assertRaisesRegex(RuntimeError, "peer"):
            self.close()
        self.assert_closed_once(stops=0)

    def test_borrowed_lease_scope_closes_without_releasing_external_owner(self) -> None:
        self.fixture._resources.close()
        self.wire.events[:] = ["acquire"]
        lease = resource_lease.DeviceLease(
            "jlink", "123456", purpose="synthetic borrowed owner"
        )
        lease._pid, lease._held = os.getpid(), True
        lease._process = Mock()
        lease._process.poll.return_value = None
        identity = resource_lease.PortIdentity("jlink", "123456", 0x1366, 0x1061, None)
        self.serial = SerialByteTransport(self.port)
        with (
            patch.object(direct_fixture, "open_serial", return_value=self.serial),
            patch.object(direct_fixture, "port_lease", resource_lease.port_lease),
            patch.object(resource_lease, "_port_identity", return_value=identity),
            patch.object(lease, "release") as release,
        ):
            self.fixture = direct_fixture.DirectFixture(
                self.plan.port,
                self.plan.profile_path,
                fixture_session=self.plan,
                lease=lease,
            )
            self.addCleanup(self.fixture._resources.close)
            self.start()
            receipt = self.close()
            self.fixture.stop()
        self.assertTrue(receipt.resources_closed)
        release.assert_not_called()
        lease.require("jlink", "123456")
        self.assertTrue(self.serial._closed)

    def test_late_resource_release_cannot_return_success(self) -> None:
        self.start()
        clock = [100.0]
        self.port.close.side_effect = lambda: clock.__setitem__(0, 102.0)
        with (
            patch.object(
                direct_fixture.time, "monotonic", side_effect=lambda: clock[0]
            ),
            self.assertRaises(TimeoutError),
        ):
            self.fixture.stop_advertising_and_close(deadline=101.0)
        self.assert_closed_once(stops=1)

    def test_legacy_stop_keeps_best_effort_btp_behavior(self) -> None:
        self.start()
        self.wire.stop_status = b"\x01"
        with patch.object(self.fixture, "_disconnect_connected_peers") as disconnect:
            self.fixture.stop()
        disconnect.assert_called_once()
        self.assertEqual(self.wire.events[-1], "release")
        self.port.close.assert_called_once()

    # -- advertising withdrawal: the mirror of resume, on the same live owner ---------

    def test_withdrawal_stops_advertising_and_leaves_the_session_live(self) -> None:
        self.start()
        self.assertEqual(self.wire.current & ADVERTISING, ADVERTISING)
        self.fixture.withdraw_advertising()
        self.assertEqual(self.wire.current & ADVERTISING, 0)
        self.assertEqual(self.wire.events.count("advertising-stop"), 1)
        # The session is untouched: the same owner can offer the peer again, which is what
        # makes the withdrawal a state change rather than a teardown.
        self.fixture.resume_advertising()
        self.assertEqual(self.wire.current & ADVERTISING, ADVERTISING)
        receipt = self.close()
        self.assertIsInstance(receipt, direct_fixture.AdvertisingCloseReceipt)
        self.assertFalse(receipt.current_settings & ADVERTISING)
        self.assertEqual(self.wire.events.count("release"), 1)

    def test_withdrawal_refuses_a_connected_peer(self) -> None:
        self.start()
        self.wire.peer = True
        with self.assertRaisesRegex(RuntimeError, "refuses a connected peer"):
            self.fixture.withdraw_advertising()
        self.assertEqual(self.wire.events.count("advertising-stop"), 0)

    def test_withdrawal_without_a_supported_readback_is_an_evidence_gap(self) -> None:
        self.start()
        self.wire.supported = 0
        with self.assertRaisesRegex(
            direct_fixture.AdvertisingStateUnverified,
            "gap.*ADVERTISING|ADVERTISING.*gap",
        ):
            self.fixture.withdraw_advertising()

    def test_withdrawal_that_the_controller_did_not_apply_is_a_failure(self) -> None:
        self.start()
        self.wire.keep_advertising = True
        with self.assertRaisesRegex(RuntimeError, "still advertising"):
            self.fixture.withdraw_advertising()

    def test_withdrawal_with_a_malformed_readback_is_a_failure(self) -> None:
        self.start()
        self.wire.info_payload = bytes(CONTROLLER_INFO.size + 1)
        with self.assertRaisesRegex(BtpCommandError, "length"):
            self.fixture.withdraw_advertising()
