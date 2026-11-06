"""Attested ten-cycle physical barriers on one leased BTP session.

The parent supplies complete, cumulative, same-PID normalized runtime.log snapshots.
This is cooperative evidence, not a security attestation or a replacement for the
host's full command/event ledger. No serial, App, OS control or firmware owner is
created here. Resource counts are consumed from native-backed JS snapshots only.

Native wire API (each JSON object follows the normalized PREFIX):
  native:bootstrap:{run_id,pid,boot_id,bundle_id,executable,source}
  native:signal:{same identity fields,cycle,url}
`source` is a nonempty native callback provenance string, retained as evidence.
The signal must match the bootstrap identity and the physically authorized URL.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .btp_commands import GAP, INDEX_NONE, VENDOR, require_success
from .cancellation_control import read_document, require_multifunction, write_document
from .fault_control import (
    EngineFlags,
    FaultController,
    InfrastructureError,
    Opcode,
    Operation,
    Outcome,
    Phase,
    RefCounters,
    StatusFlags,
    Ticket,
    Winner,
    cleanup_ready,
    decode_status,
    encode_ticket_command,
    owner_nonce,
    status_document,
    ticket_document,
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

MODE = "recovery-ten-attested"
CONTROL_NAME = "recovery-ten-control.json"
PROGRESS_NAME = "recovery-ten-progress.json"
PHYSICAL_NAME = "recovery-ten-physical.json"
PREFIX = "BLEHUB_NITRO_RUNTIME rf-recovery-ten:"
POLL_SECONDS = 0.05
PHASE_SECONDS = 90.0
WHOLE_SECONDS = 900.0
ACK_SECONDS = 2.0
CLEANUP_SECONDS = 5.0
FAULT_SAMPLES_MAX = 128  # Bounded diagnostics, not a second progress/event ledger.

RESOURCE_FIELDS = frozenset(
    (
        "activeScans",
        "connections",
        "gattAttributes",
        "subscriptions",
        "pendingIo",
        "watchdogs",
        "callbackRoutes",
        "ingressRoots",
        "adapterDelegates",
        "peerCacheEntries",
        "backendLeases",
        "hubRoots",
        "handoffRoots",
        "handoffPending",
        "listeners",
        "workerThreads",
        "lifecycleOwners",
    )
)
RESOURCE_PHASES = ("baseline", "active", "passive", "recovery-disconnected", "closed")
IDENTITY_FIELDS = {"run_id", "pid", "boot_id", "bundle_id", "executable", "source"}
PROGRESS_PHASES = frozenset(
    (
        "preparing",
        "subscription-enabled",
        "notification-observed",
        "passive-observed",
        "recovery-ready",
        "recovery-read",
        "cycle-closed",
        "runtime-closed",
        "closed",
    )
)
STATUSES = frozenset(("running", "passed", "failed"))
BUNDLES = {
    "org.reactjs.native.example.BleHubNitroHost": "ios",
    "com.blehubnitrohost": "android",
}


def validate_control(root: Path, plan: FixtureSession | None) -> FixtureSession:
    """Validate the only immutable control document before opening hardware."""
    plan = require_multifunction(plan)
    if str(uuid.UUID(plan.session_id)) != plan.session_id:
        raise ValueError("recovery ten requires a canonical run UUID")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("recovery ten requires a private control directory")
    if {p.name for p in root.glob("*-control.json")} != {CONTROL_NAME}:
        raise ValueError("recovery ten requires exactly one control file")
    value = read_document(root / CONTROL_NAME)
    if (
        set(value) != {"schema_version", "run_id", "cycles"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["cycles"]) is not int
        or value["cycles"] != 10
        or value["run_id"] != plan.session_id
    ):
        raise ValueError("invalid recovery ten control/session/cycles")
    for name in (PROGRESS_NAME, PHYSICAL_NAME, "abort"):
        path = root / name
        if path.exists() or path.is_symlink():
            raise ValueError(f"recovery ten refuses stale output/cancellation: {path}")
    return plan


def validate_resources(value: dict[str, Any], phase: str, android: bool) -> None:
    """Mirror the actual TS 17-field lifecycle invariant; barriers may stay warm."""
    if (
        phase not in RESOURCE_PHASES
        or set(value) != RESOURCE_FIELDS
        or any(
            type(count) is not int or not 0 <= count <= 9007199254740991
            for count in value.values()
        )
    ):
        raise RuntimeError("invalid 17-field resource snapshot")
    expected = (
        {key: 0 for key in RESOURCE_FIELDS}
        if phase == "closed"
        else {
            **{
                key: 1
                for key in (
                    "hubRoots",
                    "handoffRoots",
                    "ingressRoots",
                    "backendLeases",
                    "adapterDelegates",
                    "listeners",
                )
            },
            **{
                key: 0
                for key in ("activeScans", "pendingIo", "watchdogs", "handoffPending")
            },
            "workerThreads": 2 if android else 0,
            "lifecycleOwners": 1 if android else 0,
            "connections": 1 if phase == "active" else 0,
            "subscriptions": 1 if phase == "active" else 0,
        }
    )
    if phase != "closed":
        if value["peerCacheEntries"] > 1:
            raise RuntimeError("resource peer cache exceeds one fixture")
        if phase == "active" and value["gattAttributes"] == 0:
            raise RuntimeError("missing active resource positive control")
        if android and phase != "active":
            expected.update(gattAttributes=0, callbackRoutes=0)
    if any(value[key] != count for key, count in expected.items()):
        raise RuntimeError(
            f"{phase} resource counts violate native lifecycle invariant"
        )


# Conservative test policy, not an Android cross-vendor scanning guarantee.
SCAN_BUDGET_POLICY = (
    "scan-budget-policy:version=1:platform=android:scope=conservative-test-only:"
    "quietMs=30000:minStartIntervalMs=7000:retries=0"
)


def passive_disconnect_error(event: dict, connection: str, platform: str) -> str:
    """Validate the exclusive native Result branches on the exact passive route."""
    allowed = {
        "kind",
        "commandId",
        "connectionId",
        "connectionState",
        "error",
        "cycle",
        "runId",
        "nonce",
    }
    if (
        event.get("kind") != "connectionStateChanged"
        or event.get("commandId") is not None
        or event.get("connectionId") != connection
        or any(value is not None and key not in allowed for key, value in event.items())
    ):
        raise RuntimeError("invalid passive disconnect route")
    error = event.get("error")
    if error is None:
        if event.get("connectionState") != "Disconnected":
            raise RuntimeError("invalid passive disconnect success")
        return "none"
    if (
        event.get("connectionState") is not None
        or type(error) is not dict
        or not {"category", "message"}
        <= error.keys()
        <= {"category", "message", "platformDetails"}
        or error["category"] != "connection"
        or type(error["message"]) is not str
        or len(error["message"]) > 512
    ):
        raise RuntimeError("invalid passive disconnect error result")
    details = error.get("platformDetails")
    if details is not None and (
        type(details) is not dict
        or not {"platform"} <= details.keys() <= {"platform", "code", "name", "message"}
        or details["platform"]
        != ("platform_android" if platform == "android" else "platform_apple")
        or any(
            value is not None and (type(value) is not str or len(value) > 512)
            for value in details.values()
        )
    ):
        raise RuntimeError("invalid passive disconnect platform details")
    return "connection"


class RuntimeFacts:
    """Incremental marker facts; JS cycle success alone never authorizes hardware."""

    def __init__(self, run_id: str, service_uuid: str) -> None:
        self.run_id = run_id
        self.service_uuid = service_uuid
        self.lines: list[str] = []
        self.seen: dict[str, str] = {}
        self.resources: dict[int, dict[str, dict[str, int]]] = {}
        self.bootstrap: dict[str, Any] | None = None
        self.signals: dict[int, dict[str, Any]] = {}
        self.authorized: int | None = None
        self.nonce: str | None = None
        self.cycle = -1
        self.stage = 0
        self.event_count = 0
        self.passive_errors: dict[int, str] = {}
        self.scan_budget_times: dict[str, int] = {}
        self.scan_wait_ms = 0
        self.scan_budget_clock = 0

    def refresh(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        content = read_live_snapshot(path).decode("utf-8")
        if content and not content.endswith("\n"):
            raise RuntimeError("runtime.log is not a complete normalized snapshot")
        lines = content.splitlines()
        if lines[: len(self.lines)] != self.lines:
            raise RuntimeError("runtime.log rewound or changed observed lines")
        for line in lines[len(self.lines) :]:
            self.observe(line)
        self.lines = lines

    def save(self, key: str, marker: str) -> None:
        if key in self.seen:
            raise RuntimeError(f"duplicate recovery ten marker: {key}")
        self.seen[key] = marker

    def native(self, kind: str, body: str) -> None:
        record = _json(body.encode("utf-8"))
        keys = IDENTITY_FIELDS | ({"cycle", "url"} if kind == "signal" else set())
        if (
            set(record) != keys
            or record["run_id"] != self.run_id
            or type(record["pid"]) is not int
            or record["pid"] <= 0
            or not isinstance(record["bundle_id"], str)
            or record["bundle_id"] not in BUNDLES
            or any(
                not isinstance(record[k], str) or not record[k] or len(record[k]) > 2048
                for k in ("boot_id", "executable", "source")
            )
        ):
            raise RuntimeError("malformed/foreign native recovery identity")
        if str(uuid.UUID(record["boot_id"])) != record["boot_id"]:
            raise RuntimeError("invalid native boot UUID")
        if kind == "bootstrap":
            if self.bootstrap is not None or self.nonce is not None:
                raise RuntimeError("duplicate/late native bootstrap")
            self.bootstrap = record
        else:
            cycle = record["cycle"]
            if (
                self.bootstrap is None
                or type(cycle) is not int
                or cycle != self.authorized
                or cycle != self.cycle
                or record["url"] != self.url(cycle)
                or f"{cycle}:recovery-awaiting" not in self.seen
                or any(
                    record[k] != self.bootstrap[k] for k in IDENTITY_FIELDS - {"source"}
                )
            ):
                raise RuntimeError("foreign/early native recovery signal")
            self.save(f"{cycle}:native-signal", body)
            self.signals[cycle] = record

    def url(self, cycle: int) -> str:
        return f"blehub-rn-spike://recover/{self.run_id}/{cycle}"

    def scan_budget(self, marker: str) -> bool:
        """Require exact Android-only policy, timing schema and per-cycle order.

        JS monotonic markers attest test scheduling, not independent HCI timestamps.
        The full ledger also checks native scan events inside the dispatch window.
        """
        if marker == SCAN_BUDGET_POLICY:
            if (
                self.bootstrap is None
                or BUNDLES[self.bootstrap["bundle_id"]] != "android"
                or self.nonce is None
                or self.cycle != -1
            ):
                raise RuntimeError("scan budget policy outside Android runtime start")
            self.save("scan-budget-policy", marker)
            return True
        match = re.fullmatch(
            r"cycle:([0-9]):scan-budget-(wait|dispatch|started):"
            r"(?:waitMs=(0|[1-9][0-9]*):)?monotonicMs=(0|[1-9][0-9]*)"
            r"(:policy=android-conservative-test-only-v1:quietMs=30000:"
            r"minStartIntervalMs=7000)?",
            marker,
        )
        if match is None:
            return False
        cycle, phase, wait, timestamp, policy = match.groups()
        now = int(timestamp)
        cycle = int(cycle)
        key = f"{cycle}:scan-budget-{phase}"
        if (
            "scan-budget-policy" not in self.seen
            or cycle != self.cycle
            or self.stage != 2
            or (phase == "wait") != (wait is not None)
            or (phase == "wait") != (policy is not None)
            or now > 9007199254740991
            or now < self.scan_budget_clock
        ):
            raise RuntimeError("foreign/out-of-order scan budget marker")
        if phase == "wait":
            previous = self.scan_budget_times.get(f"{cycle - 1}:scan-budget-started")
            if cycle > 0 and previous is None:
                raise RuntimeError("scan budget missing previous native start")
            expected = 30000 if cycle == 0 else max(0, previous + 7000 - now)
            if int(wait) != expected:
                raise RuntimeError(
                    "scan budget wait does not match conservative policy"
                )
            self.scan_wait_ms = expected
        elif phase == "dispatch":
            start = self.scan_budget_times.get(f"{cycle}:scan-budget-wait")
            if start is None or now < start + self.scan_wait_ms:
                raise RuntimeError("scan dispatch before quiet/interval budget")
        elif f"{cycle}:scan-budget-dispatch" not in self.seen:
            raise RuntimeError("native scan start before budget dispatch")
        self.save(key, marker)
        self.scan_budget_times[key] = now
        self.scan_budget_clock = now
        return True

    def observe(self, line: str) -> None:
        if not line.startswith(PREFIX):
            raise RuntimeError("non-normalized recovery ten runtime line")
        marker = line[len(PREFIX) :]
        if "failed:" in marker or ":failed" in marker:
            raise RuntimeError(f"JS/native recovery failure: {marker}")
        for kind in ("bootstrap", "signal"):
            prefix = f"native:{kind}:"
            if marker.startswith(prefix):
                self.native(kind, marker[len(prefix) :])
                return
        runtime = re.fullmatch(
            r"runtime:runId=([^:]+):nonce=([^:\s]+):phase=(start|close)", marker
        )
        if runtime:
            run_id, nonce, phase = runtime.groups()
            if run_id != self.run_id or self.bootstrap is None:
                raise RuntimeError("runtime before native bootstrap or foreign run")
            if phase == "start":
                if self.nonce is not None:
                    raise RuntimeError("second JS runtime")
                self.nonce = nonce
            elif nonce != self.nonce or "close" not in self.seen:
                raise RuntimeError("runtime close before ten lifecycles closed")
            self.save(f"runtime-{phase}", marker)
            return
        if self.scan_budget(marker):
            return
        if marker == "listener:registered-before-initialize":
            if self.nonce is None or self.cycle < 0 or self.stage != 0:
                raise RuntimeError("listener registration outside cycle initialization")
            self.save(f"{self.cycle}:listener", marker)
            return
        if marker.startswith("event:"):
            if len(marker.encode("utf-8")) > 20000:
                raise RuntimeError("runtime event exceeds compact GATT marker budget")
            event = _json(marker[6:].encode())
            self.event_count += 1
            if (
                self.event_count > 1024
                or event.get("runId") != self.run_id
                or event.get("nonce") != self.nonce
                or type(event.get("cycle")) is not int
                or event["cycle"] != self.cycle
            ):
                raise RuntimeError("foreign cycle/run/runtime event")
            if event.get("kind") == "connectionStateChanged" and (
                event.get("commandId") is None or event.get("error") is not None
            ):
                if (
                    self.bootstrap is None
                    or self.stage != 8
                    or self.cycle in self.passive_errors
                ):
                    raise RuntimeError(
                        "duplicate/out-of-order passive disconnect event"
                    )
                self.passive_errors[self.cycle] = passive_disconnect_error(
                    event,
                    str(10004 + self.cycle * 100),
                    BUNDLES[self.bootstrap["bundle_id"]],
                )
            return  # Remaining BLE command/domain semantics belong to the host checker.
        if marker == "close:passed:listener-removed=true:probe-closed=true":
            if self.cycle != 9 or "9:passed" not in self.seen:
                raise RuntimeError("final close before ten JS lifecycles")
            self.save("close", marker)
            return
        if marker == f"passed:cycles=10:runId={self.run_id}":
            if "runtime-close" not in self.seen:
                raise RuntimeError("final passed before runtime close")
            self.save("passed", marker)
            return
        start = re.fullmatch(r"cycle:([0-9]):start", marker)
        if start:
            cycle = int(start[1])
            if (
                self.nonce is None
                or cycle != self.cycle + 1
                or self.bootstrap is not None
                and BUNDLES[self.bootstrap["bundle_id"]] == "android"
                and "scan-budget-policy" not in self.seen
                or cycle > 0
                and f"{cycle - 1}:passed" not in self.seen
            ):
                raise RuntimeError("cycle start before previous closed/passed")
            self.cycle, self.stage = cycle, 0
            self.save(f"{cycle}:start", marker)
            return
        resource = re.fullmatch(
            r"resources:cycle=([0-9]):phase=([^:]+):snapshot=(.+)", marker
        )
        if resource:
            cycle, phase, body = int(resource[1]), resource[2], resource[3]
            if cycle != self.cycle or self.bootstrap is None:
                raise RuntimeError("foreign resource cycle")
            value = _json(body.encode())
            validate_resources(
                value, phase, BUNDLES[self.bootstrap["bundle_id"]] == "android"
            )
            self.advance(
                "recovery-disconnected-resources"
                if phase == "recovery-disconnected"
                else phase,
                marker,
            )
            self.resources.setdefault(cycle, {})[phase] = value
            return
        step = re.fullmatch(r"cycle:([0-9]):(.+)", marker)
        if step is None or int(step[1]) != self.cycle:
            raise RuntimeError("malformed/foreign recovery ten marker")
        cycle, body = int(step[1]), step[2]
        if body == f"recovery-awaiting:runId={self.run_id}":
            self.advance("recovery-awaiting", marker)
        elif body == f"recovery-ready:runId={self.run_id}:source=linking":
            if self.authorized != cycle:
                raise RuntimeError("JS readiness before physical authorization")
            self.advance("recovery-ready", marker)
        elif body == "passed":
            self.advance("passed", marker)
        else:
            match = re.fullmatch(r"([^:]+):passed:(.+)", body)
            if match is None:
                raise RuntimeError("malformed recovery step")
            name, fields = match.groups()
            self.validate_step(cycle, name, fields)
            self.advance(name, marker)

    def advance(self, name: str, marker: str) -> None:
        order = (
            "initialize",
            "baseline",
            "scan",
            "connected",
            "gatt",
            "notification-enable",
            "active",
            "notification-value",
            "passive-disconnect",
            "old-route-silence",
            "passive",
            "recovery-awaiting",
            "recovery-ready",
            "recovery-connected",
            "recovery-gatt",
            "recovery-read",
            "recovery-disconnected",
            "recovery-disconnected-resources",
            "closed",
            "cycle",
            "passed",
        )

        if (
            name == "scan"
            and self.bootstrap is not None
            and BUNDLES[self.bootstrap["bundle_id"]] == "android"
            and f"{self.cycle}:scan-budget-started" not in self.seen
        ):
            raise RuntimeError("scan passed without complete Android budget markers")
        if name == "initialize" and f"{self.cycle}:listener" not in self.seen:
            raise RuntimeError("initialize marker before registered listener")
        if self.stage >= len(order) or order[self.stage] != name:
            raise RuntimeError(f"out-of-order cycle {self.cycle} marker: {name}")
        self.save(f"{self.cycle}:{name}", marker)
        self.stage += 1

    def validate_step(self, cycle: int, name: str, fields: str) -> None:
        if name == "passive-disconnect" and cycle not in self.passive_errors:
            raise RuntimeError("passive marker without native event")
        base = 10000 + cycle * 100
        c, r, g, rg, s = base + 4, base + 10, base + 6, base + 14, base + 8
        positive = r"[1-9][0-9]*"
        u64 = r"(?:0|[1-9][0-9]*)"
        routes = {
            "initialize": f"commandId={base}",
            "scan": rf"scanSessionId={base + 1}:startCommandId={base + 2}:stopCommandId={base + 3}:peripheralId={u64}:serviceUuid={re.escape(self.service_uuid)}:silenceMs=2000",
            "connected": rf"commandId={base + 5}:connectionId={c}:peripheralId={u64}",
            "gatt": rf"commandId={base + 7}:connectionId={c}:gattSessionId={g}:serviceCount={positive}:characteristicCount={positive}:descriptorCount={positive}",
            "notification-enable": rf"commandId={base + 9}:connectionId={c}:gattSessionId={g}:subscriptionId={s}:characteristicId={positive}:actualMode=Notification",
            "notification-value": f"connectionId={c}:gattSessionId={g}:subscriptionId={s}:value=ef",
            "passive-disconnect": f"commandId=none:connectionId={c}:error={self.passive_errors.get(cycle)}",
            "old-route-silence": f"connectionId={c}:gattSessionId={g}:subscriptionId={s}:observationMs=1000",
            "recovery-connected": rf"commandId={base + 11}:connectionId={r}:peripheralId={u64}",
            "recovery-gatt": rf"commandId={base + 13}:connectionId={r}:gattSessionId={rg}:serviceCount={positive}:characteristicCount={positive}:descriptorCount={positive}",
            "recovery-read": rf"commandId={base + 15}:connectionId={r}:gattSessionId={rg}:characteristicId={positive}:value=00",
            "recovery-disconnected": f"commandId={base + 12}:connectionId={r}:observationMs=1000",
            "cycle": f"scanSessionId={base + 1}:connectionId={c}:recoveryConnectionId={r}",
        }
        if name not in routes or re.fullmatch(routes[name], fields) is None:
            raise RuntimeError(f"foreign/malformed cycle {cycle} {name} route/value")


class RecoveryTen:
    """One physical owner, ten exact 2-connect/2-disconnect lifecycles."""

    def __init__(
        self,
        fixture: Any,
        root: Path,
        handles: dict[str, int],
        *,
        parent_guard: Callable[[], None] = lambda: None,
        retained_ticket: Ticket | None = None,
    ) -> None:
        self.fixture, self.root = fixture, root
        self.parent_guard = parent_guard
        self.plan = validate_control(root, fixture._fixture_session)
        self.control_bytes = _read_bytes(root / CONTROL_NAME)
        self.handles = dict(handles)
        self.process = (os.getpid(), os.getpgid(0))
        self.retained_ticket = retained_ticket
        if retained_ticket is not None:
            retained_ticket.encode()
            if (
                retained_ticket.owner_nonce != owner_nonce(self.plan.session_id)
                or retained_ticket.operation is not Operation.READ
                or retained_ticket.handle != self.handles["read-write"]
            ):
                raise ValueError("recovery retained witness owner/READ/target drift")
        self.fault = FaultController(
            fixture._session, self.plan.session_id
        )  # A supplied retired witness is read-only; this scenario never ARMs.
        self.facts = RuntimeFacts(self.plan.session_id, self.plan.service_uuid)
        self.whole_deadline = time.monotonic() + WHOLE_SECONDS
        self.cycle = -1
        self.phase = "preparing"
        self.peer: tuple[str, int] | None = None
        self.allow_connection = False
        self.allow_disconnect = False
        self.link_events: list[dict[str, Any]] = []
        self.record: dict[str, Any] | None = None
        self.evidence: dict[str, Any] = {
            "schema_version": 1,
            "mode": MODE,
            "status": "failed",
            "run_id": self.plan.session_id,
            "session_id": self.plan.session_id,
            "fixture_pid": self.process[0],
            "fixture_pgid": self.process[1],
            "contract_sha256": self.plan.contract_sha256,
            "profile_sha256": self.plan.profile_sha256,
            "resource_identity": self.plan.resource_identity,
            "port": self.plan.port,
            "service_uuid": self.plan.service_uuid,
            "profile_id": fixture._profile.profile_id,
            "profile": read_document(self.plan.profile_path),
            "handles": self.handles,
            "live_receipt_path": str(self.plan.receipt_path),
            "budgets_seconds": {
                "phase": PHASE_SECONDS,
                "whole": WHOLE_SECONDS,
                "ack": ACK_SECONDS,
                "cleanup": CLEANUP_SECONDS,
            },
            "cycles": [],
            "boundaries": [],
            "errors": [],
            "primary_error": None,
            "cleanup_errors": [],
            "hold_status": {"state": None, "identity": None},
            "native_bootstrap": None,
            "native_signals": {},
            "final_groups": None,
        }
        if retained_ticket is not None:
            self.evidence.update(
                retained_ticket=ticket_document(retained_ticket), fault_samples=[]
            )

    def boundary(self, name: str, **fields: Any) -> None:
        value = {
            "boundary": name,
            "cycle": self.cycle,
            "monotonic": time.monotonic(),
            **fields,
        }
        self.evidence["boundaries"].append(value)
        if self.record is not None:
            self.record["boundaries"].append(value)

    def progress(self, phase: str, status: str = "running") -> None:
        if phase not in PROGRESS_PHASES or status not in STATUSES:
            raise ValueError("invalid recovery ten progress phase/status")
        self.phase = phase
        write_document(
            self.root / PROGRESS_NAME,
            {
                "schema_version": 1,
                "mode": MODE,
                "run_id": self.plan.session_id,
                "cycle": self.cycle,
                "phase": phase,
                "status": status,
                "monotonic": time.monotonic(),
                "recovery_url": self.facts.url(self.cycle)
                if phase == "recovery-ready"
                else None,
                "physical_closed": phase == "closed" and status == "passed",
            },
        )

    def guard(self, deadline: float) -> None:
        self.parent_guard()
        self.fixture._transport.require()
        if self.fixture._stop_requested or (self.root / "abort").exists():
            raise RuntimeError("recovery ten aborted by parent")
        if _read_bytes(self.root / CONTROL_NAME) != self.control_bytes:
            raise RuntimeError("recovery ten immutable control changed")
        if (os.getpid(), os.getpgid(0)) != self.process:
            raise RuntimeError("provider PID/PGID changed")
        if time.monotonic() >= min(deadline, self.whole_deadline):
            raise TimeoutError(f"recovery ten absolute budget exceeded: {self.phase}")

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
            raise RuntimeError("malformed GAP lifecycle event")
        peer = (event.payload[1:7][::-1].hex(), event.payload[0])
        peers = self.fixture._connected_peers
        connected = event.opcode == GAP_DEVICE_CONNECTED
        if connected:
            if not self.allow_connection or self.peer is not None or peers:
                raise RuntimeError("foreign/duplicate/early GAP connection")
            self.peer = peer
            peers.add(peer)
            self.allow_connection = False
        else:
            if not self.allow_disconnect or self.peer != peer or peers != {peer}:
                raise RuntimeError("foreign/duplicate/early GAP disconnect")
            peers.remove(peer)
            self.peer = None
            self.allow_disconnect = False
        fact = {
            "kind": "connected" if connected else "disconnected",
            "peer": list(peer),
            "payload_hex": event.payload.hex(),
            "monotonic": time.monotonic(),
        }
        self.link_events.append(fact)
        self.boundary("gap-" + fact["kind"], **fact)

    def drain(self) -> None:
        for _ in range(256):
            event = self.fixture._session.pop_event()
            if event is None:
                break
            self.observe_gap(event)
        else:
            raise RuntimeError("recovery GAP backlog did not reach idle")
        if self.fixture._connected_peers != ({self.peer} if self.peer else set()):
            raise RuntimeError("exact GAP tracker drift")

    def retained_idle(self, deadline: float, guard: Callable[[], None]) -> None:
        """Only Tester evidence TX may drain under the exact old retired witness.

        Zephyr att_fault.inc sets REARMABLE after releasing the five fault owners;
        btp.c also counts ordinary replies/GAP events in its global sixth counter.
        Neither observation alone proves idle: require cleanup_ready AND fresh limits.
        """
        ticket = self.retained_ticket
        assert ticket is not None
        deadline = min(deadline, self.whole_deadline)
        while True:
            guard()
            self.drain()
            sample: dict[str, Any] = {
                "cycle": self.cycle,
                "phase": self.phase,
                "monotonic": time.monotonic(),
                "deadline": deadline,
                "status_opcode": None,
                "status_wire": None,
                "limits": None,
                "error": None,
            }
            samples = self.evidence["fault_samples"]
            if len(samples) == FAULT_SAMPLES_MAX:
                # Retain the actual primary/cleanup failure even if subsequent
                # read-only cleanup succeeds. Ordinary idle polling is not history.
                removable = next(
                    (i for i, old in enumerate(samples) if old["error"] is None), None
                )
                if removable is None:
                    raise RuntimeError("recovery retained diagnostic capacity exceeded")
                del samples[removable]
            samples.append(sample)
            try:
                # Capture even a foreign/non-success reply before semantic validation;
                # use the existing wire codec, not a fabricated IDLE ticket or receipt.
                response = self.fixture._session.request(
                    service=VENDOR,
                    opcode=int(Opcode.STATUS),
                    index=INDEX_NONE,
                    payload=encode_ticket_command(Opcode.STATUS, ticket),
                    timeout=min(1.0, deadline - time.monotonic()),
                )
                sample.update(
                    status_opcode=response.opcode, status_wire=response.payload.hex()
                )
                guard()
                self.drain()
                require_success(response, operation="recovery retained STATUS")
                if (response.service, response.opcode, response.index) != (
                    VENDOR,
                    Opcode.STATUS,
                    INDEX_NONE,
                ):
                    raise RuntimeError(
                        "recovery retained STATUS response routing drift"
                    )
                snapshot = decode_status(response.payload)
                refs = snapshot.refs
                if (
                    snapshot.ticket != ticket
                    or snapshot.phase is not Phase.RETIRED
                    or snapshot.winner is not Winner.DISCONNECT
                    or snapshot.outcome is not Outcome.DISCONNECTED_UNCOMMITTED
                    or not snapshot.flags & StatusFlags.REARMABLE
                    or snapshot.infrastructure_error is not InfrastructureError.NONE
                    or snapshot.flags & StatusFlags.INFRASTRUCTURE_FAILED
                    or refs != RefCounters(0, 0, 0, 0, 0, refs.evidence_pending)
                ):
                    raise RuntimeError(
                        "recovery retained witness not exact retired/rearmable/owner-free"
                    )
                limits = self.fault.get_limits(
                    timeout=min(1.0, deadline - time.monotonic())
                )
                sample["limits"] = {
                    "boot_nonce": limits.boot_nonce.hex(),
                    "engine_flags": int(limits.engine_flags),
                    "peer_present": limits.peer_present,
                    "peer": limits.peer.hex(),
                    "connection_epoch": limits.connection_epoch,
                    "bearer_epoch": limits.bearer_epoch,
                    "database_epoch": limits.database_epoch,
                }
                guard()
                self.drain()
                if (
                    limits.boot_nonce != ticket.boot_nonce
                    or limits.engine_flags & ~EngineFlags.BUSY
                ):
                    raise RuntimeError("recovery retained limits boot/quarantine drift")
                if cleanup_ready(snapshot) and not limits.engine_flags:
                    self.evidence["hold_status"] = {
                        "protocol_version": 2,
                        "engine_flags": 0,
                        "boot_nonce": limits.boot_nonce.hex(),
                        "retained_status": status_document(snapshot),
                    }
                    return
                # No progress, mutation or resource-zero checkpoint on a BUSY sample.
                time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
            except Exception as error:
                sample["error"] = str(error)
                raise

    def sample(self, deadline: float) -> None:
        self.guard(deadline)
        self.drain()
        if self.retained_ticket is None:
            limits = self.fault.require_idle()
            self.evidence["hold_status"] = {
                "protocol_version": 2,
                "engine_flags": int(limits.engine_flags),
            }
        else:
            self.retained_idle(deadline, lambda: self.guard(deadline))
        self.guard(deadline)
        self.drain()
        self.facts.refresh(self.root / "runtime.log")
        self.guard(deadline)

    def wait(
        self,
        label: str,
        predicate: Callable[[], bool],
        *,
        seconds: float = PHASE_SECONDS,
    ) -> None:
        self.progress(label)
        deadline = min(self.whole_deadline, time.monotonic() + seconds)
        while True:
            self.sample(deadline)
            if predicate():
                self.boundary(label)
                return
            time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))

    def seen(self, *names: str) -> bool:
        return all(f"{self.cycle}:{name}" in self.facts.seen for name in names)

    def no_peer(self) -> None:
        self.drain()
        if self.peer is not None or self.fixture._connected_peers:
            raise RuntimeError("no-peer physical boundary violated")

    def prepare(self) -> None:
        verify_receipt(self.plan, fixture_identity=self.process)
        deadline = min(self.whole_deadline, time.monotonic() + PHASE_SECONDS)
        self.fixture._gap.stop_advertising()
        self.sample(deadline)
        self.no_peer()
        if self.fixture._profile.notification_value != b"\xef":
            raise RuntimeError("recovery ten requires profile notifyValue EF")
        for role in ("read-write", "updates"):
            characteristic = next(
                c for c in self.fixture._profile.characteristics if c.role == role
            )
            attributes = self.fixture._gatt.get_attributes(
                attribute_uuid=characteristic.uuid
            )
            self.guard(deadline)
            self.drain()
            if (
                len(attributes) != 1
                or attributes[0].handle != self.handles[role]
                or attributes[0].uuid != characteristic.uuid
                or attributes[0].permissions
                != tester_characteristic_native_permissions(characteristic.permissions)
                or role == "read-write"
                and characteristic.initial_value != b"\x00"
            ):
                raise RuntimeError(f"actual profile/target handle mismatch: {role}")
        self.boundary("prepared", hold_inactive=True, peer_count=0)

    def advertise(self, *, accept_peer: bool = True) -> None:
        self.no_peer()
        self.allow_connection = accept_peer
        self.fixture._start_advertising()
        self.boundary("advertising-started", peer_count=0)

    def run_cycle(self, cycle: int) -> None:
        self.cycle = cycle
        self.link_events = []
        self.record = {"cycle": cycle, "status": "running", "boundaries": []}
        self.evidence["cycles"].append(self.record)
        self.sample(min(self.whole_deadline, time.monotonic() + PHASE_SECONDS))
        self.no_peer()
        self.record["initial_hold_status"] = self.evidence["hold_status"]
        self.boundary("cycle-before-start", hold_inactive=True, peer_count=0)
        self.advertise()
        self.wait(
            "subscription-enabled",
            lambda: (
                self.peer is not None
                and self.seen(
                    "start", "connected", "gatt", "notification-enable", "active"
                )
            ),
        )
        self.fixture._gap.stop_advertising()
        old_peer = self.peer
        self.record["old_peer"] = list(old_peer) if old_peer else None
        self.fixture._gatt.set_value(
            self.handles["updates"], self.fixture._profile.notification_value
        )
        self.boundary(
            "notification-submitted", handle=self.handles["updates"], value_hex="ef"
        )
        self.wait(
            "notification-observed",
            lambda: self.peer == old_peer and self.seen("notification-value", "active"),
        )
        self.allow_disconnect = True
        self.fixture._gap.disconnect(*old_peer)
        self.boundary("passive-fault-requested", peer=list(old_peer))
        self.wait(
            "passive-observed",
            lambda: (
                self.peer is None
                and len(self.link_events) == 2
                and self.seen(
                    "passive-disconnect",
                    "old-route-silence",
                    "passive",
                    "recovery-awaiting",
                )
            ),
        )
        self.no_peer()
        self.advertise(accept_peer=False)
        self.sample(min(self.whole_deadline, time.monotonic() + PHASE_SECONDS))
        self.no_peer()
        self.allow_connection = True
        self.facts.authorized = cycle
        ready_at = time.monotonic()
        self.allow_disconnect = True
        self.wait(
            "recovery-ready",
            lambda: self.seen("recovery-ready", "native-signal"),
            seconds=ACK_SECONDS,
        )
        self.record["readiness_ack_seconds"] = time.monotonic() - ready_at
        # JS can submit its logical disconnect before the cumulative console snapshot
        # reaches the provider. Its ordered read marker and the exact GAP pair remain
        # mandatory; do not require the recovered link to still be live at sampling.
        self.wait(
            "recovery-read",
            lambda: (
                len(self.link_events) >= 3
                and self.seen("recovery-connected", "recovery-gatt", "recovery-read")
            ),
        )
        self.record["new_peer"] = self.link_events[2]["peer"]
        # No local RW SET_VALUE, reset or profile replacement: 00 is a remote JS read.
        self.fixture._gap.stop_advertising()
        self.wait(
            "cycle-closed",
            lambda: (
                self.peer is None
                and len(self.link_events) == 4
                and self.seen(
                    "recovery-disconnected",
                    "recovery-disconnected-resources",
                    "closed",
                    "passed",
                )
            ),
        )
        self.no_peer()
        self.record.update(
            status="passed",
            gap_events=list(self.link_events),
            resources=self.facts.resources[cycle],
            notification_value_hex="ef",
            remote_read_value_hex="00",
        )
        self.record["final_hold_status"] = self.evidence["hold_status"]
        self.record["cleanup"] = {
            "hold_inactive": True,
            "peer_count": 0,
            "advertising_stop_confirmed": True,
        }
        self.boundary("physical-cycle-closed", hold_inactive=True, peer_count=0)
        self.progress("cycle-closed")
        self.record = None

    def cleanup(self) -> None:
        """Never adopt foreign peers/holds, even when primary validation failed."""
        deadline = time.monotonic() + CLEANUP_SECONDS
        for name, action in (
            ("stop-advertising", self.fixture._gap.stop_advertising),
            ("owned-peer-disconnect", lambda: self.cleanup_peer(deadline)),
            ("hold-status", lambda: self.cleanup_hold(deadline)),
        ):
            try:
                self.fixture._transport.require()
                if time.monotonic() >= deadline:
                    raise TimeoutError("cleanup absolute budget exceeded")
                action()
                if time.monotonic() >= deadline:
                    raise TimeoutError("cleanup response exceeded absolute budget")
            except (OSError, ValueError, RuntimeError, EOFError, TimeoutError) as error:
                self.evidence["cleanup_errors"].append(f"{name}: {error}")
        self.evidence["cleanup"] = {
            "hold_inactive": not any(
                e.startswith("hold-status:") for e in self.evidence["cleanup_errors"]
            ),
            "peer_count": len(self.fixture._connected_peers),
            "owned_peer_remaining": list(self.peer) if self.peer else None,
            "advertising_stop_confirmed": not any(
                e.startswith("stop-advertising:")
                for e in self.evidence["cleanup_errors"]
            ),
        }

    def cleanup_peer(self, deadline: float) -> None:
        # A failed sample may have buffered a legitimate connection/disconnect.
        # Consume it with the existing exact tracker/admission flags before deciding
        # whether we own a peer to disconnect. Unknown events still fail closed.
        self.fixture._transport.require()
        if time.monotonic() >= deadline:
            raise TimeoutError("owned peer cleanup event deadline")
        self.drain()
        if self.peer is None:
            return
        self.allow_connection = False
        self.allow_disconnect = True
        self.boundary("cleanup-owned-peer-disconnect", peer=list(self.peer))
        self.fixture._gap.disconnect(*self.peer)
        while self.peer is not None:
            self.fixture._transport.require()
            if time.monotonic() >= deadline:
                raise TimeoutError("owned peer cleanup disconnect not observed")
            event = (
                self.fixture._session.pop_event()
                or self.fixture._session.poll_event(
                    min(POLL_SECONDS, deadline - time.monotonic())
                )
            )
            if event is not None:
                self.observe_gap(event)

    def cleanup_hold(self, deadline: float) -> None:
        if self.retained_ticket is not None:

            def guard() -> None:
                self.fixture._transport.require()
                if time.monotonic() >= deadline:
                    raise TimeoutError("cleanup retained witness deadline")

            self.retained_idle(deadline, guard)
            return
        # Standalone has no retained authority: preserve fail-closed require_idle.
        limits = FaultController(
            self.fixture._session, self.plan.session_id
        ).require_idle()
        self.evidence["hold_status"] = {
            "protocol_version": 2,
            "engine_flags": int(limits.engine_flags),
        }

    def run(self, on_ready: Callable[[], None] = lambda: None) -> None:
        try:
            self.progress("preparing")
            self.prepare()
            on_ready()  # Live receipt + no-peer/inactive proof precede provider READY.
            for cycle in range(10):
                self.run_cycle(cycle)
            self.wait(
                "runtime-closed",
                lambda: all(
                    key in self.facts.seen
                    for key in ("close", "runtime-close", "passed")
                ),
            )
            self.allow_connection = False
            self.fixture._gap.stop_advertising()
            self.sample(min(self.whole_deadline, time.monotonic() + PHASE_SECONDS))
            self.no_peer()
            verify_receipt(self.plan, fixture_identity=self.process)
            self.boundary(
                "physical-closed",
                hold_inactive=True,
                peer_count=0,
                advertising_stopped=True,
            )
            self.evidence.update(
                status="passed",
                native_bootstrap=self.facts.bootstrap,
                native_signals=self.facts.signals,
                resource_scope="logical native owners; OS phone peer cache excluded",
                final_groups={
                    "cycles": 10,
                    "connections": sum(
                        event["kind"] == "connected"
                        for cycle in self.evidence["cycles"]
                        for event in cycle["gap_events"]
                    ),
                    "disconnections": sum(
                        event["kind"] == "disconnected"
                        for cycle in self.evidence["cycles"]
                        for event in cycle["gap_events"]
                    ),
                    "closed_snapshots": sum(
                        "closed" in cycle["resources"]
                        for cycle in self.evidence["cycles"]
                    ),
                },
                cleanup={
                    "hold_inactive": True,
                    "peer_count": 0,
                    "advertising_stop_confirmed": True,
                },
            )
            # Commit the immutable document last; no fallible work after a passed
            # physical publication can turn it into stale failed evidence.
            self.progress("closed", "passed")
            _publish_receipt(self.root / PHYSICAL_NAME, self.evidence)
        except Exception as error:
            self.evidence["status"] = "failed"
            self.evidence["errors"].append(str(error))
            self.evidence["primary_error"] = str(error)
            self.evidence.update(
                native_bootstrap=self.facts.bootstrap, native_signals=self.facts.signals
            )
            self.cleanup()
            if self.record is not None:
                self.record.update(status="failed", gap_events=list(self.link_events))
            for publish in (
                lambda: _publish_receipt(self.root / PHYSICAL_NAME, self.evidence),
                lambda: self.progress("closed", "failed"),
            ):
                try:
                    publish()
                except (OSError, ValueError, RuntimeError) as publication_error:
                    error.add_note(
                        f"recovery ten evidence publication failed: {publication_error}"
                    )
            raise


def run(
    fixture: Any,
    root: Path,
    handles: dict[str, int],
    *,
    on_ready: Callable[[], None] = lambda: None,
) -> None:
    RecoveryTen(fixture, root, handles).run(on_ready)
