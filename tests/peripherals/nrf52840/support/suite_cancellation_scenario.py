"""Per-case synthetic GATT scenarios for the suite cancellation regressions."""

from __future__ import annotations

import struct
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from .. import cancellation_control as controls
from .. import continuous_watchdog, fault_control, full_suite
from ..btp_commands import GAP, GATT
from ..btp_protocol import BtpFrame
from ..btp_session import BtpSession
from ..direct_fixture import DirectFixture
from ..gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
    GAP_DISCONNECT,
    GAP_START_ADVERTISING,
    GAP_STOP_ADVERTISING,
    GapCommands,
)
from ..gatt_commands import (
    GATT_GET_ATTRIBUTE_VALUE,
    GATT_GET_ATTRIBUTES,
    GATT_SET_VALUE,
    GattCommands,
    uuid_to_le_bytes,
)
from ..profile import load_profile
from .read_fault_emulator import PEER, Clock, Wire
from .session_receipts import fake_attempt


class SuiteFinished(Exception):
    """Stop at the next phase without running the independent watchdog scenario."""


class SuiteWire(Wire):
    """Local writable ATT plus the existing v2 engine emulator, never USB/RF."""

    def __init__(self, clock, attempt, profile, handles):
        super().__init__(clock, "")
        self.attempt, self.profile, self.handles = attempt, profile, handles
        self.values = {
            handles[c.role]: c.initial_value for c in profile.characteristics
        }
        self.trace = []
        self.general_queries = 0
        self.io_written = False
        self.preparing = self.resumed = False
        self.idles = self.reads = 0
        self.inject = lambda step: None
        self.step = ""
        self.target_permissions = 0x43
        self.read_att = 0
        self.ignore_write = False
        self.boot_drift = False
        self.prep_limits = []
        self.passive_phase = "pending"
        self.recovery_schedule = "queued"
        self.recovery_started = None
        self.recovery_stopped = False
        self.recovery_events = [
            BtpFrame(GAP, GAP_DEVICE_CONNECTED, 0, PEER + bytes(6)),
            BtpFrame(GAP, GAP_DEVICE_DISCONNECTED, 0, PEER),
        ]
        self.recovery_emitted = 0
        self.done_when_recovery_emitted = None

    def pump(self):
        """Synthetic Central publishes each flow-7 marker only after its dependency."""
        directory = self.attempt.root / "flow-7-rf-passive-disconnect"
        if (self.attempt.root / "flow-6-rf-indication/post-disable-triggered").exists():
            (directory / "started").touch()
        if (
            self.passive_phase == "original-connected"
            and (directory / "value-applied").exists()
        ):
            (directory / "connected").touch()
        if (
            self.passive_phase != "recovering"
            or not (directory / "recovery-ready").exists()
        ):
            return
        if self.recovery_started is None:
            self.recovery_started = self.clock.now
        elapsed = self.clock.now - self.recovery_started
        offsets, done_at = {
            "queued": ((0.0, 0.0), 0.0),
            "delayed": ((0.1, 0.2), 0.0),
            "split": ((0.0, 0.2), 0.1),
        }[self.recovery_schedule]
        while self.recovery_emitted < len(self.recovery_events):
            offset = offsets[min(self.recovery_emitted, 1)]
            if elapsed < offset:
                break
            event = self.recovery_events[self.recovery_emitted]
            self.recovery_emitted += 1
            if event.opcode == GAP_DEVICE_CONNECTED:
                self.connected = True
                self.epoch += 1
            elif event.opcode == GAP_DEVICE_DISCONNECTED:
                self.connected = False
            self.frames.append(event)
        if elapsed >= done_at:
            if self.done_when_recovery_emitted is None:
                self.done_when_recovery_emitted = self.recovery_emitted
            (directory / "done").touch()
            (self.attempt.directory / "started").touch()

    def receive(self, timeout):
        self.pump()
        return super().receive(timeout)

    def send(self, frame):
        service, opcode = frame.service, frame.opcode
        root = self.attempt.root
        if (
            service == GAP
            and opcode == GAP_STOP_ADVERTISING
            and (root / "flow-7-rf-passive-disconnect/recovery-ready").exists()
        ):
            if not self.recovery_stopped:
                self.recovery_stopped = True
                self.trace.append("flow7-close")
            else:
                self.preparing = not self.resumed
        step = ""
        if self.preparing:
            if service == GAP and opcode == GAP_STOP_ADVERTISING:
                step = "stop"
            elif service == 255 and opcode == fault_control.Opcode.GET_LIMITS:
                self.idles += 1
                step = f"idle-{self.idles}"
            elif service == GATT and opcode == GATT_GET_ATTRIBUTES:
                step = "attributes"
            elif service == GATT and opcode == GATT_GET_ATTRIBUTE_VALUE:
                self.reads += 1
                step = "before" if self.reads == 1 else "after"
            elif service == GATT and opcode == GATT_SET_VALUE:
                step = "set"
        if step:
            self.step = step
            self.trace.append(step)
            self.inject(step)
        if service == 255:
            super().send(frame)
            if self.preparing and opcode == fault_control.Opcode.GET_LIMITS:
                response = self.frames.pop()
                payload = response.payload
                if self.boot_drift:
                    payload = payload[:2] + bytes([99]) + payload[3:]
                self.frames.append(BtpFrame(service, opcode, frame.index, payload))
                self.prep_limits.append(fault_control.decode_limits(payload))
            return
        self.commands.append(frame)
        payload = b""
        if service == GAP and opcode == GAP_START_ADVERTISING:
            if self.preparing:
                # The synthetic Central cannot connect until the receipt exists.
                receipt = controls.read_document(
                    self.attempt.directory / "preparation-evidence.json"
                )
                assert (
                    receipt["readback"] == "00"
                    and self.values[self.handles["read-write"]] == b"\x00"
                )
                self.trace.append("resume")
                self.preparing, self.resumed = False, True
                self.gap(GAP_DEVICE_CONNECTED)
                controls.write_document(
                    self.attempt.directory / "request-arm.json",
                    asdict(self.attempt.control),
                )
            elif self.passive_phase == "ready-for-original":
                self.passive_phase = "original-connected"
                self.gap(GAP_DEVICE_CONNECTED)
                (root / "flow-7-rf-passive-disconnect/subscription-enabled").touch()
            elif self.passive_phase == "original-closed":
                self.passive_phase = "recovering"
            elif self.general_queries == 4 and not self.io_written:
                # Synthetic App's earlier rf-io write persists in the same ATT DB.
                self.values[self.handles["read-write"]] = self.remote_value = b"\x01"
                self.io_written = True
                self.trace.append("rf-io:01")
        elif service == GAP and opcode == GAP_STOP_ADVERTISING:
            pass
        elif service == GAP and opcode == GAP_DISCONNECT:
            assert self.passive_phase == "original-connected"
            self.gap(GAP_DEVICE_DISCONNECTED)
            self.passive_phase = "original-closed"
        elif service == GATT and opcode == GATT_GET_ATTRIBUTES:
            if frame.payload[-1:] == b"\x00":
                self.general_queries += 1
                if (
                    self.passive_phase == "pending"
                    and (root / "flow-7-rf-passive-disconnect/started").exists()
                ):
                    self.passive_phase = "ready-for-original"
                payload = b"\x00"
            else:
                raw_uuid = uuid_to_le_bytes(self.profile.characteristics[0].uuid)
                payload = (
                    b"\x01"
                    + struct.pack(
                        "<HBB",
                        self.handles["read-write"],
                        self.target_permissions,
                        len(raw_uuid),
                    )
                    + raw_uuid
                )
        elif service == GATT and opcode == GATT_GET_ATTRIBUTE_VALUE:
            handle = struct.unpack("<H", frame.payload[-2:])[0]
            value = self.values[handle]
            payload = struct.pack("<BH", self.read_att, len(value)) + value
        elif service == GATT and opcode == GATT_SET_VALUE:
            handle, length = struct.unpack("<HH", frame.payload[:4])
            value = frame.payload[4:]
            assert len(value) == length
            if not self.ignore_write:
                self.values[handle] = value
                if handle == self.handles["read-write"]:
                    self.remote_value = value
        else:
            raise AssertionError(f"unexpected suite command: {frame}")
        self.frames.append(BtpFrame(service, opcode, frame.index, payload))


class SuiteCancellationScenario:
    """One synthetic suite session, ATT database and marker tree; never live I/O."""

    def __init__(self, root: Path) -> None:
        self.attempt = fake_attempt(root)
        self.attempt.path.unlink()  # No previous cancellation success may authorize prep.
        self.profile = load_profile(self.attempt.plan.profile_path)
        self.handles = {
            c.role: 51 + 2 * i for i, c in enumerate(self.profile.characteristics)
        }
        self.clock = Clock()
        self.wire = SuiteWire(self.clock, self.attempt, self.profile, self.handles)
        self.fixture = DirectFixture.__new__(DirectFixture)
        self.fixture._session = BtpSession(self.wire)
        self.fixture._transport = Mock()
        self.fixture._fixture_session = self.attempt.plan
        self.fixture._profile = self.profile
        self.fixture._connected_peers = set()
        self.fixture._stop_requested = False
        self.fixture._gatt = GattCommands(self.fixture._session)
        self.fixture._gap = GapCommands(self.fixture._session)
        for i, mode in enumerate(full_suite.MODES):
            directory = self.attempt.root / f"flow-{i}-{mode}"
            directory.mkdir(exist_ok=True)
            if i < 7:
                (directory / "started").touch()
            if i < 7:
                for name in (
                    "done",
                    "subscription-enabled",
                    "subscription-disabled",
                    "connected",
                ):
                    (directory / name).touch()
        self.receipt = self.attempt.directory / "preparation-evidence.json"

    def guard(self):
        self.wire.pump()
        directory = self.attempt.directory
        for signal, request in (("armed", "read-dispatched"), ("held", "release")):
            path = directory / f"request-{request}.json"
            if (directory / f"signal-{signal}.json").exists() and not path.exists():
                if signal == "armed":
                    self.wire.capture_read()
                controls.write_document(path, asdict(self.attempt.control))
        if (directory / "signal-released.json").exists() and not (
            directory / "done"
        ).exists():
            self.wire.gap(GAP_DEVICE_DISCONNECTED)
            (directory / "done").touch()
            (self.attempt.root / "flow-9-rf-watchdog/started").touch()

    def run_suite(self, guard=None):
        def parent_guard():
            self.wire.pump()
            (guard or self.guard)()

        with (
            patch.object(time, "monotonic", self.clock.monotonic),
            patch.object(time, "sleep", self.clock.sleep),
            patch.object(
                continuous_watchdog, "restore_canonical", side_effect=SuiteFinished
            ) as next_phase,
        ):
            try:
                full_suite.run_continuous(
                    self.fixture,
                    self.attempt.root,
                    self.handles,
                    parent_guard=parent_guard,
                )
            except SuiteFinished:
                return next_phase
        raise AssertionError("suite did not reach the next phase")

    def preparation_offset(self):
        # Last stop before the preparation engine probes; prior passive disconnect is owned.
        stops = [
            i
            for i, f in enumerate(self.wire.commands)
            if f.service == GAP and f.opcode == GAP_STOP_ADVERTISING
        ]
        return stops[-1] if stops else len(self.wire.commands)

    def cancellation_log(self):
        from tests.support.rf_lab.markers import (
            CANCELLATION_MARKERS,
            controlled_markers,
            log,
        )

        return (
            "BLEHUB_NITRO_RUNTIME rf-lab-suite:flow-started:index=8:mode=rf-cancellation\n"
            + log(controlled_markers(CANCELLATION_MARKERS, self.attempt.control))
        )


@contextmanager
def suite_cancellation_scenario() -> Iterator[SuiteCancellationScenario]:
    """Own cleanup before setup, including setup failures and failed subcases."""
    with tempfile.TemporaryDirectory() as temporary:
        yield SuiteCancellationScenario(Path(temporary))
