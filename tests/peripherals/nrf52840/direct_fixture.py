"""Long-lived direct BTP nRF fixture lifecycle for explicit RF lanes."""

from __future__ import annotations

import math
import signal
import time
from builtins import BaseExceptionGroup
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

COORDINATION_POLL_INTERVAL = 0.05


from .btp_commands import GAP, BtpCommandError
from .btp_protocol import BtpFrame, BtpSessionError
from .btp_session import BtpSession
from .btp_transport import ByteTransport, FramedTransport
from .doctor import NrfDoctor
from .gap_commands import (
    AD_COMPLETE_LOCAL_NAME,
    AD_FLAGS,
    AD_SERVICE_UUID128,
    CONTROLLER_INFO,
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
    GapCommands,
)
from .gatt_commands import (
    GattCommands,
    tester_characteristic_native_permissions,
    uuid_to_le_bytes,
)
from .profile import load_profile
from .resource_lease import DeviceLease, ResourceLeaseError, port_lease
from .serial_transport import SerialByteTransport, open_serial

if TYPE_CHECKING:
    from .fixture_session import FixtureSession


def require_v2_coordination(mode: str) -> None:
    """Retain unsupported entry identities without ever dispatching their old wire."""
    if mode in {"rf-cancellation", "continuous-watchdog", "legacy-suite-runner"}:
        raise NotImplementedError(
            f"{mode}: legacy launcher has no immutable v2 control/fixture context; "
            "use the attested standalone diagnostic or full-suite; no v1 wire fallback"
        )


# Zephyr tests/bluetooth/tester/src/btp/btp_gap.h defines this setting as bit 10.
# btp_gap.c clears it only after stop succeeds and returns it in controller_info.
_GAP_SETTING_ADVERTISING = 1 << 10


class AdvertisingStateUnverified(RuntimeError):
    """Advertising inactivity cannot be proved by the available BTP readback."""


@dataclass(frozen=True)
class AdvertisingCloseReceipt:
    """BTP inactive readback and local scope closure, not an over-the-air verdict.

    A borrowed lease remains caller-owned. No receipt is returned if any wire,
    validation or resource-close step fails; unsupported readback is a gap.
    """

    session_id: str
    resource_identity: str
    boot_nonce: str
    supported_settings: int
    current_settings: int
    proof_method: str = "btp-gap-controller-info-advertising-bit-clear"
    advertising_inactive: bool = True
    resources_closed: bool = True


def _close_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("passive advertising close deadline exceeded")
    return remaining


class _LeasedTransport:
    """Fail closed on broker loss before touching the fixture's serial stream."""

    def __init__(self, transport: ByteTransport, lease: DeviceLease) -> None:
        self._transport = transport
        self._lease = lease
        self.close_deadline: float | None = None
        self.close_errors: list[BaseException] | None = None

    def require(self) -> None:
        kind, serial = self._lease.identity.split(":", 1)
        self._lease.require(kind, serial)

    def write(self, data: bytes) -> None:
        self.require()
        if self.close_deadline is not None:
            remaining = _close_remaining(self.close_deadline)
            if isinstance(self._transport, SerialByteTransport):
                # This terminal path owns the serial adapter until close. Cap the
                # actual pyserial write too, not just the subsequent BTP read.
                port = self._transport._port
                previous = port.write_timeout
                port.write_timeout = (
                    remaining if previous is None else min(previous, remaining)
                )
        self._transport.write(data)
        if self.close_deadline is not None:
            _close_remaining(self.close_deadline)

    def read(self, timeout: float) -> bytes:
        self.require()
        if self.close_deadline is not None:
            timeout = min(timeout, _close_remaining(self.close_deadline))
        data = self._transport.read(timeout)
        if self.close_deadline is not None:
            _close_remaining(self.close_deadline)
        return data

    def close(self) -> None:
        try:
            self._transport.close()
        except BaseException as error:
            if self.close_errors is None:
                raise
            # BtpSession closes on timeout/EOF. A close failure must not replace
            # that primary failure before the owner can independently unwind.
            error.add_note("passive advertising close: transport close")
            self.close_errors.append(error)


class DirectFixture:
    def __init__(
        self,
        port: str,
        profile_path: Path,
        *,
        lease: DeviceLease | None = None,
        fixture_session: FixtureSession | None = None,
    ) -> None:
        if (
            fixture_session is not None
            and profile_path.resolve() != fixture_session.profile_path.resolve()
        ):
            raise ValueError("direct fixture profile does not match session snapshot")
        self._port = port
        self._fixture_session = fixture_session
        self._preserve_gatt = fixture_session is not None
        # Failed live proof must not permit even teardown commands on the wrong image.
        self._mutation_allowed = fixture_session is None
        self._close_errors: list[BaseException] | None = None
        self._attested_owner: (
            tuple[FixtureSession, BtpSession, DeviceLease, bytes] | None
        ) = None
        with ExitStack() as resources:
            self._lease = resources.enter_context(
                port_lease(port, purpose="BleHub direct BTP fixture", lease=lease)
            )
            kind, serial = self._lease.identity.split(":", 1)
            self._lease.require(kind, serial)
            if fixture_session is not None:
                if fixture_session.port != port:
                    raise ResourceLeaseError(
                        "direct fixture port does not match session request"
                    )
                if fixture_session.resource_identity != self._lease.identity:
                    raise ResourceLeaseError(
                        "direct fixture resource identity does not match session request"
                    )
            self._serial = open_serial(port)
            resources.callback(self._close_resource, self._serial.close)
            self._transport = _LeasedTransport(self._serial, self._lease)
            self._session = BtpSession(FramedTransport(self._transport))
            resources.callback(self._close_resource, self._session.close)
            self._gap = GapCommands(self._session)
            self._gatt = GattCommands(self._session)
            self._profile = load_profile(profile_path)
            self._start_attempted = False
            self._started = False
            self._stopping = False
            self._stop_requested = False
            self._connected_peers: set[tuple[str, int]] = set()
            self._resources = resources.pop_all()

    def require_active_session(self, plan: FixtureSession) -> None:
        """Require this started session and its live broker without fixture I/O.

        This is a synchronous local/lease guard, not a new image attestation or
        an RF liveness observation. Receipt verification remains caller-owned.
        """

        def require_local_state() -> None:
            if self._fixture_session != plan:
                raise ResourceLeaseError(
                    "direct fixture session does not match request"
                )
            if self._port != plan.port:
                raise ResourceLeaseError("direct fixture port does not match request")
            if (
                not self._started
                or self._stopping
                or self._stop_requested
                or not self._mutation_allowed
            ):
                raise RuntimeError("direct fixture session is not active")
            if self._lease.identity != plan.resource_identity:
                raise ResourceLeaseError(
                    "direct fixture resource identity does not match request"
                )

        require_local_state()
        self._transport.require()
        require_local_state()

    def start(
        self, *, before_mutations: Callable[[], None] = lambda: None
    ) -> dict[str, int]:
        if self._start_attempted or self._stopping or self._stop_requested:
            raise RuntimeError("direct fixture start already attempted or stopped")
        self._start_attempted = True
        try:
            self._transport.require()
            if self._fixture_session is not None:
                from .fixture_session import attest_session

                attest_session(
                    self._fixture_session,
                    self._session,
                    lease=self._lease,
                    port=self._port,
                )
            # Hold/no-peer hooks need registered BTP handlers, but must still run
            # before the doctor provisions or reinitializes the GATT profile.
            NrfDoctor(self._session).ensure_services()
            if self._fixture_session is not None:
                from .fault_control import FaultController

                limits = FaultController(
                    self._session, self._fixture_session.session_id
                ).require_idle()
                if limits.peer_present:
                    raise RuntimeError(
                        "fixture startup refuses an unowned connected peer"
                    )
                self._attested_owner = (
                    self._fixture_session,
                    self._session,
                    self._lease,
                    limits.boot_nonce,
                )
            before_mutations()
            self._mutation_allowed = True
        except Exception:
            self._mutation_allowed = False
            self.stop()
            raise
        try:
            self._gap.stop_advertising()
        except (BtpCommandError, BtpSessionError, EOFError, TimeoutError):
            # A fresh boot may not be advertising. Profile provisioning below is
            # the authoritative database boundary; GATT reset is not implemented
            # by every supported Zephyr Tester image.
            pass
        # NrfDoctor either reuses and reinitializes this exact profile or fails
        # closed before advertising; it must not depend on optional reset-server.
        result = NrfDoctor(self._session).run(self._profile)
        for operation in (
            lambda: self._gap.stop_advertising(),
            lambda: self._gap.set_powered(True),
            lambda: self._gap.set_connectable(True),
            lambda: self._gap.set_discoverable(True),
        ):
            try:
                operation()
            except (BtpCommandError, BtpSessionError, EOFError, TimeoutError):
                # Resident firmware may reject a no-op state transition; the
                # subsequent advertising command remains the authoritative check.
                pass
        self._start_advertising()
        handles = result.role_handles
        self._started = True
        return handles

    def _start_advertising(self) -> None:
        self._gap.start_advertising(
            {
                AD_FLAGS: b"\x06",
                AD_SERVICE_UUID128: uuid_to_le_bytes(self._profile.service_uuid),
            },
            {AD_COMPLETE_LOCAL_NAME: self._profile.local_name.encode("utf-8")},
        )

    def _restart_advertising(self) -> None:
        try:
            self._gap.stop_advertising()
        except (BtpCommandError, BtpSessionError, EOFError, TimeoutError):
            pass
        self._start_advertising()

    def run_subscription_coordination(
        self,
        coordination_path: Path,
        handles: dict[str, int],
        value_role: str = "updates",
    ) -> None:
        enabled = coordination_path / "subscription-enabled"
        disabled = coordination_path / "subscription-disabled"
        applied = coordination_path / "value-applied"
        post_disable = coordination_path / "post-disable-triggered"
        deadline = time.monotonic() + 240
        while not enabled.is_file():
            if self._stop_requested or time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for host subscription enable")
            time.sleep(COORDINATION_POLL_INTERVAL)
        self._gatt.set_value(handles[value_role], self._profile.notification_value)
        applied.write_text("ready\n", encoding="ascii")
        while not disabled.is_file():
            if self._stop_requested or time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for host subscription disable")
            time.sleep(COORDINATION_POLL_INTERVAL)
        post_disable.write_text("ready\n", encoding="ascii")

    def run_subscription_soak_coordination(
        self,
        coordination_path: Path,
        handles: dict[str, int],
        value_role: str,
        cycles: int,
    ) -> None:
        """Coordinate repeated subscriptions while keeping this fixture alive."""
        if cycles < 1:
            raise ValueError("subscription soak requires at least one cycle")
        for cycle in range(1, cycles + 1):
            self._gatt.set_value(handles["read-write"], b"\x00")
            cycle_path = coordination_path / f"cycle-{cycle}"
            cycle_path.mkdir(parents=True, exist_ok=True)
            enabled = cycle_path / "subscription-enabled"
            disabled = cycle_path / "subscription-disabled"
            applied = cycle_path / "value-applied"
            post_disable = cycle_path / "post-disable-triggered"
            deadline = time.monotonic() + 240
            while not enabled.is_file():
                if self._stop_requested or time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out waiting for host subscription enable in cycle {cycle}"
                    )
                time.sleep(COORDINATION_POLL_INTERVAL)
            self._gatt.set_value(handles[value_role], self._profile.notification_value)
            applied.write_text("ready\\n", encoding="ascii")
            while not disabled.is_file():
                if self._stop_requested or time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out waiting for host subscription disable in cycle {cycle}"
                    )
                time.sleep(COORDINATION_POLL_INTERVAL)
            post_disable.write_text("ready\\n", encoding="ascii")
            while True:
                if self._stop_requested or time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out waiting for host disconnect in cycle {cycle}"
                    )
                event = self._next_coordination_event()
                if event is not None and self._is_gap_event(
                    event, GAP_DEVICE_DISCONNECTED
                ):
                    break
            self._start_advertising()

    @staticmethod
    def _is_gap_event(event: BtpFrame, opcode: int) -> bool:
        return event.service == 1 and event.opcode == opcode

    def _next_coordination_event(self) -> BtpFrame | None:
        return self._session.pop_event() or self._session.poll_event(
            COORDINATION_POLL_INTERVAL
        )

    def run_host_close_coordination(
        self, coordination_path: Path, handles: dict[str, int]
    ) -> None:
        generation_0 = coordination_path / "generation-0"
        generation_1 = coordination_path / "generation-1"
        generation_0.mkdir(parents=True, exist_ok=True)
        generation_1.mkdir(parents=True, exist_ok=True)
        subscription_enabled = generation_0 / "subscription-enabled"
        value_applied = generation_0 / "value-applied"
        active = generation_0 / "active"
        armed = generation_0 / "armed"
        host_disconnected = generation_0 / "host-disconnected"
        deadline = time.monotonic() + 240

        # Establish an event boundary before the RN runner can create the first
        # connection. Events already buffered by an earlier fixture generation
        # must not satisfy this generation's connection/disconnect checks.
        self._gatt.get_attributes()
        while self._session.pop_event() is not None:
            pass

        connected = False
        while not connected or not subscription_enabled.is_file():
            if self._stop_requested or time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for RN host-close connection")
            event = self._next_coordination_event()
            if event is None:
                continue
            if self._is_gap_event(event, GAP_DEVICE_DISCONNECTED):
                raise BtpCommandError(
                    "nRF host-close observed GAP disconnect before first connection"
                )
            if self._is_gap_event(event, GAP_DEVICE_CONNECTED):
                if len(event.payload) < 7:
                    raise BtpCommandError(
                        "nRF host-close received malformed GAP connected event"
                    )
                connected = True

        self._gatt.set_value(handles["updates"], self._profile.notification_value)
        value_applied.write_text("ready\n", encoding="ascii")

        while not active.is_file():
            if self._stop_requested or time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for RN host-close active marker")
            event = self._next_coordination_event()
            if event is not None and self._is_gap_event(event, GAP_DEVICE_DISCONNECTED):
                raise BtpCommandError(
                    "nRF host-close observed GAP disconnect before armed ACK"
                )

        # Flush and inspect queued events before arming the runner. The host
        # schedules native reload with a short grace period after the active
        # marker; events queued across that boundary must not be lost.
        self._gatt.get_attributes()
        while (event := self._session.pop_event()) is not None:
            if self._is_gap_event(event, GAP_DEVICE_DISCONNECTED):
                raise BtpCommandError(
                    "nRF host-close observed GAP disconnect before armed ACK"
                )
        armed.write_text("ready\n", encoding="ascii")

        self._gatt.get_attributes()
        disconnected = False
        while (event := self._session.pop_event()) is not None:
            if self._is_gap_event(event, GAP_DEVICE_DISCONNECTED):
                disconnected = True
                break
        while not disconnected:
            if self._stop_requested or time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for host GAP disconnect")
            event = self._next_coordination_event()
            disconnected = event is not None and self._is_gap_event(
                event, GAP_DEVICE_DISCONNECTED
            )
        host_disconnected.write_text("ready\n", encoding="ascii")
        self._start_advertising()

        subscription_enabled = generation_1 / "subscription-enabled"
        value_applied = generation_1 / "value-applied"
        while not subscription_enabled.is_file():
            if self._stop_requested or time.monotonic() >= deadline:
                raise TimeoutError(
                    "timed out waiting for RN second-generation subscription"
                )
            time.sleep(COORDINATION_POLL_INTERVAL)
        self._gatt.set_value(handles["updates"], self._profile.notification_value)
        value_applied.write_text("ready\n", encoding="ascii")

    def run_passive_disconnect_coordination(
        self, coordination_path: Path, handles: dict[str, int]
    ) -> None:
        subscription_enabled = coordination_path / "subscription-enabled"
        value_applied = coordination_path / "value-applied"
        connected = coordination_path / "connected"
        triggered = coordination_path / "fault-triggered"
        deadline = time.monotonic() + 240
        while not subscription_enabled.is_file():
            if self._stop_requested or time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for RN notification enable")
            time.sleep(COORDINATION_POLL_INTERVAL)
        self._gatt.set_value(handles["updates"], self._profile.notification_value)
        value_applied.write_text("ready\n", encoding="ascii")
        while not connected.is_file():
            if self._stop_requested or time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for RN connection")
            time.sleep(COORDINATION_POLL_INTERVAL)

        # A request is used to drain the BTP event stream while preserving the
        # single-session serial owner. The GAP connected event carries the peer
        # address required by the BTP disconnect command.
        self._gatt.get_attributes()
        peer: tuple[str, int] | None = None
        while True:
            event = self._session.pop_event()
            if event is None:
                break
            if (
                event.service == GAP
                and event.opcode == GAP_DEVICE_CONNECTED
                and len(event.payload) >= 7
            ):
                address_type = event.payload[0]
                address = event.payload[1:7][::-1].hex()
                peer = (address, address_type)
        if peer is None:
            raise BtpCommandError("GAP connected event did not expose a peer address")
        triggered.write_text("ready\n", encoding="ascii")
        self._gap.disconnect(*peer)

        disconnected_deadline = time.monotonic() + 30
        disconnected = False
        while time.monotonic() < disconnected_deadline:
            event = self._session.pop_event() or self._session.poll_event(0.2)
            if (
                event is not None
                and event.service == 1
                and event.opcode == GAP_DEVICE_DISCONNECTED
            ):
                disconnected = True
                break
        if not disconnected:
            raise TimeoutError(
                "nRF passive-disconnect fixture did not observe GAP disconnect"
            )
        self._start_advertising()
        (coordination_path / "recovery-ready").write_text("ready\n", encoding="ascii")

    def run_cancellation_coordination(
        self,
        root: Path,
        handles: dict[str, int],
        *,
        parent_guard: Callable[[], None] = lambda: None,
        preparation: dict[str, object] | None = None,
    ) -> None:
        """Capture one remote read on the existing leased session, never prelaunch ARM."""
        import os
        from dataclasses import asdict

        from .cancellation_control import (
            POLL_SECONDS,
            RELEASE_BUDGET,
            read_document,
            require_multifunction,
            validate_preparation,
            verify_control,
            write_document,
        )
        from .fault_control import (
            Decision,
            FaultController,
            Phase,
            cleanup_ready,
            encode_status,
            read_completed,
            ticket_document,
        )

        plan = require_multifunction(self._fixture_session)
        control = verify_control(root, plan, (os.getpid(), os.getpgid(0)))
        directory = root / control.directory
        directory.mkdir(exist_ok=True)
        rw = next(c for c in self._profile.characteristics if c.role == "read-write")
        if rw.initial_value != b"\x00" or rw.permissions != 0x03:
            raise ValueError("cancellation requires ordinary RW with initial 00")
        target = handles["read-write"]
        evidence = {
            "schema_version": 2,
            "control": asdict(control),
            "session_id": plan.session_id,
            "contract_sha256": plan.contract_sha256,
            "profile_sha256": plan.profile_sha256,
            "resource_identity": plan.resource_identity,
            "fixture_pid": os.getpid(),
            "fixture_pgid": os.getpgid(0),
            "target_uuid": rw.uuid,
            "target_handle": target,
            "release_budget_seconds": RELEASE_BUDGET,
            "status": "running",
            "boundaries": [],
            "cleanup_errors": [],
        }
        if preparation is not None:
            evidence["preparation"] = preparation
        fault = FaultController(self._session, plan.session_id)
        ticket = None
        armed_at = 0.0
        peer = None
        closing = disconnected = False

        def record(boundary: str, status=None, **fields: object) -> None:
            if ticket is not None:
                fields["ticket"] = ticket_document(ticket)
            if status is not None:
                fields["status_wire"] = encode_status(status).hex()
            evidence["boundaries"].append(
                {"boundary": boundary, "monotonic": time.monotonic(), **fields}
            )
            write_document(directory / "physical-evidence.json", evidence)

        def guard(*, active: bool = False) -> None:
            parent_guard()
            self._transport.require()
            if self._stop_requested or (directory / "abort").exists():
                raise RuntimeError("cancellation barrier aborted by owner")
            if active and time.monotonic() >= armed_at + RELEASE_BUDGET:
                raise TimeoutError(
                    "cancellation missed pre-native-watchdog resolution budget"
                )

        def drain() -> None:
            nonlocal disconnected
            for _ in range(256):
                event = self._session.pop_event()
                if event is None:
                    break
                if event.service != GAP or event.opcode not in (
                    GAP_DEVICE_CONNECTED,
                    GAP_DEVICE_DISCONNECTED,
                ):
                    continue
                if (
                    type(event.service) is not int
                    or type(event.opcode) is not int
                    or type(event.index) is not int
                    or event.index != 0
                    or type(event.payload) is not bytes
                    or len(event.payload)
                    != (13 if event.opcode == GAP_DEVICE_CONNECTED else 7)
                    or event.payload[0] not in range(4)
                ):
                    raise RuntimeError("malformed cancellation GAP peer event")
                observed = (event.payload[1:7][::-1].hex(), event.payload[0])
                if event.opcode == GAP_DEVICE_CONNECTED:
                    if peer is not None or self._connected_peers:
                        raise RuntimeError(
                            "foreign or duplicate cancellation GAP connection"
                        )
                    self._connected_peers.add(observed)
                else:
                    if observed not in self._connected_peers or (
                        peer is not None and observed != peer
                    ):
                        raise RuntimeError(
                            "foreign or duplicate cancellation GAP disconnect"
                        )
                    if peer is not None and not closing:
                        raise RuntimeError("cancellation peer disconnected during hold")
                    self._connected_peers.remove(observed)
                    disconnected = peer is not None
            else:
                raise RuntimeError("cancellation GAP backlog did not reach idle")
            if peer is not None and self._connected_peers != (
                set() if disconnected else {peer}
            ):
                raise RuntimeError("cancellation GAP peer changed during hold/close")

        def request(phase: str, deadline: float, *, active: bool = False) -> None:
            path = directory / f"request-{phase}.json"
            while True:
                guard(active=active)
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"missing cancellation request: {phase}")
                if path.exists():
                    if read_document(path) != asdict(control):
                        raise ValueError("cancellation request generation/token drift")
                    return
                time.sleep(POLL_SECONDS)

        def status(*, active: bool = True):
            guard(active=active)
            snapshot = fault.status(ticket)
            guard(active=active)
            drain()
            return snapshot

        def signal(phase: str) -> None:
            write_document(directory / f"signal-{phase}.json", asdict(control))

        try:
            request("arm", time.monotonic() + 300)
            attributes = self._gatt.get_attributes(attribute_uuid=rw.uuid)
            if (
                len(attributes) != 1
                or attributes[0].handle != target
                or attributes[0].uuid != rw.uuid
                or attributes[0].permissions
                != tester_characteristic_native_permissions(rw.permissions)
            ):
                raise RuntimeError(
                    "cancellation target does not match provider profile/ATT handle"
                )
            drain()
            if len(self._connected_peers) != 1:
                raise RuntimeError("cancellation requires exactly one GAP peer")
            peer = next(iter(self._connected_peers))
            before = self._gatt.get_attribute_value(target)
            if before.att_response != 0 or before.value != rw.initial_value:
                raise RuntimeError("cancellation before value mismatch")
            if preparation is not None:
                limits = fault.require_idle()
                guard()
                drain()
                validate_preparation(
                    preparation,
                    evidence,
                    boot_nonce=limits.boot_nonce,
                    armed_at=time.monotonic(),
                )
            armed_at = time.monotonic()
            peer_wire = bytes([peer[1]]) + bytes.fromhex(peer[0])[::-1]
            ticket = fault.arm_read(target, before.value, peer_wire).ticket
            snapshot = status()
            if snapshot.phase is not Phase.ARMED:
                raise RuntimeError("cancellation ARM did not reach exact ARMED")
            record("armed", snapshot, monotonic=armed_at, peer=list(peer))
            signal("armed")
            request("read-dispatched", armed_at + RELEASE_BUDGET, active=True)
            while True:
                snapshot = status()
                if snapshot.phase is Phase.HELD:
                    break
                if snapshot.phase is not Phase.ARMED:
                    raise RuntimeError("read never reached HELD")
                time.sleep(POLL_SECONDS)
            record("held", snapshot, peer=list(peer))
            signal("held")
            request("release", armed_at + RELEASE_BUDGET, active=True)
            snapshot = status()
            if snapshot.phase is not Phase.HELD:
                raise RuntimeError("read no longer HELD before RESOLVE")
            record("still-held", snapshot, peer=list(peer))
            fault.resolve(ticket, Decision.COMMIT)
            guard(active=True)
            record("resolve-dispatched")
            resolution_deadline = min(armed_at + RELEASE_BUDGET, time.monotonic() + 2)
            while True:
                snapshot = status()
                if time.monotonic() >= resolution_deadline:
                    raise TimeoutError("cancellation normal read resolution deadline")
                if read_completed(snapshot):
                    break
                if snapshot.phase is Phase.RETIRED:
                    raise RuntimeError(
                        "cancellation read did not complete NORMAL_NO_COMMIT"
                    )
                time.sleep(POLL_SECONDS)
            record("read-completed", snapshot, peer=list(peer))
            signal("released")
            deadline = time.monotonic() + 60
            while not (directory / "done").exists():
                guard()
                if time.monotonic() >= deadline:
                    raise TimeoutError("cancellation ledger/close did not finish")
                time.sleep(POLL_SECONDS)
            closing = True
            deadline = time.monotonic() + 3
            while True:
                snapshot = status(active=False)
                if time.monotonic() >= deadline:
                    raise TimeoutError("cancellation peer did not disconnect on close")
                if not read_completed(snapshot):
                    raise RuntimeError("cancellation retained read receipt drift")
                if disconnected:
                    break
                time.sleep(POLL_SECONDS)
            limits = fault.require_idle()
            guard()
            drain()
            verify_control(root, plan, (os.getpid(), os.getpgid(0)))
            if (
                limits.peer_present
                or limits.boot_nonce != ticket.boot_nonce
                or time.monotonic() >= deadline
            ):
                raise RuntimeError("cancellation final live idle/boot/deadline drift")
            evidence["status"] = "passed"
            record("closed", snapshot, hold_inactive=True, peer_count=0)
        except BaseException as error:
            evidence["status"] = "failed"
            evidence["primary_error"] = str(error)
            try:
                if ticket is not None:
                    # Cleanup never renews the fault deadline and never mutates a
                    # disconnected owner. A stale receipt blocks further mutation.
                    deadline = time.monotonic() + 7
                    snapshot = fault.status(ticket)
                    limits = fault.get_limits()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("fault cleanup inspection deadline")
                    if (
                        snapshot.phase in (Phase.ARMED, Phase.HELD)
                        and limits.peer_present
                    ):
                        if (
                            limits.boot_nonce != ticket.boot_nonce
                            or limits.peer != snapshot.peer
                            or (
                                limits.connection_epoch,
                                limits.bearer_epoch,
                                limits.database_epoch,
                            )
                            != (
                                ticket.connection_epoch,
                                ticket.bearer_epoch,
                                ticket.database_epoch,
                            )
                        ):
                            raise RuntimeError(
                                "refusing cleanup of a foreign fault owner"
                            )
                        fault.abort(ticket)
                    while True:
                        snapshot = fault.status(ticket)
                        if time.monotonic() >= deadline:
                            raise TimeoutError("fault cleanup did not retire")
                        if cleanup_ready(snapshot):
                            break
                        time.sleep(POLL_SECONDS)
                    record("cleanup", snapshot, hold_inactive=True)
            except (
                OSError,
                ValueError,
                RuntimeError,
                EOFError,
                TimeoutError,
            ) as cleanup_error:
                evidence["cleanup_errors"] = [str(cleanup_error)]
                error.add_note(f"fault cleanup failed: {cleanup_error}")
            try:
                write_document(directory / "physical-evidence.json", evidence)
                write_document(
                    directory / "failure.json",
                    {"control": asdict(control), "error": str(error)},
                )
            except (OSError, ValueError, RuntimeError) as publication_error:
                error.add_note(
                    f"physical failure evidence publication failed: {publication_error}"
                )
            raise

    def run_recovery_coordination(
        self,
        coordination_path: Path,
        handles: dict[str, int],
        cycles: int = 3,
        first_cycle: int = 0,
        wait_for_next_disconnect: bool = True,
    ) -> None:
        if cycles < 1:
            raise ValueError("recovery coordination requires at least one cycle")

        def wait_for(path: Path, label: str) -> None:
            deadline = time.monotonic() + 90
            while not path.is_file():
                if self._stop_requested or time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for RN recovery {label}")
                time.sleep(COORDINATION_POLL_INTERVAL)

        if first_cycle < 0:
            raise ValueError("recovery first cycle must not be negative")

        for cycle in range(first_cycle, first_cycle + cycles):
            directory = coordination_path / f"cycle-{cycle}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "fixture-ready").write_text("ready\n", encoding="ascii")
            wait_for(directory / "subscription-enabled", "subscription enable")
            self._gatt.set_value(handles["updates"], self._profile.notification_value)
            (directory / "value-applied").write_text("ready\n", encoding="ascii")
            wait_for(directory / "connected", "value observation")
            self._gatt.get_attributes()
            peer: tuple[str, int] | None = None
            while (event := self._session.pop_event()) is not None:
                if (
                    event.service == GAP
                    and event.opcode == GAP_DEVICE_CONNECTED
                    and len(event.payload) >= 7
                ):
                    peer = (event.payload[1:7][::-1].hex(), event.payload[0])
            if peer is None:
                raise BtpCommandError(
                    f"cycle {cycle} GAP connected event did not expose a peer"
                )
            self._gap.disconnect(*peer)
            (directory / "fault-triggered").write_text("ready\n", encoding="ascii")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not self._stop_requested:
                event = self._session.pop_event() or self._session.poll_event(0.2)
                if (
                    event is not None
                    and event.service == 1
                    and event.opcode == GAP_DEVICE_DISCONNECTED
                ):
                    break
            else:
                raise TimeoutError(
                    f"cycle {cycle} fixture did not observe GAP disconnect"
                )
            wait_for(directory / "passive-disconnected", "passive disconnect ACK")
            self._start_advertising()
            (directory / "recovery-ready").write_text("ready\n", encoding="ascii")
            wait_for(directory / "cycle-passed", "cycle completion ACK")
            if wait_for_next_disconnect and cycle < first_cycle + cycles - 1:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not self._stop_requested:
                    event = self._session.pop_event() or self._session.poll_event(0.2)
                    if (
                        event is not None
                        and event.service == 1
                        and event.opcode == GAP_DEVICE_DISCONNECTED
                    ):
                        break
                else:
                    raise TimeoutError(
                        f"cycle {cycle} recovery disconnect was not observed by fixture"
                    )
                self._start_advertising()

    def _observe_gap_event(self, event: BtpFrame) -> None:
        if event.service != 1:
            return
        if event.opcode == GAP_DEVICE_CONNECTED and len(event.payload) >= 7:
            self._connected_peers.add(
                (event.payload[1:7][::-1].hex(), event.payload[0])
            )
        elif event.opcode == GAP_DEVICE_DISCONNECTED and len(event.payload) >= 7:
            self._connected_peers.discard(
                (event.payload[1:7][::-1].hex(), event.payload[0])
            )

    def _disconnect_connected_peers(self) -> None:
        """Disconnect peers through this fixture's sole BTP session before exit."""
        # Drain events collected by the main loop and give the dispatcher one
        # final request boundary for a connection event that raced with stop.
        while (event := self._session.pop_event()) is not None:
            self._observe_gap_event(event)
        try:
            self._gatt.get_attributes()
        except (BtpCommandError, BtpSessionError, EOFError, TimeoutError):
            return
        while (event := self._session.pop_event()) is not None:
            self._observe_gap_event(event)

        peers = set(self._connected_peers)
        for address, address_type in peers:
            try:
                self._gap.disconnect(address, address_type)
            except (BtpCommandError, BtpSessionError, EOFError, TimeoutError):
                pass

        deadline = time.monotonic() + 5
        while peers and time.monotonic() < deadline:
            event = self._session.pop_event() or self._session.poll_event(0.2)
            if event is not None:
                self._observe_gap_event(event)
                peers.intersection_update(self._connected_peers)

    def _close_resource(self, close: Callable[[], None]) -> None:
        try:
            close()
        except BaseException as error:
            if self._close_errors is None:
                raise
            error.add_note("passive advertising close: owned resource close")
            self._close_errors.append(error)

    def stop_advertising_and_close(self, *, deadline: float) -> AdvertisingCloseReceipt:
        """One-shot passive teardown on the same live-attested BTP owner.

        ``deadline`` is a finite absolute monotonic deadline shared by all wire
        I/O (including fragmented reads and serial writes), checks and completion.
        Local resource release is unconditional, even after expiry, and uses the
        lease broker's own bounded shutdown budget; this is not a hard wall-clock
        bound on OS close/reaping. Late completion never returns a receipt.

        Refuse unknown/busy fault state, boot drift and any peer observation. Send
        STOP_ADVERTISING once, then require exact controller-info readback with
        the supported ADVERTISING bit clear and another idle/no-peer snapshot.
        Unsupported readback raises an explicit evidence gap, never a pass.
        No disconnect, reset, flash, retry or second serial/session is permitted.

        Single failures are re-raised unchanged. Multiple failures form a
        BaseExceptionGroup, primary first, followed by resource-release errors.
        All outcomes consume this fixture: subsequent legacy stop() is a no-op.
        """
        from .fault_control import FaultController

        if self._stopping:
            raise RuntimeError("direct fixture already stopped or closing")
        release_errors: list[BaseException] = []
        self._close_errors = release_errors
        self._transport.close_errors = release_errors
        primary: BaseException | None = None
        receipt: AdvertisingCloseReceipt | None = None
        try:
            if not math.isfinite(deadline):
                raise ValueError("passive advertising close deadline must be finite")
            _close_remaining(deadline)
            owner = self._attested_owner
            if (
                owner is None
                or self._fixture_session != owner[0]
                or self._session is not owner[1]
                or self._lease is not owner[2]
            ):
                raise RuntimeError(
                    "passive close requires the same live-attested session"
                )
            plan, session, _, boot_nonce = owner
            self.require_active_session(plan)
            self._stopping = True
            self._started = False
            self._transport.close_deadline = deadline
            fault = FaultController(session, plan.session_id)

            def require_no_peer() -> None:
                self._transport.require()
                limits = fault.require_idle(
                    timeout=min(1.0, _close_remaining(deadline))
                )
                self._transport.require()
                _close_remaining(deadline)
                if limits.boot_nonce != boot_nonce:
                    raise RuntimeError("passive close refuses changed fixture boot")
                if limits.peer_present or self._connected_peers:
                    raise RuntimeError("passive close refuses a connected peer")
                # A fresh RX snapshot is necessary but not enough if a peer event
                # raced with it. Do not adopt, discard or disconnect that peer.
                for _ in range(256):
                    _close_remaining(deadline)
                    event = session.pop_event()
                    if event is None:
                        return
                    if event.service == GAP and event.opcode in (
                        GAP_DEVICE_CONNECTED,
                        GAP_DEVICE_DISCONNECTED,
                    ):
                        raise RuntimeError("passive close refuses a GAP peer event")
                raise RuntimeError(
                    "passive close cannot establish no-peer: event backlog"
                )

            require_no_peer()
            self._gap.stop_advertising()
            self._transport.require()
            _close_remaining(deadline)
            info = self._gap.read_controller_info()
            if len(info) != CONTROLLER_INFO.size:
                raise BtpCommandError(
                    "GAP controller-info response has the wrong length"
                )
            _, supported, current, _, _, _ = CONTROLLER_INFO.unpack(info)
            require_no_peer()
            if not supported & _GAP_SETTING_ADVERTISING:
                raise AdvertisingStateUnverified(
                    "advertising inactive evidence gap: ADVERTISING setting unsupported"
                )
            if current & _GAP_SETTING_ADVERTISING:
                raise RuntimeError("BTP controller is still advertising after stop")
            receipt = AdvertisingCloseReceipt(
                plan.session_id,
                plan.resource_identity,
                boot_nonce.hex(),
                supported,
                current,
            )
        except BaseException as error:  # noqa: BLE001 - release independently, then re-raise all failures
            primary = error
        finally:
            self._started = False
            self._stopping = True
            self._mutation_allowed = False
            # Close callbacks collect independently so ExitStack reaches lease
            # exit with no active exception that could hide a release failure.
            try:
                self._resources.close()
            except BaseException as error:  # noqa: BLE001 - aggregate without hiding the primary failure
                error.add_note("passive advertising close: lease scope release")
                release_errors.append(error)
        errors = ([primary] if primary is not None else []) + release_errors
        if not errors:
            try:
                _close_remaining(deadline)
            except TimeoutError as error:
                errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("passive advertising close failed", errors)
        assert receipt is not None
        return receipt

    def stop(self) -> None:
        self._started = False
        if self._stopping:
            return
        self._stopping = True
        try:
            if not self._mutation_allowed:
                return
            self._transport.require()
            try:
                if not getattr(self, "_strict_recovery_cleanup", False):
                    self._disconnect_connected_peers()
            except (BtpCommandError, BtpSessionError, EOFError, TimeoutError):
                pass
            try:
                self._gap.stop_advertising()
            except (BtpCommandError, BtpSessionError, EOFError, TimeoutError):
                pass
            if getattr(self, "_preserve_gatt", False):
                return
            try:
                # A pending ATT indication can leave the resident dynamic GATT server
                # unable to accept the next profile's initial values. Reset only the
                # host-owned dynamic server at teardown; supported firmware may close
                # the BTP transport as part of this reset, which is also safe here.
                self._gatt.reset_server()
            except (BtpCommandError, BtpSessionError, EOFError, TimeoutError):
                pass
        finally:
            self._resources.close()


def serve(
    port: str,
    profile_path: Path,
    coordination_path: Path | None = None,
    coordination_mode: str = "notification",
    coordination_cycles: int = 1,
    fixture_session: FixtureSession | None = None,
) -> None:
    require_v2_coordination(coordination_mode)
    if coordination_mode == "permission-transition":
        raise ValueError(
            "permission-transition requires its dedicated attested provider entry"
        )
    if coordination_mode == "adapter-transition-attested":
        from .adapter_transition import validate_control

        if coordination_path is None:
            raise ValueError("adapter-transition-attested requires --coordination-path")
        validate_control(coordination_path, fixture_session)
    if coordination_mode in ("android-pending-reload", "ios-pending-reload"):
        from .pending_reload import validate_control

        if coordination_path is None:
            raise ValueError(f"{coordination_mode} requires --coordination-path")
        validate_control(
            coordination_path,
            fixture_session,
            platform="ios" if coordination_mode == "ios-pending-reload" else "android",
        )
    if coordination_mode == "recovery-ten-attested":
        from .recovery_ten import validate_control

        if coordination_path is None:
            raise ValueError("recovery-ten-attested requires --coordination-path")
        validate_control(coordination_path, fixture_session)
    if coordination_mode == "rf-lab-full-attested":
        from .full_suite import validate_control

        if coordination_path is None:
            raise ValueError("rf-lab-full-attested requires --coordination-path")
        validate_control(coordination_path, fixture_session)

    reject_mode = coordination_mode in (
        "write-reject-attested",
        "ccc-enable-reject-attested",
        "ccc-disable-reject-attested",
    )
    if reject_mode:
        if coordination_mode == "ccc-disable-reject-attested":
            from . import ccc_disable_reject as reject_gate
        elif coordination_mode == "ccc-enable-reject-attested":
            from . import ccc_enable_reject as reject_gate
        else:
            from . import write_reject as reject_gate

        if coordination_path is None:
            raise ValueError(f"{coordination_mode} requires --coordination-path")
        reject_gate.validate_control(coordination_path, fixture_session)

    def record_start_failure(error: BaseException) -> None:
        if reject_mode:
            assert coordination_path is not None and fixture_session is not None
            reject_gate.record_start_failure(coordination_path, fixture_session, error)
        if coordination_mode == "adapter-transition-attested":
            from .adapter_transition import record_start_failure as record

            assert coordination_path is not None and fixture_session is not None
            record(coordination_path, fixture_session, error)
        if coordination_mode == "rf-lab-full-attested":
            from .full_suite import record_start_failure as record

            assert coordination_path is not None and fixture_session is not None
            record(coordination_path, fixture_session, error)

    try:
        fixture = DirectFixture(port, profile_path, fixture_session=fixture_session)
    except BaseException as error:
        record_start_failure(error)
        raise
    fixture._preserve_gatt = fixture_session is not None or coordination_mode in (
        "android-pending-reload",
        "ios-pending-reload",
        "recovery-ten-attested",
        "rf-lab-full-attested",
    )
    fixture._strict_recovery_cleanup = coordination_mode in (
        "recovery-ten-attested",
        "rf-lab-full-attested",
        "write-reject-attested",
        "ccc-enable-reject-attested",
        "ccc-disable-reject-attested",
    )
    primary: BaseException | None = None
    full_dispatched = False
    reject_dispatched = False
    try:
        if coordination_mode == "adapter-transition-attested":
            from .adapter_transition import run

            assert coordination_path is not None
            signal.signal(
                signal.SIGTERM, lambda *_: setattr(fixture, "_stop_requested", True)
            )
            signal.signal(
                signal.SIGINT, lambda *_: setattr(fixture, "_stop_requested", True)
            )
            # Own startup and physical cleanup, then retain the same read-only
            # session/lease until stop-owned signals this process. Returning only
            # after that signal keeps the host's final liveness checks meaningful.
            run(
                fixture,
                coordination_path,
                on_ready=lambda: print("NRF52840_DIRECT_READY", flush=True),
            )
            return
        handles = fixture.start()
        fixture._transport.require()

        def ready() -> None:
            print(
                f"NRF52840_DIRECT_READY profile={fixture._profile.profile_id} handles={handles}",
                flush=True,
            )

        signal.signal(
            signal.SIGTERM, lambda *_: setattr(fixture, "_stop_requested", True)
        )
        signal.signal(
            signal.SIGINT, lambda *_: setattr(fixture, "_stop_requested", True)
        )
        if coordination_mode == "recovery-ten-attested":
            from .recovery_ten import run

            assert coordination_path is not None
            run(fixture, coordination_path, handles, on_ready=ready)
        elif coordination_mode == "system-loss-attested":
            from .system_loss_attested import run

            assert coordination_path is not None
            run(fixture, coordination_path, handles, on_ready=ready)
            return
        else:
            ready()
        if (
            coordination_path is not None
            and coordination_mode != "recovery-ten-attested"
        ):
            coordination_path.mkdir(parents=True, exist_ok=True)
            if coordination_mode == "notification":
                fixture.run_subscription_coordination(coordination_path, handles)
            elif coordination_mode == "indication":
                fixture.run_subscription_coordination(
                    coordination_path, handles, value_role="indications"
                )
            elif coordination_mode == "soak-notification":
                fixture.run_subscription_soak_coordination(
                    coordination_path, handles, "updates", coordination_cycles
                )
            elif coordination_mode == "soak-indication":
                fixture.run_subscription_soak_coordination(
                    coordination_path, handles, "indications", coordination_cycles
                )
            elif coordination_mode == "passive-disconnect":
                fixture.run_passive_disconnect_coordination(coordination_path, handles)
            elif coordination_mode in ("android-pending-reload", "ios-pending-reload"):
                from .pending_reload import run

                run(
                    fixture,
                    coordination_path,
                    handles,
                    **(
                        {"platform": "ios"}
                        if coordination_mode == "ios-pending-reload"
                        else {}
                    ),
                )
            elif reject_mode:
                reject_dispatched = True
                reject_gate.run(fixture, coordination_path, handles)
            elif coordination_mode == "rf-lab-full-attested":
                from .full_suite import run

                full_dispatched = True
                run(fixture, coordination_path, handles)
            elif coordination_mode == "rf-lab-suite":
                from .full_suite import run_continuous

                run_continuous(fixture, coordination_path, handles)
            elif coordination_mode == "host-close":
                fixture.run_host_close_coordination(coordination_path, handles)
            elif coordination_mode == "recovery":
                fixture.run_recovery_coordination(coordination_path, handles)
            elif coordination_mode == "recovery-ios":
                fixture.run_recovery_coordination(
                    coordination_path,
                    handles,
                    first_cycle=1,
                    wait_for_next_disconnect=False,
                )
            elif coordination_mode == "recovery-ten":
                fixture.run_recovery_coordination(coordination_path, handles, cycles=10)
            else:
                raise ValueError(
                    f"unsupported direct RF coordination mode: {coordination_mode}"
                )
        fixture._transport.require()
        while not fixture._stop_requested:
            fixture._transport.require()
            event = fixture._session.pop_event() or fixture._session.poll_event(0.2)
            if event is not None:
                if coordination_mode in (
                    "recovery-ten-attested",
                    "rf-lab-full-attested",
                    "write-reject-attested",
                    "ccc-enable-reject-attested",
                    "ccc-disable-reject-attested",
                ) and (
                    event.service == GAP
                    and event.opcode in (GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED)
                ):
                    raise RuntimeError(
                        "unexpected GAP lifecycle after physical recovery close"
                    )
                fixture._observe_gap_event(event)
    except BaseException as error:
        primary = error
        if reject_dispatched:
            assert coordination_path is not None
            try:
                reject_gate.record_failure(coordination_path, error)
            except BaseException as publication_error:  # noqa: BLE001 - keep the original scenario failure
                error.add_note(
                    f"{coordination_mode} failure publication: {publication_error}"
                )
        if (
            not full_dispatched
            and not reject_dispatched
            and coordination_mode != "adapter-transition-attested"
        ):
            record_start_failure(error)
        raise
    finally:
        try:
            fixture.stop()
        except BaseException as cleanup_error:
            if reject_mode:
                assert coordination_path is not None
                try:
                    reject_gate.record_failure(
                        coordination_path, cleanup_error, cleanup=True
                    )
                except BaseException as publication_error:  # noqa: BLE001 - retain cleanup failure if publication also fails
                    cleanup_error.add_note(
                        f"{coordination_mode} cleanup publication: {publication_error}"
                    )
            if primary is None:
                raise
            primary.add_note(f"direct fixture cleanup failed: {cleanup_error}")
