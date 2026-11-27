"""Long-lived direct BTP nRF fixture lifecycle for explicit RF lanes."""

from __future__ import annotations

import math
import signal
import time
from builtins import BaseExceptionGroup, ExceptionGroup
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self

COORDINATION_POLL_INTERVAL = 0.05
# How long a controller-issued disconnect may take to be reported as the link's end. It is
# this command's own budget, not a scene's: LL supervision for a phone is well inside it, and
# a link that is genuinely still up must be named instead of waited on forever.
PEER_DROP_CONFIRM_SECONDS = 15.0


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
    from .fault_control import ArmedHold, Decision, Operation, Phase, Status
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


class FixtureConstructionError(BaseExceptionGroup):
    """Construction failed and local resource closure is uncertain.

    The initial group's ``exceptions[0]`` is the original construction failure;
    subsequent members are independent cleanup failures in unwind order. This is an ExceptionGroup
    when all members are Exceptions, otherwise an interruption-bearing group.
    Clean construction rejection never raises this marker.
    """

    def __new__(cls, message: str, exceptions: Sequence[BaseException]) -> Self:
        target = cls
        if cls is FixtureConstructionError and all(
            isinstance(error, Exception) for error in exceptions
        ):
            target = _FixtureConstructionExceptionGroup
        return super().__new__(target, message, exceptions)

    def derive(self, exceptions: Sequence[BaseException]) -> FixtureConstructionError:
        return FixtureConstructionError(self.message, exceptions)


class _FixtureConstructionExceptionGroup(FixtureConstructionError, ExceptionGroup):
    pass


class AdvertisingStateUnverified(RuntimeError):
    """Advertising inactivity cannot be proved by the available BTP readback."""


def require_resume_adoption_allowed(
    peer_present: bool, *, expect_adoption: bool
) -> None:
    """Refuse an adopted peer at resume unless the scene declared adoption expected.

    Kept as one decision rather than a condition inside a wire sequence: the default stays
    strict, and only a scene whose own claim is "connect when the peer appears" asks for the
    other answer.
    """
    if peer_present and not expect_adoption:
        raise RuntimeError("advertising resume adopted a peer")


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


def _deadline_remaining(
    deadline: float, operation: str = "passive advertising close"
) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{operation} deadline exceeded")
    return remaining


class _LeasedTransport:
    """Fail closed on broker loss before touching the fixture's serial stream."""

    def __init__(self, transport: ByteTransport, lease: DeviceLease) -> None:
        self._transport = transport
        self._lease = lease
        self.io_deadline: float | None = None
        self.deadline_operation = "passive advertising close"
        self.close_errors: list[BaseException] | None = None

    def require(self) -> None:
        kind, serial = self._lease.identity.split(":", 1)
        self._lease.require(kind, serial)

    def write(self, data: bytes) -> None:
        self.require()
        if self.io_deadline is not None:
            remaining = _deadline_remaining(self.io_deadline, self.deadline_operation)
            if isinstance(self._transport, SerialByteTransport):
                # Cap the actual pyserial write, not just the subsequent BTP read.
                port = self._transport._port
                previous = port.write_timeout
                port.write_timeout = (
                    remaining if previous is None else min(previous, remaining)
                )
        self._transport.write(data)
        if self.io_deadline is not None:
            _deadline_remaining(self.io_deadline, self.deadline_operation)

    def read(self, timeout: float) -> bytes:
        self.require()
        if self.io_deadline is not None:
            timeout = min(
                timeout,
                _deadline_remaining(self.io_deadline, self.deadline_operation),
            )
        data = self._transport.read(timeout)
        if self.io_deadline is not None:
            _deadline_remaining(self.io_deadline, self.deadline_operation)
        return data

    def close(self) -> None:
        try:
            self._transport.close()
        except BaseException as error:
            if self.close_errors is None:
                raise
            # BtpSession closes on timeout/EOF. A close failure must not replace
            # that primary failure before the owner can independently unwind.
            error.add_note("direct fixture: transport close")
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
        construction_cleanup: list[BaseException] = []
        self._close_errors = construction_cleanup
        resources = ExitStack()
        try:
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
            self._transport.close_errors = construction_cleanup
            self._session = BtpSession(FramedTransport(self._transport))
            resources.callback(self._close_resource, self._session.close)
            self._gap = GapCommands(self._session)
            self._gatt = GattCommands(self._session)
            self._profile = load_profile(profile_path)
            self._start_attempted = False
            self._started = False
            self._stopping = False
            self._stop_requested = False
            self._role_handles: dict[str, int] | None = None
            self._connected_peers: set[tuple[str, int]] = set()
            self._resources = resources
            self._transport.close_errors = None
            self._close_errors = None
        except BaseException as primary:
            self._close_errors = construction_cleanup
            transport = getattr(self, "_transport", None)
            if transport is not None:
                transport.close_errors = construction_cleanup
            # Close with no active construction error: lease release must not
            # replace it or hide an independent failure in exception notes.
            try:
                resources.close()
            except BaseException as cleanup:  # noqa: BLE001 - report lease unwind independently
                cleanup.add_note("direct fixture construction: lease scope release")
                construction_cleanup.append(cleanup)
            if construction_cleanup:
                raise FixtureConstructionError(
                    "direct fixture construction and cleanup failed",
                    [primary, *construction_cleanup],
                ) from None
            raise

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

    def local_address(self) -> str:
        """This peripheral's own BLE address, as its controller reports it.

        A static-random address is the identity a passive sniffer follows into the connection,
        so it is read back from the controller instead of assumed from a port or a profile. The
        read is bounded, issues no state change, and works while advertising or connected.
        """
        if not self._started or self._stopping or self._stop_requested:
            raise RuntimeError("direct fixture session is not active")
        info = self._gap.read_controller_info()
        if len(info) != CONTROLLER_INFO.size:
            raise BtpCommandError("GAP controller-info response has the wrong length")
        address = CONTROLLER_INFO.unpack(info)[0]
        # BTP carries the address little-endian; display order is most significant byte first.
        return ":".join(f"{byte:02X}" for byte in reversed(address))

    def start(
        self,
        deadline: float | None = None,
        *,
        before_mutations: Callable[[], None] = lambda: None,
    ) -> dict[str, int]:
        """Start once, optionally sharing a finite absolute monotonic I/O deadline.

        An explicit deadline is validated before startup I/O and bounds every BTP
        read/write, including fragments. Hooks and local work are not preempted;
        late completion fails without sending subsequent mutations. On any failure
        this path releases resources only, never disconnecting/resetting the DUT.
        Resource release uses its own lease/OS budget, not the startup deadline.
        Single failures retain identity; multiple failures are grouped primary first.
        """
        if deadline is None:
            return self._start(before_mutations=before_mutations)
        if self._start_attempted or self._stopping or self._stop_requested:
            raise RuntimeError("direct fixture start already attempted or stopped")
        release_errors: list[BaseException] = []
        self._close_errors = release_errors
        self._transport.close_errors = release_errors
        try:
            if (
                isinstance(deadline, bool)
                or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)
            ):
                raise ValueError(
                    "direct fixture startup deadline must be finite, not bool"
                )
            _deadline_remaining(deadline, "direct fixture startup")
            self._transport.io_deadline = deadline
            self._transport.deadline_operation = "direct fixture startup"
            port = (
                self._serial._port
                if isinstance(self._serial, SerialByteTransport)
                else None
            )
            previous_write_timeout = port.write_timeout if port is not None else None
            handles = self._start(deadline=deadline, before_mutations=before_mutations)
            if port is not None:
                port.write_timeout = previous_write_timeout
            _deadline_remaining(deadline, "direct fixture startup")
        except BaseException as primary:
            self._release_resources(release_errors)
            if release_errors:
                raise BaseExceptionGroup(
                    "direct fixture startup failed", [primary, *release_errors]
                ) from None
            raise
        self._transport.io_deadline = None
        self._transport.deadline_operation = "passive advertising close"
        self._transport.close_errors = None
        self._close_errors = None
        return handles

    def _start(
        self,
        *,
        deadline: float | None = None,
        before_mutations: Callable[[], None],
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
            if deadline is not None:
                _deadline_remaining(deadline, "direct fixture startup")
            self._mutation_allowed = True
        except Exception:
            self._mutation_allowed = False
            if deadline is None:
                self.stop()
            raise
        try:
            self._gap.stop_advertising()
        except (BtpCommandError, BtpSessionError, EOFError, TimeoutError) as error:
            # A fresh boot may not be advertising. Profile provisioning below is
            # the authoritative database boundary; GATT reset is not implemented
            # by every supported Zephyr Tester image.
            if deadline is not None and not isinstance(error, BtpCommandError):
                raise
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
            except (BtpCommandError, BtpSessionError, EOFError, TimeoutError) as error:
                # Resident firmware may reject a no-op state transition; the
                # subsequent advertising command remains the authoritative check.
                if deadline is not None and not isinstance(error, BtpCommandError):
                    raise
        self._start_advertising()
        handles = result.role_handles
        self._role_handles = handles
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

    def drain_events(
        self, *, limit: int = 64, record: list[tuple[int, bytes]] | None = None
    ) -> int:
        """Consume queued tester events through this owner's own observer.

        An in-process host is the only reader of this session, so it must pump the queue
        itself or GAP connect/disconnect events stay unread and a later passive close
        correctly refuses to run. This reads no state and issues no command.
        """
        if not self._started or self._stopping or self._stop_requested:
            raise RuntimeError("direct fixture session is not active")
        seen = 0
        for _ in range(limit):
            # The queue only holds frames some request already read; an idle owner must also
            # read the wire, exactly as the long-running fixture loop does.
            event = self._session.pop_event() or self._session.poll_event(0.01)
            if event is None:
                break
            if record is not None and event.service == 1:
                # Diagnostic only: the raw GAP frames this owner read and routed.
                record.append((event.opcode, event.payload))
            self._observe_gap_event(event)
            seen += 1
        return seen

    def observed_peers(self) -> frozenset[tuple[str, int]]:
        """Peers this owner has seen connect and not yet seen disconnect (read-only)."""
        return frozenset(self._connected_peers)

    def settle_no_peer(self, *, deadline: float) -> None:
        """Pump this owner's queue until the queue and the controller agree: no peer.

        A client connects and disconnects without the fixture ever being asked anything,
        so a scenario that ran through another stack leaves **both** GAP events in this
        queue. `observed_peers()` cannot see that: it only knows peers whose events were
        already drained, so it reads empty and a caller that waits only "while a peer is
        observed" skips the wait entirely and hands a non-empty queue to the one-shot
        passive close, which refuses it.

        Draining first, then checking, is therefore the only order that can conclude
        anything. Every caller that closes after a scenario goes through here, so the
        close policy exists once instead of once per runner.
        """
        while True:
            self.drain_events()
            if not self._connected_peers and not self.peer_present(deadline=deadline):
                return
            if _deadline_remaining(deadline, "peer wait") <= 5:
                raise RuntimeError(
                    "peer never disconnected before the close budget: "
                    f"observed={sorted(self._connected_peers)}"
                )
            time.sleep(0.2)

    def drop_peer(self, *, deadline: float | None = None) -> bytes:
        """End the link from the peer side and wait until the tester agrees it is gone.

        This is the peripheral dropping its own link, so the central sees a *remote* end
        rather than a command it issued. The address is never cached here: it is the fault
        engine's live `peer` when that engine holds a channel, and otherwise the tester's
        own GAP connection event — the same seven wire bytes, read from this owner's queue.

        A second connection is why the fallback exists: the engine's live peer is an ATT
        channel, and a central that has connected but whose channel the engine has not
        adopted leaves it null while the link is genuinely up. A drop that refuses there
        would report a missing peer the tester itself just watched connect.
        """
        from .fault_control import FaultController

        self._require_mutation_handles("peer drop")
        if self._fixture_session is None:
            raise RuntimeError("peer drop requires an attested fixture session")
        fault = FaultController(self._session, self._fixture_session.session_id)
        timeout = (
            1.0
            if deadline is None
            else min(1.0, _deadline_remaining(deadline, "peer drop"))
        )
        # The engine's `busy` bit is composed from the live fault slot *and* the GATT
        # target's pending-evidence flag, so ordinary ATT traffic leaves it set for a
        # moment. A peer drop is not a fault command, and sampling the guard once made a
        # later drop race an earlier read (measured on a ten-cycle recovery: the ninth drop
        # found the engine busy). The bound is unchanged: a fault that really is foreign
        # keeps the engine busy past it and is reported by name.
        idle_deadline = time.monotonic() + timeout

        def idle_limits() -> object:
            while True:
                limits = fault.get_limits(timeout=timeout)
                if not limits.engine_flags:
                    return limits
                if time.monotonic() >= idle_deadline:
                    return fault.require_idle(timeout=timeout)
                time.sleep(COORDINATION_POLL_INTERVAL)

        limits = idle_limits()
        fallback = not limits.peer_present
        peer = limits.peer if not fallback else self._single_observed_peer()
        self._gap.disconnect(peer[1:][::-1].hex(), peer[0])
        # A disconnect is accepted by the controller and settles on its own schedule, so the
        # wait for the end is bounded by this command's own budget and never by the caller
        # being patient. A caller that gave no budget still gets a reported end or a named
        # refusal instead of a scene that hangs until an unrelated timeout fires.
        settle_by = time.monotonic() + PEER_DROP_CONFIRM_SECONDS
        if deadline is not None:
            settle_by = min(settle_by, deadline)
        while True:
            self.drain_events()
            present = idle_limits().peer_present
            # A live channel in the fault engine *is* the connection fact. When it holds none,
            # the tester's own connect/disconnect events are this owner's only statement about
            # that link, so its end is claimed only once that peer is gone too.
            confirmed = not self._connected_peers if fallback else not present
            if confirmed:
                return peer
            if time.monotonic() >= settle_by:
                raise TimeoutError(
                    "the controller never confirmed the peer drop: "
                    f"peer_present={present} "
                    f"observed={sorted(self._connected_peers)} "
                    f"source={'observed' if fallback else 'fault-engine'}"
                )
            time.sleep(COORDINATION_POLL_INTERVAL)

    def _single_observed_peer(self) -> bytes:
        """The one peer this owner's own GAP events still hold, as seven wire bytes.

        Draining first is what makes the answer current: a connection this owner has not
        read yet is still in its queue, and a queue that is only *known* empty proves
        nothing. One connection at a time is the fixture's own limit, so more than one
        observed peer is a state this command cannot answer for.
        """
        self.drain_events()
        observed = sorted(self._connected_peers)
        if not observed:
            raise RuntimeError("peer drop requires a connected peer")
        if len(observed) > 1:
            raise RuntimeError(
                f"peer drop requires one connected peer: {len(observed)} observed"
            )
        address, address_type = observed[0]
        return bytes([address_type]) + bytes.fromhex(address)[::-1]

    def peer_present(self, *, deadline: float | None = None) -> bool:
        """Read-only controller fact: does the tester report a connected peer?

        A host that must let a central finish its own disconnect needs this fact before
        the one-shot passive close, which refuses to run while a peer is connected. This
        reads the controller only; it never disconnects, resets or adopts anything.
        """
        if (
            not self._started
            or self._stopping
            or self._stop_requested
            or not self._mutation_allowed
        ):
            raise RuntimeError("direct fixture session is not active")
        if self._fixture_session is None:
            raise RuntimeError("peer presence requires an attested fixture session")
        from .fault_control import FaultController

        timeout = (
            1.0
            if deadline is None
            else min(1.0, _deadline_remaining(deadline, "peer read"))
        )
        return (
            FaultController(self._session, self._fixture_session.session_id)
            .require_idle(timeout=timeout)
            .peer_present
        )

    def prepare_next_cycle(self, *, deadline: float) -> None:
        """Make this peer ready for another scene cycle: prove the link is gone, then advertise.

        A repeated scene must not assume either half. Proving absence without draining first reads
        a stale event queue, and `resume_advertising` refuses a peer that is still connected; doing
        the two in this order is the only sequence that can conclude anything.
        """
        self.settle_no_peer(deadline=deadline)
        self.resume_advertising(deadline=deadline)

    def resume_advertising(
        self, *, expect_adoption: bool = False, deadline: float | None = None
    ) -> None:
        """Make the peer discoverable again after a link it ended.

        The firmware stops connectable advertising when a link is established and does not
        restart it by itself, so a host that wants a *second* cycle must ask. This is one
        GAP advertising write plus the controller's own readback: no connection, reset,
        flash, retry or fault state is issued, and an unsupported readback is an explicit
        evidence gap rather than a pass.

        A peer that adopts the resumed advertisement inside the readback is normally a
        contradiction — except in the scene that states it: `auto-connect` exists to prove a
        standing "connect when the peer appears" request, so an immediate adoption is its
        expected answer, and refusing it made the scene a race against the App's own retry
        timer (measured on 2026-11-26: the same scene passed, then failed with
        "advertising resume adopted a peer" on the next sequence run). The scene, not the
        fixture, decides which answer it expects.
        """
        from .fault_control import FaultController

        self._require_mutation_handles("advertising resume")
        if self._fixture_session is None:
            raise RuntimeError(
                "advertising resume requires an attested fixture session"
            )
        timeout = (
            1.0
            if deadline is None
            else min(1.0, _deadline_remaining(deadline, "advertising resume"))
        )
        fault = FaultController(self._session, self._fixture_session.session_id)
        if fault.require_idle(timeout=timeout).peer_present:
            raise RuntimeError("advertising resume refuses a connected peer")
        self.drain_events()
        self._restart_advertising()
        info = self._gap.read_controller_info()
        if len(info) != CONTROLLER_INFO.size:
            raise BtpCommandError("GAP controller-info response has the wrong length")
        _, supported, current, _, _, _ = CONTROLLER_INFO.unpack(info)
        adopted = fault.require_idle(timeout=timeout).peer_present
        require_resume_adoption_allowed(adopted, expect_adoption=expect_adoption)
        if adopted:
            # A link *is* the proof: the controller stops connectable advertising when one is
            # established, so the advertising bit is clear **because** the peer adopted the
            # resumed advertisement. Requiring the bit here as well contradicted the scene
            # that asked for adoption (measured 2026-11-26: `auto-connect` failed with
            # "BTP controller is not advertising after resume" on the run right after the
            # adoption refusal itself was fixed).
            return
        if not supported & _GAP_SETTING_ADVERTISING:
            raise AdvertisingStateUnverified(
                "advertising resume evidence gap: ADVERTISING setting unsupported"
            )
        if not current & _GAP_SETTING_ADVERTISING:
            raise RuntimeError("BTP controller is not advertising after resume")

    def withdraw_advertising(self, *, deadline: float | None = None) -> None:
        """Make the peer undiscoverable while this attested session stays live.

        The mirror of `resume_advertising`, and the fixture half of a connect-failure scene:
        a central that already holds an advertisement from a previous scan must be given a
        peer that no longer answers, not a peer that was merely never seen. One GAP
        advertising stop plus the controller's own readback, so the absence is a fixture fact
        rather than an inference from a failed connect.

        A connected peer is refused: its advertising was already stopped by the link, so the
        readback would prove nothing about the central's next attempt. An unsupported
        readback is an explicit evidence gap, never a pass.
        """
        from .fault_control import FaultController

        self._require_mutation_handles("advertising withdrawal")
        if self._fixture_session is None:
            raise RuntimeError(
                "advertising withdrawal requires an attested fixture session"
            )
        timeout = (
            1.0
            if deadline is None
            else min(1.0, _deadline_remaining(deadline, "advertising withdrawal"))
        )
        fault = FaultController(self._session, self._fixture_session.session_id)
        if fault.require_idle(timeout=timeout).peer_present:
            raise RuntimeError("advertising withdrawal refuses a connected peer")
        self.drain_events()
        self._gap.stop_advertising()
        info = self._gap.read_controller_info()
        if len(info) != CONTROLLER_INFO.size:
            raise BtpCommandError("GAP controller-info response has the wrong length")
        _, supported, current, _, _, _ = CONTROLLER_INFO.unpack(info)
        if fault.require_idle(timeout=timeout).peer_present:
            raise RuntimeError("advertising withdrawal adopted a peer")
        if not supported & _GAP_SETTING_ADVERTISING:
            raise AdvertisingStateUnverified(
                "advertising withdrawal evidence gap: ADVERTISING setting unsupported"
            )
        if current & _GAP_SETTING_ADVERTISING:
            raise RuntimeError("BTP controller is still advertising after withdrawal")

    def arm_read_hold(self, role: str, *, deadline: float | None = None) -> ArmedHold:
        """ARM one remote read on `role` and return the ticket this fixture now holds."""
        from .fault_control import Operation

        return self.arm_hold(Operation.READ, role, deadline=deadline)

    def arm_write_hold(
        self, role: str, requested: bytes, *, deadline: float | None = None
    ) -> ArmedHold:
        """ARM one remote characteristic write on `role` for exactly `requested`.

        The request bytes are part of the binding, not an expectation after the fact: the
        firmware only captures the write that matches them, so a central that writes
        something else keeps its normal path and never becomes a hold.
        """
        from .fault_control import Operation

        return self.arm_hold(
            Operation.RESPONSE_WRITE, role, requested, deadline=deadline
        )

    def arm_ccc_hold(
        self,
        role: str,
        requested: bytes,
        *,
        before: bytes | None = None,
        deadline: float | None = None,
    ) -> ArmedHold:
        """ARM one remote CCC write on `role`'s managed descriptor for exactly `requested`.

        A CCC write is addressed by the descriptor, so the handle this binds is the one the
        server's own attribute table gives that role — never the characteristic value.
        `requested` is the exact u16 the central will write, which is also what makes an
        enable distinct from a disable: a no-op can never stand in for the operation a
        scene claims to be holding.

        `before` is the pre-state the caller already proved. The Tester value getter reads
        a managed CCC descriptor's *stored* value, not the state the central enabled, so a
        host that knows the state from the subscription itself states it here — and the
        firmware still re-checks it at capture, so a wrong claim never captures.
        """
        from .fault_control import Operation

        return self.arm_hold(
            Operation.CCC_WRITE, role, requested, before=before, deadline=deadline
        )

    def arm_hold(
        self,
        operation: Operation,
        role: str,
        requested: bytes = b"",
        *,
        before: bytes | None = None,
        deadline: float | None = None,
    ) -> ArmedHold:
        """ARM one remote operation and return the ticket this fixture now holds.

        ARM binds the firmware's boot/connection/bearer epochs and the exact live GAP
        peer, so a stale or foreign connection cannot be held and the host cannot mint a
        ticket for one. This method only arms: the request must still be dispatched by the
        central under test, which is why an armed ticket is not an observation.

        `before` is read through this fixture's own GATT server unless the caller already
        proved it. The firmware re-checks the expected before-state at capture and at
        commit, so a stale or invented claim is a hold that never captures.
        """
        from .fault_control import ArmedHold, FaultController, Operation

        if type(operation) is not Operation:
            raise TypeError("operation must be a fault_control.Operation")
        handles = self._require_mutation_handles("fault hold")
        if role not in handles:
            raise ValueError(
                f"direct fixture profile has no {role!r} characteristic handle"
            )
        if not isinstance(requested, bytes):
            raise TypeError("requested must be bytes")
        if operation is not Operation.READ and not requested:
            raise ValueError(f"{operation.name} hold requires the exact request bytes")
        if before is not None and not isinstance(before, bytes):
            raise TypeError("before must be bytes when the caller states it")
        if self._fixture_session is None:
            raise RuntimeError("fault hold requires an attested fixture session")
        target = (
            self.require_ccc_handle(role, deadline=deadline)
            if operation is Operation.CCC_WRITE
            else handles[role]
        )
        timeout = (
            1.0
            if deadline is None
            else min(1.0, _deadline_remaining(deadline, "fault hold"))
        )
        fault = FaultController(self._session, self._fixture_session.session_id)
        limits = fault.require_idle(timeout=timeout)
        if not limits.peer_present:
            raise RuntimeError(f"{operation.name} hold requires a connected peer")
        if before is None:
            observed = self._gatt.get_attribute_value(target)
            if observed.att_response != 0:
                raise RuntimeError(
                    f"fault hold could not read the current value: "
                    f"ATT {observed.att_response}"
                )
            declared = observed.value
        else:
            declared = before
        if operation is Operation.CCC_WRITE:
            if len(declared) != 2:
                raise RuntimeError("CCC hold requires the managed u16 descriptor value")
            if len(requested) != 2:
                raise ValueError("CCC hold request must be the exact u16")
        if operation is not Operation.READ and requested == declared:
            # The firmware would still capture this, but a no-op can never stand in for the
            # enable/disable a caller claims to hold, so refuse it as a caller error.
            raise ValueError(
                f"{operation.name} hold request is a no-op on the current value"
            )
        ticket = fault.arm_bound(
            operation, target, declared, requested, timeout=timeout
        ).ticket
        return ArmedHold(
            ticket=ticket,
            operation=operation,
            handle=target,
            before=declared,
            requested=requested,
            peer=limits.peer,
        )

    def require_ccc_handle(self, role: str, *, deadline: float | None = None) -> int:
        """Resolve one role's managed CCC descriptor handle from the native inventory.

        A CCC write is addressed by its descriptor, and the only authority for that handle
        is the server's own ordered attribute table: the value entry the role names, then
        the single `0x2902` entry before the next declaration or service boundary. Nothing
        here is taken from a profile layout claim, and an ambiguous table is refused
        instead of resolved by position.
        """
        handles = self._require_mutation_handles("CCC hold")
        if role not in handles:
            raise ValueError(
                f"direct fixture profile has no {role!r} characteristic handle"
            )
        value_handle = handles[role]
        attributes = self._gatt.get_attributes()
        index = next(
            (
                position
                for position, item in enumerate(attributes)
                if item.handle == value_handle
            ),
            None,
        )
        if index is None:
            raise RuntimeError(
                f"native inventory does not hold the {role!r} value handle "
                f"0x{value_handle:04x}"
            )
        following = attributes[index + 1 :]
        end = next(
            (
                item.handle
                for item in following
                if item.uuid in ("2800", "2801", "2803")
            ),
            0x10000,
        )
        descriptors = [
            item for item in following if item.handle < end and item.uuid == "2902"
        ]
        if len(descriptors) != 1:
            raise RuntimeError(
                f"{role!r} does not own exactly one CCC descriptor: "
                f"{[item.handle for item in descriptors]}"
            )
        if descriptors[0].permissions != 3:
            raise RuntimeError(
                f"{role!r} CCC descriptor is not readable and writable: "
                f"permissions=0x{descriptors[0].permissions:02x}"
            )
        return descriptors[0].handle

    def hold_phase(self, hold: ArmedHold, *, deadline: float | None = None) -> Phase:
        """Read one armed ticket's phase without waiting for the hold to capture.

        A scene that has to decide while the App is running cannot block until HELD: it polls
        this and resolves the hold itself. The phase is the firmware's own statement, never
        inferred from the App's behaviour.
        """
        from .fault_control import FaultController

        self._require_mutation_handles("fault hold")
        if self._fixture_session is None:
            raise RuntimeError("fault hold requires an attested fixture session")
        timeout = (
            1.0
            if deadline is None
            else min(1.0, _deadline_remaining(deadline, "fault hold"))
        )
        fault = FaultController(self._session, self._fixture_session.session_id)
        return fault.status(hold.ticket, timeout=timeout).phase

    def require_hold_held(
        self, hold: ArmedHold, *, deadline: float | None = None
    ) -> None:
        """Wait until the peer is holding the armed operation, never inventing one.

        ARMED means the engine is ready and nothing has arrived; HELD is the firmware's
        own statement that the exact remote request is parked inside it. A ticket that
        leaves ARMED in any other direction is reported instead of polled until a timeout.
        """
        from .fault_control import FaultController, Phase

        self._require_mutation_handles("fault hold")
        if self._fixture_session is None:
            raise RuntimeError("fault hold requires an attested fixture session")
        fault = FaultController(self._session, self._fixture_session.session_id)
        while True:
            timeout = (
                1.0
                if deadline is None
                else min(1.0, _deadline_remaining(deadline, "fault hold"))
            )
            snapshot = fault.status(hold.ticket, timeout=timeout)
            if snapshot.phase is Phase.HELD:
                return
            if snapshot.phase is not Phase.ARMED:
                raise RuntimeError(
                    f"armed {hold.operation.name.lower()} left ARMED without a held "
                    f"request: phase={snapshot.phase.name} outcome={snapshot.outcome.name}"
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"{hold.operation.name.lower()} never reached HELD")
            time.sleep(COORDINATION_POLL_INTERVAL)

    def release_hold(
        self,
        hold: ArmedHold,
        decision: Decision,
        *,
        deadline: float | None = None,
    ) -> Status:
        """Resolve one held operation with the peer's own decision and wait for retirement.

        COMMIT must produce the normal completion that operation is defined to have: a
        read answered in order, or a write whose callback ran and whose after-snapshot is
        exactly the request. REJECT must produce the firmware's refused receipt: request
        copied, response ordered, no callback, no commit, stored value unchanged. A ticket
        that retires any other way is a failure, not a slow operation. The retired receipt
        is returned so the caller can state what the peer decided instead of inferring it
        from a symptom.
        """
        from .fault_control import (
            Decision,
            FaultController,
            Operation,
            Phase,
            injected_reject,
            mutation_committed,
            read_completed,
        )

        if type(decision) is not Decision:
            raise TypeError("decision must be a fault_control.Decision")
        self._require_mutation_handles("fault hold")
        if self._fixture_session is None:
            raise RuntimeError("fault hold requires an attested fixture session")
        fault = FaultController(self._session, self._fixture_session.session_id)
        timeout = (
            1.0
            if deadline is None
            else min(1.0, _deadline_remaining(deadline, "fault release"))
        )
        fault.resolve(hold.ticket, decision, timeout=timeout)
        while True:
            timeout = (
                1.0
                if deadline is None
                else min(1.0, _deadline_remaining(deadline, "fault release"))
            )
            snapshot = fault.status(hold.ticket, timeout=timeout)
            if snapshot.phase is Phase.QUARANTINED:
                raise RuntimeError(
                    "held operation was quarantined: "
                    f"outcome={snapshot.outcome.name} "
                    f"infrastructure={snapshot.infrastructure_error.name}"
                )
            if snapshot.phase is Phase.RETIRED:
                settled = (
                    injected_reject(snapshot)
                    if decision is Decision.REJECT
                    else read_completed(snapshot)
                    if hold.operation is Operation.READ
                    else mutation_committed(snapshot, hold.requested)
                )
                if settled:
                    return snapshot
                wanted = (
                    "rejection" if decision is Decision.REJECT else "normal completion"
                )
                raise RuntimeError(
                    f"held {hold.operation.name.lower()} retired without a {wanted}: "
                    f"winner={snapshot.winner.name} outcome={snapshot.outcome.name} "
                    f"flags=0x{int(snapshot.flags):03x} before={snapshot.before.hex()} "
                    f"after={snapshot.after.hex()}"
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"held {hold.operation.name.lower()} did not retire after RESOLVE"
                )
            time.sleep(COORDINATION_POLL_INTERVAL)

    def _require_mutation_handles(self, operation: str) -> dict[str, int]:
        """This session's role handles, under the same active-session guard as writes."""
        if (
            not self._started
            or self._stopping
            or self._stop_requested
            or not self._mutation_allowed
        ):
            raise RuntimeError(f"direct fixture session is not active for {operation}")
        if self._role_handles is None:
            raise RuntimeError("direct fixture started without role handles")
        return self._role_handles

    def publish_notification(self, value_role: str) -> bytes:
        """Write the profile's notification value once, through this live session.

        The firmware only emits when the peer's CCC is already enabled, so the
        caller must have observed its own successful enable first. The return value
        is the exact payload this fixture applied; it is a fixture-side fact, never
        proof that a central received it.
        """
        if (
            not self._started
            or self._stopping
            or self._stop_requested
            or not self._mutation_allowed
        ):
            raise RuntimeError("direct fixture session is not active")
        handles = self._role_handles
        if handles is None:
            raise RuntimeError("direct fixture started without role handles")
        if value_role not in handles:
            raise ValueError(
                f"direct fixture profile has no {value_role!r} characteristic handle"
            )
        value = self._profile.notification_value
        self._gatt.set_value(handles[value_role], value)
        return value

    def publish_notification_sequence(
        self, value_role: str, count: int, *, gap_seconds: float = 0.04
    ) -> int:
        """Write this profile's notification value `count` times over one live session.

        The firmware fixes one length per characteric value, so every write reuses the
        profile's own payload; what varies is only how many admissions the peer makes.
        A gap keeps the peer's TX queue from being the limiter, so a bounded-consumer
        result is about the central's budget, not about controller buffering.
        """
        if type(count) is not int or not 1 <= count <= 512:
            raise ValueError("notification sequence needs 1..512 values")
        if not math.isfinite(gap_seconds) or gap_seconds < 0:
            raise ValueError("notification sequence gap must be finite and >= 0")
        for _ in range(count):
            self.publish_notification(value_role)
            if gap_seconds:
                time.sleep(gap_seconds)
        return count

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
            error.add_note("direct fixture: owned resource close")
            self._close_errors.append(error)

    def _release_resources(self, errors: list[BaseException]) -> None:
        self._started = False
        self._stopping = True
        self._mutation_allowed = False
        self._close_errors = errors
        self._transport.close_errors = errors
        try:
            self._resources.close()
        except BaseException as error:  # noqa: BLE001 - callbacks must not hide lease-release interruption
            error.add_note("direct fixture: lease scope release")
            errors.append(error)

    def close_resources(self) -> None:
        """Retire local resources only, from any state, without any BTP commands.

        Idempotent even after failed startup/close. Independently close session,
        serial and lease scope, preserving all failures (including interruptions).
        Consumes this fixture: later start is refused and legacy stop is a no-op.
        Returns no receipt and makes no claim about advertising or peer state.
        """
        errors: list[BaseException] = []
        self._release_resources(errors)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("direct fixture resource close failed", errors)

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
            _deadline_remaining(deadline)
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
            self._transport.io_deadline = deadline
            fault = FaultController(session, plan.session_id)

            def require_no_peer() -> None:
                self._transport.require()
                limits = fault.require_idle(
                    timeout=min(1.0, _deadline_remaining(deadline))
                )
                self._transport.require()
                _deadline_remaining(deadline)
                if limits.boot_nonce != boot_nonce:
                    raise RuntimeError("passive close refuses changed fixture boot")
                if limits.peer_present or self._connected_peers:
                    raise RuntimeError("passive close refuses a connected peer")
                # A fresh RX snapshot is necessary but not enough if a peer event
                # raced with it. Do not adopt, discard or disconnect that peer.
                for _ in range(256):
                    _deadline_remaining(deadline)
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
            _deadline_remaining(deadline)
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
                _deadline_remaining(deadline)
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
    reject_mode = coordination_mode == "ccc-disable-reject-attested"
    reject_gate = None
    if reject_mode:
        from . import ccc_disable_reject as reject_gate

        if coordination_path is None:
            raise ValueError(f"{coordination_mode} requires --coordination-path")
        reject_gate.validate_control(coordination_path, fixture_session)

    def record_start_failure(error: BaseException) -> None:
        if reject_mode:
            assert coordination_path is not None and fixture_session is not None
            reject_gate.record_start_failure(coordination_path, fixture_session, error)

    try:
        fixture = DirectFixture(port, profile_path, fixture_session=fixture_session)
    except BaseException as error:
        record_start_failure(error)
        raise
    fixture._preserve_gatt = fixture_session is not None
    fixture._strict_recovery_cleanup = reject_mode
    primary: BaseException | None = None
    reject_dispatched = False
    try:
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
        if coordination_mode == "system-loss-attested":
            from .system_loss_attested import run

            assert coordination_path is not None
            run(fixture, coordination_path, handles, on_ready=ready)
            return
        ready()
        if coordination_path is not None:
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
            elif reject_mode:
                reject_dispatched = True
                reject_gate.run(fixture, coordination_path, handles)
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
                if reject_mode and (
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
        if not reject_dispatched:
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
