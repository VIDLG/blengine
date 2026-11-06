"""Physical two-engine Android barrier on the provider's sole leased BTP session.

The parent owns normalized, cumulative runtime.log snapshots and all Android
commands. This module neither opens serial/logcat nor validates the full JS ledger.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .btp_commands import GAP
from .cancellation_control import read_document, require_multifunction, write_document
from .fault_control import (
    Decision,
    FaultController,
    Outcome,
    Phase,
    Status,
    Ticket,
    Winner,
    cleanup_ready,
    encode_status,
    read_completed,
    ticket_document,
)
from .fixture_session import (
    FixtureSession,
    _publish_receipt,
    _read_bytes,
    read_live_snapshot,
    verify_receipt,
)
from .gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED
from .gatt_commands import tester_characteristic_native_permissions

MODE = "android-pending-reload"
IOS_MODE = "ios-pending-reload"


def route_offsets(platform: str) -> tuple[int, int, int]:
    """Return discovery/read/session offsets without changing Android's old ABI."""
    if platform not in ("android", "ios"):
        raise ValueError("unsupported pending reload platform")
    return (7, 8, 6) if platform == "android" else (9, 11, 8)


CONTROL_NAME = "pending-reload-control.json"
PROGRESS_NAME = "pending-reload-progress.json"
PHYSICAL_NAME = "pending-reload-physical.json"
POLL_SECONDS = 0.05  # Do not bypass the firmware's 40 ms status/logging cadence.
PHASE_SECONDS = 30.0
WHOLE_SECONDS = 180.0
ARM_SECONDS = (
    8.0  # Pre-read ARM acknowledgement is bounded below the 10 s firmware arm limit.
)
RELEASE_SECONDS = 3.0
NATIVE_CLOSE_SECONDS = 9.0  # Leave margin before the 10 s native read watchdog.
PREFIX = "BLEHUB_NITRO_RUNTIME rf-pending-read-reload:"
NATIVE_EVENTS = (
    "read-accepted",
    "engine-invalidation-close-started",
    "dispose-returned",
    "engine-close-complete",
    "late-submit-rejected",
)


def validate_control(
    root: Path, plan: FixtureSession | None, *, platform: str = "android"
) -> FixtureSession:
    """Preflight immutable parent control before opening or mutating the fixture."""
    route_offsets(platform)
    plan = require_multifunction(plan)
    uuid_value = uuid.UUID(plan.session_id)
    if str(uuid_value) != plan.session_id:
        raise ValueError("pending reload requires a canonical run UUID")
    value = read_document(root / CONTROL_NAME)
    if (
        set(value)
        != (
            {"schema_version", "run_id"}
            | ({"platform"} if platform == "ios" else set())
        )
        or (platform == "ios" and value.get("platform") != "ios")
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["run_id"] != plan.session_id
    ):
        raise ValueError("pending reload control does not match fixture session")
    for name in (PHYSICAL_NAME, PROGRESS_NAME):
        path = root / name
        if path.exists() or path.is_symlink():
            raise ValueError(f"pending reload refuses stale output: {path}")
    return plan


class RuntimeFacts:
    """Only the routing facts necessary to synchronize physical barriers.

    Input is the parent's same-PID normalized snapshot, never raw logcat. Native
    completion and synthetic handoff rejection remain facts, not late RF proof.
    """

    def __init__(self, run_id: str, *, platform: str = "android") -> None:
        _, self.read_offset, self.gatt_offset = route_offsets(platform)
        self.run_id = run_id
        self.lines: list[str] = []
        self.seen: dict[str, str] = {}
        self.routes: dict[int, str] = {}
        self.nonces: dict[int, str] = {}
        self.probe_id: str | None = None

    def refresh(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        lines = read_live_snapshot(path).decode("utf-8").splitlines()
        if lines[: len(self.lines)] != self.lines:
            raise RuntimeError(
                "runtime.log rewound or changed previously observed lines"
            )
        for line in lines[len(self.lines) :]:
            self.observe(line)
        self.lines = lines

    def save(self, key: str, line: str) -> None:
        if key in self.seen:
            raise RuntimeError(f"duplicate pending reload marker: {key}")
        self.seen[key] = line

    def observe(self, line: str) -> None:
        if not line.startswith(PREFIX):
            raise RuntimeError("runtime.log contains a non-normalized pending marker")
        marker = line[len(PREFIX) :]
        if ":failed:" in marker or "native-boundary-failed" in marker:
            raise RuntimeError(f"runtime failure: {marker}")
        if marker.startswith("native:"):
            match = re.fullmatch(
                r"native:([^:]+):runId=([^:]+):probeId=([^:]+):"
                r"commandId=(\d+):connectionId=(\d+):gattSessionId=(\d+):"
                r"characteristicId=([1-9][0-9]*)(.*)",
                marker,
            )
            if match is None:
                raise RuntimeError("malformed native pending reload marker")
            event, run, probe, command, connection, gatt, characteristic, suffix = (
                match.groups()
            )
            route = f"commandId={command}:connectionId={connection}:gattSessionId={gatt}:characteristicId={characteristic}"
            if (
                event not in NATIVE_EVENTS
                or run != self.run_id
                or str(uuid.UUID(probe)) != probe
                or route != self.routes.get(1)
                or (self.probe_id is not None and self.probe_id != probe)
                or suffix
                != (
                    ":source=synthetic:result=closed"
                    if event == "late-submit-rejected"
                    else ""
                )
            ):
                raise RuntimeError("foreign native probe/run/read route or suffix")
            self.probe_id = probe
            self.save(f"native:{event}", line)
            return
        if marker == f"passed:runId={self.run_id}":
            self.save("passed", line)
            return
        match = re.fullmatch(r"generation=([12]):(.+)", marker)
        if match is None:
            raise RuntimeError("foreign run or malformed pending reload marker")
        generation = int(match[1])
        body = match[2]
        key = f"{generation}:"
        if body.startswith("runtime-start:"):
            boot = re.fullmatch(r"runtime-start:runId=([^:]+):nonce=([^:\s]+)", body)
            if (
                boot is None
                or boot[1] != self.run_id
                or boot[2] in self.nonces.values()
            ):
                raise RuntimeError("foreign run or reused runtime nonce")
            self.nonces[generation] = boot[2]
            self.save(key + "runtime-start", line)
        elif body == "native-ready:passed":
            if generation not in self.nonces:
                raise RuntimeError("native ready before runtime start")
            self.save(key + "native-ready", line)
        elif body.startswith("awaiting-arm:"):
            base = 12000 if generation == 1 else 13000
            route = body.removeprefix("awaiting-arm:")
            if (
                key + "native-ready" not in self.seen
                or re.fullmatch(
                    rf"commandId={base + self.read_offset}:connectionId={base + 4}:gattSessionId={base + self.gatt_offset}:characteristicId=[1-9][0-9]{{0,19}}",
                    route,
                )
                is None
                or int(route.rsplit("=", 1)[-1]) > (1 << 64) - 1
            ):
                raise RuntimeError("invalid awaiting-arm native route/readiness")
            self.routes[generation] = route
            self.save(key + "awaiting-arm", line)
        elif body.startswith("arm-js-observed:"):
            if (
                body != "arm-js-observed:source=linking:url=matched"
                or generation not in self.routes
            ):
                raise RuntimeError("invalid JS arm observation")
            self.save(key + "arm-js-observed", line)
        elif body.startswith("read-dispatched:"):
            route = self.routes.get(generation)
            expected = f"read-dispatched:{route}"
            valid = (
                re.fullmatch(
                    re.escape(expected) + r":peripheralId=(?:0|[1-9][0-9]{0,19})", body
                )
                if generation == 1
                else body == expected
            )
            if (
                not valid
                or key + "arm-js-observed" not in self.seen
                or (generation == 1 and int(body.rsplit("=", 1)[-1]) > (1 << 64) - 1)
            ):
                raise RuntimeError("read dispatch does not match armed native route")
            self.save(key + "read-dispatched", line)
        elif body == "close:passed":
            self.save(key + "close", line)
        # Scan/GATT/read terminal semantics and complete event ordering belong to
        # the host checker. They must not be promoted into physical evidence here.


class PendingReload:
    """Bounded physical coordinator; fixture supplies all existing native owners."""

    def __init__(
        self,
        fixture: Any,
        root: Path,
        handles: dict[str, int],
        *,
        platform: str = "android",
    ) -> None:
        self.fixture = fixture
        self.root = root
        self.plan = validate_control(root, fixture._fixture_session, platform=platform)
        self.control_bytes = _read_bytes(root / CONTROL_NAME)
        self.process = (os.getpid(), os.getpgid(0))
        self.fault = FaultController(fixture._session, self.plan.session_id)
        self.facts = RuntimeFacts(self.plan.session_id, platform=platform)
        self.target = handles["read-write"]
        self.rw = next(
            c for c in fixture._profile.characteristics if c.role == "read-write"
        )
        self.identity: Ticket | None = None
        self.peer: tuple[str, int] | None = None
        self.generation = 0
        self.closing = False
        self.disconnected = False
        self.armed_at = 0.0
        self.whole_deadline = time.monotonic() + WHOLE_SECONDS
        self.boundaries: list[dict[str, Any]] = []
        self.evidence: dict[str, Any] = {
            "schema_version": 2,
            "status": "failed",
            "run_id": self.plan.session_id,
            "session_id": self.plan.session_id,
            "fixture_pid": self.process[0],
            "fixture_pgid": self.process[1],
            "profile_id": fixture._profile.profile_id,
            "contract_sha256": self.plan.contract_sha256,
            "profile_sha256": self.plan.profile_sha256,
            "resource_identity": self.plan.resource_identity,
            "live_receipt_path": str(self.plan.receipt_path),
            "port": self.plan.port,
            "service_uuid": self.plan.service_uuid,
            "target_uuid": self.rw.uuid,
            "target_handle": self.target,
            "target_permissions": {
                "profile": self.rw.permissions,
                "expected_native": 0x43,
                "native": None,
            },
            "budgets_seconds": {
                "whole": WHOLE_SECONDS,
                "phase": PHASE_SECONDS,
                "arm_to_held": ARM_SECONDS,
                "resolve_to_retired": RELEASE_SECONDS,
                "native_close": NATIVE_CLOSE_SECONDS,
            },
            "boundaries": self.boundaries,
            "cleanup_errors": [],
            "native_close_facts": {},
            "physical_cleanup": {},
            **({"platform": "ios"} if platform == "ios" else {}),
        }

    def record(self, boundary: str, **fields: Any) -> None:
        self.boundaries.append(
            {
                "boundary": boundary,
                "generation": self.generation,
                "monotonic": time.monotonic(),
                **(ticket_document(self.identity) if self.identity else {}),
                "peer": list(self.peer) if self.peer else None,
                **fields,
            }
        )

    def progress(self, phase: str) -> None:
        write_document(
            self.root / PROGRESS_NAME,
            {
                "schema_version": 1,
                "run_id": self.plan.session_id,
                "phase": phase,
                "status": self.evidence["status"] if phase == "closed" else "running",
                "generation": self.generation,
                "monotonic": time.monotonic(),
                "sequence": self.identity.sequence if self.identity else None,
                "handle": self.target,
                "peer": list(self.peer) if self.peer else None,
                "peers": [list(p) for p in sorted(self.fixture._connected_peers)],
                "route": self.facts.routes.get(self.generation),
                "probe_id": self.facts.probe_id,
                "arm_url": f"blehub-rn-spike://arm/{self.plan.session_id}",
            },
        )

    def guard(self, deadline: float) -> None:
        self.fixture._transport.require()
        if self.fixture._stop_requested or (self.root / "abort").exists():
            raise RuntimeError("pending reload aborted by parent")
        if _read_bytes(self.root / CONTROL_NAME) != self.control_bytes:
            raise RuntimeError("pending reload immutable control changed")
        if time.monotonic() >= min(deadline, self.whole_deadline):
            raise TimeoutError("pending reload physical phase deadline exceeded")

    def drain(self, first: Any = None) -> None:
        event = first or self.fixture._session.pop_event()
        while event is not None:
            self.observe_gap(event)
            event = self.fixture._session.pop_event()
        if self.peer is not None and self.fixture._connected_peers != (
            set() if self.disconnected else {self.peer}
        ):
            raise RuntimeError("pending reload exact GAP peer drift")

    def observe_gap(self, event: Any) -> None:
        if event.service != GAP or event.opcode not in (
            GAP_DEVICE_CONNECTED,
            GAP_DEVICE_DISCONNECTED,
        ):
            return
        if (
            type(event.service) is not int
            or type(event.opcode) is not int
            or type(event.index) is not int
            or event.index != 0
            or type(event.payload) is not bytes
            or len(event.payload) != (13 if event.opcode == GAP_DEVICE_CONNECTED else 7)
            or event.payload[0] not in (0, 1, 2, 3)
        ):
            raise RuntimeError("malformed pending reload GAP event")
        peer = (event.payload[1:7][::-1].hex(), event.payload[0])
        peers = self.fixture._connected_peers
        if event.opcode == GAP_DEVICE_CONNECTED:
            if peers or (self.peer is not None and not self.disconnected):
                raise RuntimeError("foreign or duplicate pending reload GAP connection")
            if self.disconnected:
                raise RuntimeError("new peer before advertising recovery boundary")
            peers.add(peer)
            self.record("gap-connected", observed_peer=list(peer))
        else:
            if peer not in peers or (self.peer is not None and peer != self.peer):
                raise RuntimeError("foreign or duplicate pending reload GAP disconnect")
            if not self.closing:
                raise RuntimeError(
                    "pending reload peer disconnected before release/close"
                )
            peers.remove(peer)
            self.disconnected = True
            self.record("gap-disconnected", observed_peer=list(peer))

    def sample(self, deadline: float, *, logs: bool = True) -> Status | None:
        self.guard(deadline)
        if self.identity is None:
            self.fault.require_idle()
            snapshot = None
        else:
            snapshot = self.fault.status(self.identity)
        self.guard(deadline)
        self.drain()
        if logs:
            self.facts.refresh(self.root / "runtime.log")
        self.guard(deadline)
        return snapshot

    def wait(self, deadline: float, predicate: Any) -> Status | None:
        while True:
            snapshot = self.sample(deadline)
            if predicate(snapshot):
                return snapshot
            time.sleep(POLL_SECONDS)

    def receipt(self, boundary: str, snapshot: Status, **fields: Any) -> None:
        if snapshot.ticket != self.identity:
            raise RuntimeError("pending reload STATUS ticket drift")
        self.record(boundary, status_wire=encode_status(snapshot).hex(), **fields)

    def verify(self) -> None:
        if _read_bytes(self.root / CONTROL_NAME) != self.control_bytes:
            raise RuntimeError("pending reload immutable control changed")
        verify_receipt(self.plan, fixture_identity=self.process)

    def cycle(self, generation: int) -> None:
        self.generation = generation
        self.peer = None
        self.disconnected = self.closing = False
        key = f"{generation}:"
        phase_deadline = min(self.whole_deadline, time.monotonic() + PHASE_SECONDS)

        def ready(snapshot: Status | None) -> bool:
            if snapshot is not None and not cleanup_ready(snapshot):
                raise RuntimeError(
                    "previous fault receipt is not resource-zero/rearmable"
                )
            return (
                key + "awaiting-arm" in self.facts.seen
                and len(self.fixture._connected_peers) == 1
            )

        self.wait(phase_deadline, ready)
        self.peer = next(iter(self.fixture._connected_peers))
        attributes = self.fixture._gatt.get_attributes(attribute_uuid=self.rw.uuid)
        self.guard(phase_deadline)
        self.drain()
        if (
            self.rw.permissions != 0x03
            or self.rw.initial_value != b"\x00"
            or len(attributes) != 1
            or attributes[0].handle != self.target
            or attributes[0].uuid != self.rw.uuid
            or attributes[0].permissions
            != tester_characteristic_native_permissions(0x03)
        ):
            raise RuntimeError(
                "pending reload requires exact ordinary RW/native 0x43 target"
            )
        before = self.fixture._gatt.get_attribute_value(self.target)
        self.guard(phase_deadline)
        self.drain()
        if before.att_response != 0 or before.value != self.rw.initial_value:
            raise RuntimeError(
                "pending reload before value does not match the exact target"
            )
        self.evidence["target_permissions"]["native"] = attributes[0].permissions
        self.verify()
        previous = self.identity
        self.armed_at = time.monotonic()
        peer_wire = bytes([self.peer[1]]) + bytes.fromhex(self.peer[0])[::-1]
        self.identity = self.fault.arm_read(self.target, before.value, peer_wire).ticket
        if previous is not None and (
            self.identity.boot_nonce != previous.boot_nonce
            or self.identity.sequence <= previous.sequence
        ):
            raise RuntimeError(
                "pending reload boot drift or sequence did not strictly increase"
            )
        arm_deadline = min(self.whole_deadline, self.armed_at + ARM_SECONDS)
        snapshot = self.sample(arm_deadline)
        if snapshot is None or snapshot.phase is not Phase.ARMED:
            raise RuntimeError("pending reload ARM did not reach exact ARMED")
        self.receipt(
            "armed",
            snapshot,
            armed_at=self.armed_at,
            route=self.facts.routes[generation],
        )
        self.progress(f"armed-{generation}")
        waiting_at: float | None = None

        def held(snapshot: Status | None) -> bool:
            nonlocal waiting_at
            if snapshot is None or snapshot.phase not in (Phase.ARMED, Phase.HELD):
                raise RuntimeError("pending read left ARMED/HELD before capture")
            if snapshot.phase is Phase.HELD and waiting_at is None:
                waiting_at = time.monotonic()
            return (
                snapshot.phase is Phase.HELD
                and key + "read-dispatched" in self.facts.seen
                and (generation == 2 or "native:read-accepted" in self.facts.seen)
            )

        snapshot = self.wait(arm_deadline, held)
        assert snapshot is not None and waiting_at is not None
        self.receipt(
            "waiting",
            snapshot,
            first_waiting_at=waiting_at,
            read_dispatched=self.facts.seen[key + "read-dispatched"],
        )
        close_deadline = min(self.whole_deadline, waiting_at + NATIVE_CLOSE_SECONDS)
        # Invalidation may disconnect as soon as native close starts. RX is not
        # blocked in v2, so never require a live HELD ticket after disposal.
        self.closing = generation == 1
        self.progress(f"waiting-{generation}")
        if generation == 1:
            self.wait(
                close_deadline,
                lambda _: all(
                    "native:" + event in self.facts.seen for event in NATIVE_EVENTS[1:3]
                ),
            )
            self.record("native-dispose-observed", probe_id=self.facts.probe_id)
            snapshot = self.wait(
                close_deadline,
                lambda value: (
                    value is not None
                    and self.disconnected
                    and cleanup_ready(value)
                    and all(
                        "native:" + event in self.facts.seen for event in NATIVE_EVENTS
                    )
                ),
            )
            assert snapshot is not None
            if (
                snapshot.winner is not Winner.DISCONNECT
                or snapshot.outcome is not Outcome.DISCONNECTED_UNCOMMITTED
            ):
                raise RuntimeError(
                    "invalidation requires the disconnected uncommitted retained receipt"
                )
        else:
            snapshot = self.sample(close_deadline)
            if (
                snapshot is None
                or snapshot.phase is not Phase.HELD
                or self.fixture._connected_peers != {self.peer}
            ):
                raise RuntimeError("pending reload lost HELD/live peer before RESOLVE")
            self.receipt("before-resolve", snapshot)
            resolve_at = time.monotonic()
            self.fault.resolve(self.identity, Decision.COMMIT)
            self.guard(close_deadline)
            self.record("resolve-requested", resolve_at=resolve_at)
            self.closing = True
            snapshot = self.wait(
                min(close_deadline, resolve_at + RELEASE_SECONDS),
                lambda value: value is not None and cleanup_ready(value),
            )
            assert snapshot is not None
            if not read_completed(snapshot):
                raise RuntimeError(
                    "normal pending read lacks NORMAL_NO_COMMIT/ordered response"
                )
        self.receipt("retired", snapshot)
        # The existing host progress phase denotes settled ownership, not a
        # firmware RELEASED state or a claim that an abandoned read was delivered.
        self.progress(f"released-{generation}")
        phase_deadline = min(self.whole_deadline, time.monotonic() + PHASE_SECONDS)
        if generation == 2:
            self.wait(
                phase_deadline,
                lambda _: "2:close" in self.facts.seen and self.disconnected,
            )
        if generation == 1:
            self.peer = None
            self.disconnected = self.closing = False
            self.fixture._start_advertising()
            self.guard(phase_deadline)
            self.record("advertising-recovered")

    def cleanup(self, *, failed: bool) -> None:
        errors = self.evidence["cleanup_errors"]
        cleanup: dict[str, Any] = {
            "hold_inactive": False,
            "hold_state": "RETIRED",
            "no_known_peers": False,
            "advertising_stopped": False,
        }
        self.evidence["physical_cleanup"] = cleanup
        self.closing = True
        deadline = time.monotonic() + PHASE_SECONDS
        try:
            self.fixture._transport.require()
            if self.identity is not None:
                snapshot = self.fault.status(self.identity)
                self.drain()
                if (
                    failed
                    and not cleanup_ready(snapshot)
                    and snapshot.phase in (Phase.ARMED, Phase.HELD)
                    and not self.disconnected
                    and self.fixture._connected_peers
                ):
                    self.fault.abort(self.identity)
        except (OSError, ValueError, RuntimeError, EOFError) as error:
            errors.append(f"fault-abort: {error}")
        try:
            if failed:
                for peer in sorted(self.fixture._connected_peers):
                    self.fixture._gap.disconnect(*peer)
            while self.fixture._connected_peers:
                self.fixture._transport.require()
                if time.monotonic() >= deadline:
                    raise TimeoutError("pending reload cleanup peer deadline")
                event = self.fixture._session.poll_event(POLL_SECONDS)
                if event is not None:
                    self.drain(event)
            cleanup["no_known_peers"] = True
        except (OSError, ValueError, RuntimeError, EOFError) as error:
            errors.append(f"peers: {error}")
        try:
            self.fixture._gap.stop_advertising()
            self.drain()
            if self.fixture._connected_peers:
                cleanup["no_known_peers"] = False
                raise RuntimeError("pending reload peer raced with advertising stop")
            cleanup["advertising_stopped"] = True
        except (OSError, ValueError, RuntimeError, EOFError) as error:
            errors.append(f"advertising: {error}")
        try:
            if self.identity is None:
                self.fault.require_idle()
                cleanup["hold_state"] = "NO_OWNED_TICKET"
            else:
                while True:
                    snapshot = self.fault.status(self.identity)
                    self.drain()
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "pending reload resource-zero retirement deadline"
                        )
                    if cleanup_ready(snapshot):
                        break
                    time.sleep(POLL_SECONDS)
                cleanup["status_wire"] = encode_status(snapshot).hex()
            cleanup["hold_inactive"] = True
        except (OSError, ValueError, RuntimeError, EOFError) as error:
            errors.append(f"fault-retirement: {error}")
        self.record("physical-cleanup", **cleanup)
        if errors and not failed:
            raise RuntimeError(
                "pending reload physical cleanup failed: " + "; ".join(errors)
            )

    def run(self) -> None:
        primary: BaseException | None = None
        try:
            self.verify()
            self.cycle(1)
            self.cycle(2)
            self.verify()
            self.cleanup(failed=False)
            self.verify()
            self.guard(self.whole_deadline)
            self.evidence["status"] = "passed"
            self.record("closed", hold_inactive=True, peer_count=0)
            self.progress("closed")
        except BaseException as error:  # noqa: BLE001 - publish cleanup before re-raising even owner cancellation.
            primary = error
            self.evidence["status"] = "failed"
            self.evidence["primary_error"] = str(error)
            try:
                self.cleanup(failed=True)
            except BaseException as cleanup_error:  # noqa: BLE001 - never replace the primary failure.
                self.evidence["cleanup_errors"].append(
                    f"unexpected-cleanup: {cleanup_error}"
                )
            try:
                self.progress("closed")
            except (OSError, ValueError, RuntimeError) as publication_error:
                self.evidence["cleanup_errors"].append(
                    f"progress-publication: {publication_error}"
                )
        finally:
            self.evidence["native_close_facts"] = {
                k: v for k, v in self.facts.seen.items() if k.startswith("native:")
            }
            self.evidence["runtime_nonces"] = self.facts.nonces
            try:
                _publish_receipt(self.root / PHYSICAL_NAME, self.evidence)
            except (OSError, ValueError, RuntimeError) as error:
                if primary is None:
                    raise
                primary.add_note(f"physical evidence publication failed: {error}")
        if primary is not None:
            for error in self.evidence["cleanup_errors"]:
                primary.add_note(f"physical cleanup failed: {error}")
            raise primary


def run(
    fixture: Any, root: Path, handles: dict[str, int], *, platform: str = "android"
) -> None:
    """Entry used after DirectFixture.start() has published the fresh live receipt."""
    PendingReload(fixture, root, handles, platform=platform).run()
