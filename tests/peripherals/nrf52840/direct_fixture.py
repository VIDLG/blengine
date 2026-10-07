"""Long-lived direct BTP nRF fixture lifecycle for explicit RF lanes."""

from __future__ import annotations

import signal
import time
from pathlib import Path

COORDINATION_POLL_INTERVAL = 0.05


from .btp_commands import BtpCommandError
from .btp_protocol import BtpFrame, BtpSessionError
from .btp_session import BtpSession
from .btp_transport import FramedTransport
from .doctor import NrfDoctor
from .gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
    GapCommands,
)
from .gatt_commands import GattCommands, uuid_to_le_bytes
from .profile import load_profile
from .serial_transport import open_serial


class DirectFixture:
    def __init__(self, port: str, profile_path: Path) -> None:
        self._serial = open_serial(port)
        self._session = BtpSession(FramedTransport(self._serial))
        self._gap = GapCommands(self._session)
        self._gatt = GattCommands(self._session)
        self._profile = load_profile(profile_path)
        self._stopping = False
        self._stop_requested = False
        self._connected_peers: set[tuple[str, int]] = set()

    def start(self) -> dict[str, int]:
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
        return result.role_handles

    def _start_advertising(self) -> None:
        self._gap.start_advertising(
            {
                0x01: b"\x06",
                0x07: uuid_to_le_bytes(self._profile.service_uuid),
            },
            {0x09: self._profile.local_name.encode("utf-8")},
        )

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
                if event is not None and self._is_gap_event(event, GAP_DEVICE_DISCONNECTED):
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
            if event.service == 1 and event.opcode == 0x82 and len(event.payload) >= 7:
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
                    event.service == 1
                    and event.opcode == 0x82
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
            self._connected_peers.add((event.payload[1:7][::-1].hex(), event.payload[0]))
        elif event.opcode == GAP_DEVICE_DISCONNECTED:
            self._connected_peers.clear()

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
                if event.service == 1 and event.opcode == GAP_DEVICE_DISCONNECTED:
                    peers.clear()

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        try:
            self._disconnect_connected_peers()
        except (BtpCommandError, BtpSessionError, EOFError, TimeoutError):
            pass
        try:
            self._gap.stop_advertising()
        except (BtpCommandError, BtpSessionError, EOFError, TimeoutError):
            pass
        try:
            # A pending ATT indication can leave the resident dynamic GATT server
            # unable to accept the next profile's initial values. Reset only the
            # host-owned dynamic server at teardown; supported firmware may close
            # the BTP transport as part of this reset, which is also safe here.
            self._gatt.reset_server()
        except (BtpCommandError, BtpSessionError, EOFError, TimeoutError):
            pass
        finally:
            self._session.close()
            self._serial.close()


def serve(
    port: str,
    profile_path: Path,
    coordination_path: Path | None = None,
    coordination_mode: str = "notification",
    coordination_cycles: int = 1,
) -> None:
    fixture = DirectFixture(port, profile_path)
    try:
        handles = fixture.start()
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
        while not fixture._stop_requested:
            event = fixture._session.pop_event() or fixture._session.poll_event(0.2)
            if event is not None:
                fixture._observe_gap_event(event)
    finally:
        fixture.stop()
