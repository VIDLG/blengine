"""Long-lived direct BTP nRF fixture lifecycle for explicit RF lanes."""

from __future__ import annotations

import signal
import time
from pathlib import Path

COORDINATION_POLL_INTERVAL = 0.05


from .btp_commands import BtpCommandError
from .btp_protocol import BtpSessionError
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

    def start(self) -> dict[str, int]:
        try:
            self._gap.stop_advertising()
        except (BtpCommandError, BtpSessionError, EOFError, TimeoutError):
            # A fresh boot may not be advertising; reset remains the authoritative
            # database lifecycle boundary.
            pass
        self._gatt.reset_server()
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

    @staticmethod
    def _consume_marker(path: Path) -> bool:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _write_marker(path: Path) -> None:
        path.write_text("ready\n", encoding="ascii")

    def run_notification_coordination(
        self,
        coordination_path: Path,
        handles: dict[str, int],
        value_role: str = "updates",
    ) -> None:
        enabled = coordination_path / "notification-enabled"
        disabled = coordination_path / "notification-disabled"
        applied = coordination_path / "value-applied"
        post_disable = coordination_path / "post-disable-triggered"
        deadline = time.monotonic() + 240
        while not enabled.is_file():
            if self._stop_requested or time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for macOS notification enable")
            time.sleep(COORDINATION_POLL_INTERVAL)
        self._gatt.set_value(handles[value_role], self._profile.notification_value)
        applied.write_text("ready\n", encoding="ascii")
        while not disabled.is_file():
            if self._stop_requested or time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for macOS notification disable")
            time.sleep(COORDINATION_POLL_INTERVAL)
        post_disable.write_text("ready\n", encoding="ascii")

    def run_tauri_coordination(
        self,
        coordination_path: Path,
        handles: dict[str, int],
        value_role: str = "updates",
        auto_notification: bool = False,
    ) -> None:
        """Serve repeated Tauri RF control without yielding the BTP transport.

        The WebDriver process only creates marker files. This process remains
        the sole serial/BTP owner, observes GAP connection events while idle,
        applies notification values after CCCD enablement, and performs
        Peripheral-initiated disconnects on request. Completion markers are
        consumed by the caller so the same coordination directory can be used
        for multiple flow cycles.
        """
        notification_enabled = coordination_path / "notification-enabled"
        notification_disabled = coordination_path / "notification-disabled"
        value_applied = coordination_path / "value-applied"
        post_disable = coordination_path / "post-disable-triggered"
        passive_requested = coordination_path / "passive-requested"
        passive_disconnected = coordination_path / "passive-disconnected"
        peer: tuple[str, int] | None = None
        disconnect_pending = False
        notification_deadline = 0.0
        next_notification = 0.0

        while not self._stop_requested:
            event = self._session.poll_event(COORDINATION_POLL_INTERVAL)
            if event is not None and event.service == 1:
                if (
                    event.opcode == GAP_DEVICE_CONNECTED
                    and len(event.payload) >= 7
                ):
                    address_type = event.payload[0]
                    address = event.payload[1:7][::-1].hex()
                    peer = (address, address_type)
                    if auto_notification:
                        notification_deadline = time.monotonic() + 30.0
                        next_notification = 0.0
                elif event.opcode == GAP_DEVICE_DISCONNECTED:
                    peer = None
                    try:
                        self._start_advertising()
                    except BtpCommandError:
                        # Some BTP implementations resume advertising
                        # automatically after the link event. A redundant
                        # start is therefore best effort, not a fixture fault.
                        pass
                    notification_deadline = 0.0

            now = time.monotonic()
            if auto_notification and peer is not None and now < notification_deadline and now >= next_notification:
                self._gatt.set_value(handles[value_role], self._profile.notification_value)
                next_notification = now + 0.20

            if self._consume_marker(notification_enabled):
                self._gatt.set_value(handles[value_role], self._profile.notification_value)
                self._write_marker(value_applied)

            if self._consume_marker(notification_disabled):
                self._write_marker(post_disable)

            if self._consume_marker(passive_requested):
                disconnect_pending = True

            if disconnect_pending and peer is not None:
                self._gap.disconnect(*peer)
                peer = None
                try:
                    self._start_advertising()
                except BtpCommandError:
                    # A connected Peripheral may already have resumed its
                    # advertising state. A failed redundant start is not a
                    # reason to hide the completed disconnect from the test.
                    pass
                self._write_marker(passive_disconnected)
                disconnect_pending = False

    def run_passive_disconnect_coordination(self, coordination_path: Path) -> None:
        connected = coordination_path / "connected"
        triggered = coordination_path / "fault-triggered"
        deadline = time.monotonic() + 240
        while not connected.is_file():
            if self._stop_requested or time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for macOS connection")
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

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        try:
            self._gap.stop_advertising()
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
                fixture.run_notification_coordination(coordination_path, handles)
            elif coordination_mode == "indication":
                fixture.run_notification_coordination(
                    coordination_path, handles, value_role="indications"
                )
            elif coordination_mode == "passive-disconnect":
                fixture.run_passive_disconnect_coordination(coordination_path)
            elif coordination_mode == "tauri":
                fixture.run_tauri_coordination(coordination_path, handles)
            elif coordination_mode == "ios-tauri":
                fixture.run_tauri_coordination(
                    coordination_path, handles, auto_notification=True
                )
            else:
                raise ValueError(
                    f"unsupported direct RF coordination mode: {coordination_mode}"
                )
        while not fixture._stop_requested:
            time.sleep(0.2)
    finally:
        fixture.stop()
