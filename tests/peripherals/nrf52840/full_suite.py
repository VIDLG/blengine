"""Compose continuous10 and strict RecoveryTen without another physical owner.

The host owns capture, URL delivery and the recovery-only log projection. Ready is
an immutable physical handoff receipt, not a final verdict. All tests are cooperative
attestations; no marker alone is evidence of a disconnected Peripheral.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import cancellation_control as controls
from . import continuous_watchdog as watchdog
from . import recovery_ten as recovery
from .btp_commands import GAP, BtpCommandError
from .btp_protocol import BtpFrame
from .fault_control import (
    FaultController,
    Outcome,
    Ticket,
    Winner,
    cleanup_ready,
    encode_status,
    ticket_document,
    ticket_from_document,
)
from .fixture_session import (
    FixtureSession,
    _json,
    _publish_receipt,
    _read_bytes,
    read_live_snapshot,
    verify_receipt,
)
from .gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED
from .gatt_commands import tester_characteristic_native_permissions

MODE = "rf-lab-full-attested"
HANDOFF_NAME = "full-suite-handoff.json"
PHYSICAL_NAME = "full-suite-physical.json"
PREFIX = "BLEHUB_NITRO_RUNTIME "
MODES = (
    "rf",
    "rf-connect",
    "rf-gatt",
    "rf-io",
    "rf-rssi",
    "rf-notification",
    "rf-indication",
    "rf-passive-disconnect",
    "rf-cancellation",
    "rf-watchdog",
)
BASE_PASSED = "rf-lab-suite:passed:flows=10:passedCount=10:failureCount=0"
PHASE_SECONDS = 90.0
WHOLE_SECONDS = 2800.0


def _suite_passive_gap_peer(event: BtpFrame, phase: str) -> tuple[str, int] | None:
    """Validate the owned flow-7 wire before using any peer identity."""
    if (
        type(event.service) is not int
        or type(event.opcode) is not int
        or type(event.index) is not int
        or type(event.payload) is not bytes
    ):
        raise RuntimeError(f"suite passive {phase}: malformed event {event!r}")
    if event.service != GAP or event.opcode not in (
        GAP_DEVICE_CONNECTED,
        GAP_DEVICE_DISCONNECTED,
    ):
        return None
    if (
        event.index != 0
        or len(event.payload) != (13 if event.opcode == GAP_DEVICE_CONNECTED else 7)
        or event.payload[0] not in range(4)
    ):
        raise RuntimeError(f"suite passive {phase}: malformed GAP event {event!r}")
    return (event.payload[1:7][::-1].hex(), event.payload[0])


def _close_suite_passive_recovery(
    fixture: Any,
    directory: Path,
    peer: tuple[str, int],
    events: list[dict[str, object]],
    *,
    deadline: float,
    parent_guard: Callable[[], None],
) -> dict[str, object]:
    """Observe the original peer's one recovery lifecycle, not a new owner."""
    deadline = min(deadline, time.monotonic() + 60)
    connected = closed = False
    backlog = 0

    def guard() -> None:
        parent_guard()
        fixture._transport.require()
        if (
            fixture._stop_requested
            or (directory / "abort").exists()
            or (directory.parent / "abort").exists()
        ):
            raise RuntimeError(f"suite passive recovery close aborted: expected={peer}")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"suite passive recovery close deadline: expected={peer} "
                f"connected={connected} closed={closed} done={(directory / 'done').exists()}"
            )

    guard()
    if fixture._connected_peers:
        raise RuntimeError(
            f"suite passive recovery tracker not empty: {fixture._connected_peers!r}"
        )
    while True:
        guard()
        event = fixture._session.pop_event() or fixture._session.poll_event(
            min(controls.POLL_SECONDS, deadline - time.monotonic())
        )
        guard()
        if event is None:
            backlog = 0
            if closed and (directory / "done").is_file():
                return {
                    "peer": list(peer),
                    "events": events,
                    "advertising_stopped": True,
                    "peer_count": 0,
                    "monotonic": time.monotonic(),
                }
            continue
        backlog += 1
        if backlog > 256:
            raise RuntimeError("suite passive recovery event backlog exceeded 256")
        observed = _suite_passive_gap_peer(event, "recovery close")
        if observed is None:
            continue
        if (
            observed != peer
            or closed
            or (
                event.opcode == GAP_DEVICE_CONNECTED
                and (connected or fixture._connected_peers)
            )
            or (
                event.opcode == GAP_DEVICE_DISCONNECTED
                and (not connected or fixture._connected_peers != {peer})
            )
        ):
            raise RuntimeError(
                f"suite passive recovery close unexpected GAP: expected={peer} "
                f"connected={connected} closed={closed} event={event!r}"
            )
        events.append(
            {
                "opcode": event.opcode,
                "index": event.index,
                "payload": event.payload.hex(),
                "monotonic": time.monotonic(),
            }
        )
        if event.opcode == GAP_DEVICE_CONNECTED:
            connected = True
            fixture._connected_peers.add(peer)
        else:
            fixture._connected_peers.remove(peer)
            closed = True
            guard()
            fixture._gap.stop_advertising()
            guard()


def _prepare_suite_cancellation(
    fixture: Any,
    root: Path,
    handles: dict[str, int],
    *,
    deadline: float,
    parent_guard: Callable[[], None],
    passive_recovery_close: dict[str, object] | None,
) -> dict[str, object]:
    """Restore only flow 8's RW precondition, without adopting any peer/hold."""
    import os
    from dataclasses import asdict

    from .cancellation_control import (
        require_multifunction,
        verify_control,
        write_document,
    )
    from .continuous_watchdog import _require_no_peer
    from .fault_control import FaultController, Limits

    deadline = min(deadline, time.monotonic() + 10)
    if passive_recovery_close is None:
        raise RuntimeError(
            "cancellation preparation lacks owned passive recovery close"
        )
    plan = require_multifunction(fixture._fixture_session)
    identity = (os.getpid(), os.getpgid(0))
    control = verify_control(root, plan, identity)
    directory = root / control.directory
    receipt = directory / "preparation-evidence.json"
    rw = next(c for c in fixture._profile.characteristics if c.role == "read-write")
    target = handles[rw.role]
    if rw.initial_value != b"\x00" or rw.permissions != 0x03:
        raise ValueError("cancellation requires ordinary RW with initial 00")
    if receipt.exists() or receipt.is_symlink():
        raise RuntimeError("cancellation refuses stale preparation evidence")
    fault = FaultController(fixture._session, plan.session_id)
    boot = None

    def guard() -> None:
        parent_guard()
        fixture._transport.require()
        if (
            fixture._stop_requested
            or (root / "abort").exists()
            or (directory / "abort").exists()
        ):
            raise RuntimeError("cancellation preparation aborted by owner")
        if time.monotonic() >= deadline:
            raise TimeoutError("cancellation preparation deadline exceeded")

    def no_peer() -> None:
        guard()
        _require_no_peer(fixture, deadline)
        guard()

    def idle() -> Limits:
        nonlocal boot
        guard()
        limits = fault.require_idle(timeout=min(1.0, deadline - time.monotonic()))
        no_peer()
        if limits.peer_present or (boot is not None and limits.boot_nonce != boot):
            raise RuntimeError("cancellation preparation peer/boot drift")
        boot = limits.boot_nonce
        return limits

    guard()
    fixture._gap.stop_advertising()
    no_peer()
    idle()
    attributes = fixture._gatt.get_attributes(attribute_uuid=rw.uuid)
    no_peer()
    if (
        len(attributes) != 1
        or attributes[0].handle != target
        or attributes[0].uuid != rw.uuid
        or attributes[0].permissions
        != tester_characteristic_native_permissions(rw.permissions)
    ):
        raise RuntimeError("cancellation preparation target/profile mismatch")
    before = fixture._gatt.get_attribute_value(target)
    no_peer()
    if before.att_response != 0:
        raise RuntimeError("cancellation preparation before ATT read failed")
    # Earlier rf-io writes persist on this same database. Recheck ownership
    # after the local read, immediately before changing this exact handle.
    idle()
    fixture._gatt.set_value(target, rw.initial_value)
    no_peer()
    after = fixture._gatt.get_attribute_value(target)
    no_peer()
    if after.att_response != 0 or after.value != rw.initial_value:
        raise RuntimeError("cancellation preparation readback mismatch")
    limits = idle()
    directory.mkdir(exist_ok=True)
    guard()
    preparation = {
        "schema_version": 1,
        "status": "prepared",
        "control": asdict(control),
        "session_id": plan.session_id,
        "contract_sha256": plan.contract_sha256,
        "profile_sha256": plan.profile_sha256,
        "resource_identity": plan.resource_identity,
        "fixture_pid": identity[0],
        "fixture_pgid": identity[1],
        "target_uuid": rw.uuid,
        "target_handle": target,
        "before": before.value.hex(),
        "expected": rw.initial_value.hex(),
        "readback": after.value.hex(),
        "boot_nonce": limits.boot_nonce.hex(),
        "engine_flags": int(limits.engine_flags),
        "peer_present": limits.peer_present,
        "peer_count": len(fixture._connected_peers),
        "monotonic": time.monotonic(),
        "passive_recovery_close": passive_recovery_close,
    }
    write_document(receipt, preparation)
    guard()
    return preparation


def run_continuous(
    fixture: Any,
    coordination_path: Path,
    handles: dict[str, int],
    *,
    parent_guard: Callable[[], None] = lambda: None,
) -> None:
    """Keep one Peripheral/BTP generation alive across all ten RN RF flows."""
    deadline = time.monotonic() + 1800
    passive_recovery_close = None

    def wait_for_any(paths: tuple[Path, ...], label: str) -> int:
        while True:
            parent_guard()
            fixture._transport.require()
            if fixture._stop_requested or time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for suite {label}")
            for index, path in enumerate(paths):
                if path.is_file():
                    return index
            time.sleep(controls.POLL_SECONDS)

    def flow_path(index: int, mode: str) -> Path:
        return coordination_path / f"flow-{index}-{mode}"

    for index, mode in enumerate(MODES):
        directory = flow_path(index, mode)
        started = directory / "started"
        done = directory / "done"
        abort = directory / "abort"
        wait_for_any((started,), f"{index}:{mode}:started")
        if mode == "rf-watchdog":
            from .continuous_watchdog import restore_canonical, run_connected

            restored = restore_canonical(fixture, coordination_path, handles)
            fixture._restart_advertising()
            run_connected(
                fixture,
                coordination_path,
                handles,
                restored,
                parent_guard=parent_guard,
            )
            continue
        if mode == "rf-cancellation":
            # Do not let permissive teardown drain/adopt an unowned GAP event
            # before the strict no-peer preparation boundary.
            preparation = _prepare_suite_cancellation(
                fixture,
                coordination_path,
                handles,
                deadline=deadline,
                parent_guard=parent_guard,
                passive_recovery_close=passive_recovery_close,
            )
            fixture._start_advertising()
            fixture.run_cancellation_coordination(
                coordination_path,
                handles,
                parent_guard=parent_guard,
                preparation=preparation,
            )
            # Keep advertising off until watchdog no-peer preparation finishes.
            fixture._gap.stop_advertising()
            continue

        fixture._disconnect_connected_peers()
        fixture._restart_advertising()
        if abort.is_file():
            fixture._restart_advertising()
            continue

        if mode in ("rf-notification", "rf-indication"):
            value_role = "indications" if mode == "rf-indication" else "updates"
            enabled = directory / "subscription-enabled"
            disabled = directory / "subscription-disabled"
            applied = directory / "value-applied"
            post_disable = directory / "post-disable-triggered"
            if wait_for_any((enabled, abort), f"{index}:{mode}:enable") == 1:
                fixture._restart_advertising()
                continue
            fixture._gatt.set_value(
                handles[value_role], fixture._profile.notification_value
            )
            applied.write_text("ready\n", encoding="ascii")
            if wait_for_any((disabled, abort), f"{index}:{mode}:disable") == 1:
                fixture._restart_advertising()
                continue
            post_disable.write_text("ready\n", encoding="ascii")
            _ = wait_for_any((done, abort), f"{index}:{mode}:done")
            fixture._restart_advertising()
            continue

        if mode == "rf-passive-disconnect":
            enabled = directory / "subscription-enabled"
            connected = directory / "connected"
            applied = directory / "value-applied"
            triggered = directory / "fault-triggered"
            recovery_ready = directory / "recovery-ready"
            if wait_for_any((enabled, abort), f"{index}:{mode}:enable") == 1:
                fixture._restart_advertising()
                continue
            fixture._gatt.set_value(
                handles["updates"], fixture._profile.notification_value
            )
            applied.write_text("ready\n", encoding="ascii")
            if wait_for_any((connected, abort), f"{index}:{mode}:connected") == 1:
                fixture._restart_advertising()
                continue

            fixture._gatt.get_attributes()
            peer: tuple[str, int] | None = None
            peer_events: list[dict[str, object]] = []
            for _ in range(256):
                parent_guard()
                fixture._transport.require()
                if fixture._stop_requested or time.monotonic() >= deadline:
                    raise TimeoutError("suite passive original peer deadline")
                event = fixture._session.pop_event()
                if event is None:
                    break
                observed = _suite_passive_gap_peer(event, "original connect")
                if observed is None:
                    continue
                if event.opcode != GAP_DEVICE_CONNECTED or peer is not None:
                    raise RuntimeError(
                        f"suite passive original unexpected GAP: {event!r}"
                    )
                peer = observed
                peer_events.append(
                    {
                        "opcode": event.opcode,
                        "index": event.index,
                        "payload": event.payload.hex(),
                        "monotonic": time.monotonic(),
                    }
                )
            else:
                raise RuntimeError("suite passive original event backlog exceeded 256")
            if peer is None:
                raise BtpCommandError(
                    "suite passive-disconnect did not observe GAP peer"
                )
            triggered.write_text("ready\n", encoding="ascii")
            fixture._gap.disconnect(*peer)
            disconnected_deadline = min(deadline, time.monotonic() + 30)
            while time.monotonic() < disconnected_deadline:
                parent_guard()
                fixture._transport.require()
                if fixture._stop_requested or abort.exists():
                    raise RuntimeError("suite passive original close aborted")
                event = fixture._session.pop_event() or fixture._session.poll_event(
                    min(0.2, disconnected_deadline - time.monotonic())
                )
                parent_guard()
                fixture._transport.require()
                if time.monotonic() >= disconnected_deadline:
                    raise TimeoutError("suite passive original close deadline")
                if event is not None:
                    observed = _suite_passive_gap_peer(event, "original close")
                    if observed is None:
                        continue
                    if event.opcode != GAP_DEVICE_DISCONNECTED or observed != peer:
                        raise RuntimeError(
                            f"suite passive original close expected={peer} event={event!r}"
                        )
                    peer_events.append(
                        {
                            "opcode": event.opcode,
                            "index": event.index,
                            "payload": event.payload.hex(),
                            "monotonic": time.monotonic(),
                        }
                    )
                    break
            else:
                raise TimeoutError(
                    "suite passive-disconnect did not observe GAP disconnect"
                )
            fixture._restart_advertising()
            recovery_ready.write_text("ready\n", encoding="ascii")
            passive_recovery_close = _close_suite_passive_recovery(
                fixture,
                directory,
                peer,
                peer_events,
                deadline=deadline,
                parent_guard=parent_guard,
            )
            continue

        _ = wait_for_any((done, abort), f"{index}:{mode}:done")
        fixture._restart_advertising()


def prepare_control(root: Path, plan: FixtureSession) -> None:
    """Runner preparation only: add the recovery control to existing suite controls."""
    controls.require_multifunction(plan)
    if any(c.session != plan.session_id for c in controls.load_suite_controls(root)):
        raise ValueError("full suite control session drift")
    directory = root / "recovery"
    directory.mkdir(mode=0o700, exist_ok=False)
    _publish_receipt(
        directory / recovery.CONTROL_NAME,
        {
            "schema_version": 1,
            "run_id": plan.session_id,
            "cycles": 10,
        },
    )


def validate_control(root: Path, plan: FixtureSession | None) -> FixtureSession:
    """Fail before opening serial or inspecting a running provider identity."""
    plan = controls.require_multifunction(plan)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("full suite requires a private control directory")
    if {p.name for p in root.glob("*-control.json")} != {
        controls.CONTROL_NAME,
        controls.WATCHDOG_CONTROL_NAME,
    } or any(c.session != plan.session_id for c in controls.load_suite_controls(root)):
        raise ValueError("full suite control/session drift")
    recovery.validate_control(root / "recovery", plan)
    for name in (HANDOFF_NAME, PHYSICAL_NAME, "abort"):
        path = root / name
        if path.exists() or path.is_symlink():
            raise ValueError(f"full suite refuses stale output/cancellation: {path}")
    for index, mode in enumerate(MODES):
        path = root / f"flow-{index}-{mode}"
        if (
            path.is_symlink()
            or path.exists()
            and (not path.is_dir() or any(path.iterdir()))
        ):
            raise ValueError(f"full suite refuses stale flow directory: {path}")
    return plan


def record_start_failure(
    root: Path, plan: FixtureSession, error: BaseException
) -> None:
    """A serial/open/start failure still leaves evidence that recovery never ran."""
    identity = {"pid": os.getpid(), "pgid": os.getpgid(0)}
    for path, document in (
        (
            root / "recovery" / recovery.PHYSICAL_NAME,
            {
                "schema_version": 1,
                "mode": recovery.MODE,
                "run_id": plan.session_id,
                "status": "blocked",
                "primary_error": str(error),
                "cycles": [],
                "fixture_identity": identity,
            },
        ),
        (
            root / PHYSICAL_NAME,
            {
                "schema_version": 1,
                "mode": MODE,
                "run_id": plan.session_id,
                "status": "failed",
                "recovery_status": "blocked",
                "phase": "fixture-start",
                "primary_error": str(error),
                "fixture_identity": identity,
            },
        ),
    ):
        try:
            _publish_receipt(path, document)
        except (OSError, ValueError, RuntimeError) as publication_error:
            error.add_note(
                f"full suite start failure publication failed: {publication_error}"
            )


class FullSuite:
    """One DirectFixture, lease, BTP dispatcher, handles and live session throughout."""

    def __init__(self, fixture: Any, root: Path, handles: dict[str, int]) -> None:
        self.fixture, self.root, self.handles = fixture, root, dict(handles)
        self.plan = controls.require_multifunction(fixture._fixture_session)
        self.controls = controls.load_suite_controls(root)
        self.control_bytes = {
            root / c.filename: _read_bytes(root / c.filename) for c in self.controls
        }
        path = root / "recovery" / recovery.CONTROL_NAME
        self.control_bytes[path] = _read_bytes(path)
        self.process = (os.getpid(), os.getpgid(0))
        self.deadline = time.monotonic() + WHOLE_SECONDS
        self.lines: list[str] = []
        self.ready_lines: int | None = None
        self.owned_hold: Ticket | None = None
        self.recovery: recovery.RecoveryTen | None = None
        self.native_signal: dict[str, Any] | None = None
        self.report: dict[str, Any] = {
            "schema_version": 1,
            "mode": MODE,
            "run_id": self.plan.session_id,
            "status": "failed",
            "recovery_status": "blocked",
            "fixture_identity": {"pid": self.process[0], "pgid": self.process[1]},
            "handles": self.handles,
            "handoff_path": HANDOFF_NAME,
            "recovery_physical_path": f"recovery/{recovery.PHYSICAL_NAME}",
            "base_physical_paths": [
                f"{c.directory}/physical-evidence.json" for c in self.controls
            ],
            "boundaries": [],
            "primary_error": None,
            "cleanup_errors": [],
        }

    @property
    def url(self) -> str:
        return f"blehub-rn-spike://recover/{self.plan.session_id}/begin"

    def boundary(self, name: str, **fields: Any) -> None:
        self.report["boundaries"].append(
            {
                "boundary": name,
                "monotonic": time.monotonic(),
                **fields,
            }
        )

    def guard(self) -> None:
        self.fixture._transport.require()
        if (self.root / "abort").exists() or self.fixture._stop_requested:
            (self.root / "recovery" / "abort").touch(exist_ok=True)
            raise RuntimeError("full suite aborted by parent")
        if (os.getpid(), os.getpgid(0)) != self.process:
            raise RuntimeError("full suite PID/PGID changed")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("full suite absolute budget exceeded")
        if any(_read_bytes(p) != value for p, value in self.control_bytes.items()):
            raise RuntimeError("full suite immutable controls changed")
        if any(
            (self.root / f"flow-{i}-{mode}" / "abort").exists()
            for i, mode in enumerate(MODES)
        ):
            raise RuntimeError("full suite flow aborted")

    def snapshot(self) -> list[str]:
        self.guard()
        path = self.root / "runtime.log"
        text = read_live_snapshot(path).decode("utf-8") if path.exists() else ""
        if text and not text.endswith("\n"):
            raise RuntimeError("full suite runtime snapshot incomplete")
        lines = text.splitlines()
        if lines[: len(self.lines)] != self.lines or any(
            not line.startswith(PREFIX) for line in lines
        ):
            raise RuntimeError("full suite normalized snapshot rewound/changed")
        self.lines = lines
        markers = [line.removeprefix(PREFIX) for line in lines]
        if any(
            re.search(r"(?:^|:)(?:failed|flow-failed|host-failed):", line)
            for line in markers
        ):
            raise RuntimeError("full suite runtime reported failure")
        return markers

    def base_closed(self, markers: list[str]) -> bool:
        if BASE_PASSED not in markers:
            return False
        starts = [m for m in markers if m.startswith("rf-lab-suite:flow-started:")]
        passes = [m for m in markers if m.startswith("rf-lab-suite:flow-passed:")]
        if (
            starts
            != [
                f"rf-lab-suite:flow-started:index={i}:mode={m}"
                for i, m in enumerate(MODES)
            ]
            or passes
            != [
                f"rf-lab-suite:flow-passed:index={i}:mode={m}"
                for i, m in enumerate(MODES)
            ]
            or markers.count(BASE_PASSED) != 1
        ):
            raise RuntimeError("full suite base flow coverage/terminal drift")
        runtime = [m for m in markers if m.startswith("rf-lab-suite:runtime:")]
        match = re.fullmatch(
            rf"rf-lab-suite:runtime:session={self.plan.session_id}:nonce=([0-9a-z-]+):phase=start",
            runtime[0] if runtime else "",
        )
        if match is None or runtime != [
            match[0],
            match[0].removesuffix("start") + "close",
        ]:
            raise RuntimeError("full suite base runtime did not close same session")
        positions = [markers.index(runtime[0])]
        for start, passed in zip(starts, passes, strict=True):
            positions.extend((markers.index(start), markers.index(passed)))
        positions.extend((markers.index(runtime[1]), markers.index(BASE_PASSED)))
        if positions != sorted(positions):
            raise RuntimeError("full suite base close ordering drift")
        return True

    def verify_physical(self, markers: list[str]) -> None:
        cancellation, control = self.controls
        cancellation_path = (
            self.root / cancellation.directory / "physical-evidence.json"
        )
        watchdog_path = self.root / control.directory / "physical-evidence.json"
        start = markers.index("rf-lab-suite:flow-started:index=8:mode=rf-cancellation")
        end = markers.index("rf-lab-suite:flow-passed:index=8:mode=rf-cancellation")
        scoped = "\n".join(PREFIX + m for m in markers[start:end]) + "\n"
        controls.check_physical_evidence(
            cancellation_path, scoped, self.plan.service_uuid
        )
        start = markers.index("rf-lab-suite:flow-started:index=9:mode=rf-watchdog")
        scoped = "\n".join(PREFIX + m for m in markers[start:]) + "\n"
        watchdog.check_physical_evidence(
            watchdog_path,
            scoped,
            cancellation_path,
            self.plan.service_uuid,
        )
        evidence = controls.read_document(watchdog_path)
        if (
            (evidence["fixture_pid"], evidence["fixture_pgid"]) != self.process
            or evidence["target_handle"] != self.handles["read-write"]
            or any(
                item["handle"] != self.handles[item["role"]]
                for item in evidence["canonical_values"]
            )
        ):
            raise RuntimeError("full suite physical process/handle drift")
        armed = next(b for b in evidence["boundaries"] if b["boundary"] == "armed")
        self.owned_hold = ticket_from_document(
            {k: armed[k] for k in ("wire_hex", "sequence", "handle")}
        )
        self.boundary(
            "base-physical-closed", fault_ticket=ticket_document(self.owned_hold)
        )

    def fresh_idle(self, deadline: float) -> None:
        self.guard()
        watchdog._require_no_peer(self.fixture, deadline)
        if self.owned_hold is None:
            raise RuntimeError("full suite lacks an owned v2 ticket")
        fault = FaultController(self.fixture._session, self.plan.session_id)
        snapshot = fault.status(self.owned_hold)
        self.guard()
        watchdog._require_no_peer(self.fixture, deadline)
        if (
            not cleanup_ready(snapshot)
            or snapshot.winner is not Winner.DISCONNECT
            or snapshot.outcome is not Outcome.DISCONNECTED_UNCOMMITTED
        ):
            raise RuntimeError(
                "full suite last owned fault is not retired/resource-zero/rearmable"
            )
        limits = fault.require_idle()
        self.guard()
        watchdog._require_no_peer(self.fixture, deadline)
        if limits.peer_present or limits.boot_nonce != self.owned_hold.boot_nonce:
            raise RuntimeError("full suite fault boot/peer drift")
        self.boundary(
            "fresh-idle",
            fault_ticket=ticket_document(snapshot.ticket),
            status_wire=encode_status(snapshot).hex(),
            peer_count=0,
        )

    def handoff(self) -> None:
        deadline = time.monotonic() + 10
        self.fresh_idle(deadline)
        self.fixture._gap.stop_advertising()
        self.fresh_idle(deadline)
        restored = []
        for characteristic in self.fixture._profile.characteristics:
            handle = self.handles[characteristic.role]
            self.fresh_idle(deadline)
            self.fixture._gatt.set_value(handle, characteristic.initial_value)
            self.fresh_idle(deadline)
            readback = None
            if characteristic.permissions & 1:
                value = self.fixture._gatt.get_attribute_value(handle)
                self.fresh_idle(deadline)
                if (
                    value.att_response != 0
                    or value.value != characteristic.initial_value
                ):
                    raise RuntimeError("full suite canonical readback mismatch")
                readback = value.value.hex()
            restored.append(
                {
                    "role": characteristic.role,
                    "handle": handle,
                    "value": characteristic.initial_value.hex(),
                    "readback": readback,
                }
            )
        verify_receipt(self.plan, fixture_identity=self.process)
        self.fresh_idle(deadline)
        markers = self.snapshot()
        if any(
            m.startswith(("rf-lab-full:native:signal:", "rf-lab-full:recovery-ready:"))
            for m in markers
        ):
            raise RuntimeError("full suite begin ACK preceded physical authorization")
        self.ready_lines = len(self.lines)
        _publish_receipt(
            self.root / HANDOFF_NAME,
            {
                "schema_version": 1,
                "run_id": self.plan.session_id,
                "status": "ready",
                "fixture_identity": self.report["fixture_identity"],
                "hold_inactive": True,
                "peer_count": 0,
                "advertising_stop_confirmed": True,
                "canonical_values_restored": True,
                "recovery_url": self.url,
                "fault_ticket": ticket_document(self.owned_hold),
                "canonical_values": restored,
                "boundaries": list(self.report["boundaries"]),
            },
        )
        self.boundary("handoff-ready")

    def begin_ack(self, markers: list[str]) -> bool:
        awaiting = f"rf-lab-full:recovery-awaiting:runId={self.plan.session_id}"
        ready = (
            f"rf-lab-full:recovery-ready:runId={self.plan.session_id}:source=linking"
        )
        relevant = [
            (i, m)
            for i, m in enumerate(markers)
            if m.startswith(
                (
                    "rf-lab-full:recovery-awaiting:",
                    "rf-lab-full:native:signal:",
                    "rf-lab-full:recovery-ready:",
                )
            )
        ]
        expected = (awaiting, "rf-lab-full:native:signal:", ready)
        if len(relevant) > 3:
            raise RuntimeError("duplicate full suite begin ACK")
        for step, (index, marker) in enumerate(relevant):
            if step == 1:
                if (
                    self.ready_lines is None
                    or index < self.ready_lines
                    or not marker.startswith(expected[1])
                ):
                    raise RuntimeError("early/foreign full suite native begin signal")
                value = _json(marker.removeprefix(expected[1]).encode())
                bootstrap = recovery.RuntimeFacts(
                    self.plan.session_id, self.plan.service_uuid
                )
                identity = {k: v for k, v in value.items() if k != "url"}
                bootstrap.native("bootstrap", json.dumps(identity))
                if (
                    set(value) != recovery.IDENTITY_FIELDS | {"url"}
                    or value["url"] != self.url
                ):
                    raise RuntimeError("foreign full suite begin URL/identity")
                bootstraps = [
                    m.removeprefix("rf-lab-full:native:bootstrap:")
                    for m in markers
                    if m.startswith("rf-lab-full:native:bootstrap:")
                ]
                if len(bootstraps) > 1:
                    raise RuntimeError("duplicate full suite native bootstrap")
                if bootstraps:
                    native = recovery.RuntimeFacts(
                        self.plan.session_id, self.plan.service_uuid
                    )
                    native.native("bootstrap", bootstraps[0])
                    if any(
                        native.bootstrap[k] != identity[k]
                        for k in recovery.IDENTITY_FIELDS - {"source"}
                    ):
                        raise RuntimeError("full suite native bootstrap identity drift")
                self.native_signal = value
            elif (
                marker != expected[step]
                or step == 2
                and (self.ready_lines is None or index < self.ready_lines)
            ):
                raise RuntimeError("foreign/out-of-order full suite begin ACK")
        return len(relevant) == 3

    def wait(self, predicate: Callable[[list[str]], bool]) -> None:
        deadline = min(self.deadline, time.monotonic() + PHASE_SECONDS)
        while True:
            markers = self.snapshot()
            self.fresh_idle(deadline)
            if predicate(markers):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("full suite closed/begin ACK deadline exceeded")
            time.sleep(0.05)

    def recovery_guard(self) -> None:
        markers = self.snapshot()
        if not self.begin_ack(markers):
            raise RuntimeError("full suite begin authorization disappeared")
        bootstrap = self.recovery.facts.bootstrap if self.recovery else None
        if bootstrap is not None and any(
            bootstrap[k] != self.native_signal[k]
            for k in recovery.IDENTITY_FIELDS - {"source"}
        ):
            raise RuntimeError("full suite/recovery native bootstrap identity drift")

    def base_guard(self) -> None:
        self.snapshot()

    def run(self) -> None:
        try:
            run_continuous(
                self.fixture,
                self.root,
                self.handles,
                parent_guard=self.base_guard,
            )
            # No idle assumption here: first validate the independent physical reports.
            deadline = min(self.deadline, time.monotonic() + PHASE_SECONDS)
            while not self.base_closed(markers := self.snapshot()):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "full suite base closed marker deadline exceeded"
                    )
                time.sleep(0.05)
            self.verify_physical(markers)
            self.handoff()
            self.wait(self.begin_ack)
            self.boundary("begin-authorized", native_signal=self.native_signal)
            self.recovery = recovery.RecoveryTen(
                self.fixture,
                self.root / "recovery",
                self.handles,
                parent_guard=self.recovery_guard,
                retained_ticket=self.owned_hold,
            )
            self.report["recovery_status"] = "running"
            self.recovery.run()
            self.recovery_guard()
            scoped = (
                read_live_snapshot(self.root / "recovery" / "runtime.log")
                .decode("utf-8")
                .splitlines()
            )
            if scoped != [
                line for line in self.lines if line.startswith(recovery.PREFIX)
            ]:
                raise RuntimeError("full suite recovery log projection drift")
            self.report.update(status="passed", recovery_status="passed")
        except BaseException as error:
            self.report["primary_error"] = str(error)
            self.report["recovery_status"] = "failed" if self.recovery else "blocked"
            # No ARM here => no authority to ABORT any hold or adopt an unknown peer.
            try:
                self.fixture._transport.require()
                self.fixture._gap.stop_advertising()
            except (OSError, ValueError, RuntimeError, EOFError) as cleanup_error:
                self.report["cleanup_errors"].append(str(cleanup_error))
                error.add_note(f"full suite cleanup failed: {cleanup_error}")
            if self.recovery is None:
                try:
                    _publish_receipt(
                        self.root / "recovery" / recovery.PHYSICAL_NAME,
                        {
                            "schema_version": 1,
                            "mode": recovery.MODE,
                            "run_id": self.plan.session_id,
                            "status": "blocked",
                            "primary_error": str(error),
                            "cycles": [],
                            "fixture_identity": self.report["fixture_identity"],
                        },
                    )
                except (OSError, ValueError, RuntimeError) as publication_error:
                    error.add_note(
                        f"blocked recovery publication failed: {publication_error}"
                    )
            try:
                _publish_receipt(self.root / PHYSICAL_NAME, self.report)
            except (OSError, ValueError, RuntimeError) as publication_error:
                error.add_note(
                    f"full suite evidence publication failed: {publication_error}"
                )
            raise
        else:
            _publish_receipt(self.root / PHYSICAL_NAME, self.report)


def run(fixture: Any, root: Path, handles: dict[str, int]) -> None:
    FullSuite(fixture, root, handles).run()
