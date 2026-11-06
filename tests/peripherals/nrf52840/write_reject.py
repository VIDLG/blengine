"""Named response-write REJECT protocol and coordinator on one attested DirectFixture.

Flow 10 is a control discriminator, not an additional continuous10 flow. Capture
uses WriteRejectRelay(root): directory is root, request/signal files live there,
and physical evidence is published before each signal. No serial/lease is opened
here. Normal recovery and detached retirement never acquire a replacement ticket.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .cancellation_control import (
    Control,
    load_control,
    read_document,
    require_multifunction,
    write_document,
)
from .fault_control import (
    ArmCommon,
    ArmRequest,
    Decision,
    FaultController,
    InfrastructureError,
    Operation,
    Outcome,
    Phase,
    RefCounters,
    Status,
    Winner,
    cleanup_ready,
    owner_nonce,
    status_document,
    status_from_document,
)
from .fault_control import (
    StatusFlags as F,
)
from .fixture_session import FixtureSession, _json, read_live_document, verify_receipt
from .gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED
from .gatt_commands import tester_characteristic_native_permissions

MODE = "write-reject-attested"
RUNTIME_MODE = "rf-write-reject"
PREFIX = "BLEHUB_NITRO_RUNTIME "
CONTROL_NAME = "write-reject-control.json"
PHYSICAL_NAME = "write-reject-physical.json"
REQUESTS = ("arm", "write-dispatched", "reject", "recovery-done")
PHASES = ("armed", "held", "rejected", "close-allowed")
BOUNDARIES = ("armed", "held", "rejected", "recovery")
BEFORE, REQUESTED, RECOVERED = b"\x00", b"\x11", b"\x22"
ARM_MS, HOLD_MS = 7000, 9000
REJECT_SECONDS, RETIRE_SECONDS, WHOLE_SECONDS = 7.0, 5.0, 180.0
MAX_RUNTIME_BYTES = 8 * 1024 * 1024
RESOURCE_FIELDS = (
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
STEPS = (
    "connected",
    "gatt",
    "baseline-read",
    "write-dispatched",
    "queued-read-dispatched",
    "write-rejected",
    "unchanged-read",
    "recovery-write",
    "recovery-read",
    "disconnected",
    "close",
)
COMMANDS = {
    "initialize": "30000",
    "scan-start": "30002",
    "scan-stop": "30003",
    "connected": "30005",
    "disconnected": "30006",
    "gatt": "30008",
    "baseline-read": "30009",
    "write-dispatched": "30010",
    "queued-read-dispatched": "30011",
    "recovery-write": "30012",
    "recovery-read": "30013",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"write-reject: {message}")


def prepare_control(root: Path, plan: FixtureSession) -> Control:
    require_multifunction(plan)
    control = Control(plan.session_id, 10, RUNTIME_MODE, str(uuid.uuid4()))
    require(not (root / CONTROL_NAME).exists(), "refusing existing control")
    write_document(root / CONTROL_NAME, asdict(control))
    return control


def validate_control(root: Path, plan: FixtureSession | None) -> Control:
    plan = require_multifunction(plan)
    control = load_control(root / CONTROL_NAME)
    require(
        (control.flow, control.mode, control.session)
        == (10, RUNTIME_MODE, plan.session_id),
        "foreign control/session",
    )
    return control


def control_records(control: Control) -> list[str]:
    return [
        control.marker("listener", "registered"),
        *(
            marker
            for request, phase in zip(REQUESTS, PHASES, strict=True)
            for marker in (
                control.marker("ready", request),
                control.marker("observed", phase),
            )
        ),
    ]


def _fields(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in raw.split(":"):
        key, separator, value = field.partition("=")
        require(
            bool(separator) and bool(value) and key not in result,
            "malformed/duplicate marker field",
        )
        result[key] = value
    return result


def runtime_facts(
    text: str, *, run_id: str, platform: str | None = None, complete: bool = False
) -> dict[str, Any]:
    """Replay a bounded, append-only runtime snapshot; partial snapshots are not passes.

    Events retain the full projected error and list-of-byte value. Domain route
    fields are mandatory; commandCompleted normally carries only its command ID.
    Unknown, duplicate and failed scenario records always fail, including at close.
    """
    require(len(text.encode("utf-8")) <= MAX_RUNTIME_BYTES, "runtime exceeds budget")
    require(platform in (None, "android", "ios"), "unknown platform")
    nonce = None
    events: list[dict[str, Any]] = []
    steps: dict[str, dict[str, str]] = {}
    positions: dict[str, int] = {}
    terminals: dict[str, dict[str, Any]] = {}
    domains: set[tuple[str, str]] = set()
    resources = None
    closed = passed = False
    records: list[str] = []
    characteristic = None
    reject_ready = False
    for line_number, line in enumerate(text.splitlines(), 1):
        if PREFIX not in line:
            continue
        body = line.split(PREFIX, 1)[1].strip()
        if (
            body.startswith("rf-control:ready:")
            and ":flow=10:mode=rf-write-reject:" in body
            and body.endswith(":phase=reject")
        ):
            reject_ready = True
        if not body.startswith(RUNTIME_MODE + ":"):
            continue
        require(not passed, "record after passed")
        records.append(body)
        raw = body[len(RUNTIME_MODE) + 1 :]
        if raw.startswith("runtime:"):
            fields = _fields(raw[8:])
            require(
                set(fields) == {"session", "nonce", "phase"}
                and fields["session"] == run_id,
                "runtime identity",
            )
            require(
                bool(re.fullmatch(r"[A-Za-z0-9_-]{1,160}", fields["nonce"])),
                "runtime nonce",
            )
            if fields["phase"] == "start":
                require(
                    nonce is None and len(records) == 1, "duplicate/restarted runtime"
                )
                nonce = fields["nonce"]
            else:
                require(
                    fields["phase"] == "close"
                    and fields["nonce"] == nonce
                    and not closed
                    and "close" in steps
                    and resources is not None,
                    "runtime close order",
                )
                closed = True
        elif raw.startswith("event:"):
            require(
                nonce is not None and not closed and len(events) < 256,
                "event outside runtime/budget",
            )
            event = _json(raw[6:].encode())
            require(
                event.get("runId") == run_id and event.get("nonce") == nonce,
                "event identity",
            )
            kind, command = event.get("kind"), event.get("commandId")
            require(isinstance(kind, str), "event kind")
            if kind == "commandCompleted":
                require(
                    command in COMMANDS.values() and command not in terminals,
                    "unknown/duplicate terminal",
                )
                error = event.get("error")
                if command == "30010":
                    require(reject_ready, "write failed before REJECT readiness")
                    require(
                        isinstance(error, dict) and error.get("category") == "gatt",
                        "rejected write terminal category",
                    )
                    native = error.get("platformDetails")
                    require(isinstance(native, dict), "missing native ATT error")
                    expected = {
                        "android": ("platform_android", "BluetoothGatt"),
                        "ios": ("platform_apple", "CBATTErrorDomain"),
                    }
                    actual = (native.get("platform"), native.get("name"))
                    require(
                        actual
                        in ([expected[platform]] if platform else expected.values())
                        and native.get("code") == "14",
                        "native error is not ATT 0x0e",
                    )
                    require(
                        "queued-read-dispatched" in steps,
                        "failure before queued-read dispatch",
                    )
                else:
                    require(error is None, "unexpected command failure")
                    required_domain = {
                        "30002": "scanStarted:",
                        "30003": "scanStopped:",
                        "30005": "connectionStateChanged:Connected",
                        "30006": "connectionStateChanged:Disconnected",
                        "30008": "gattDiscovered:",
                        "30009": "characteristicRead:",
                        "30011": "characteristicRead:",
                        "30012": "characteristicWritten:",
                        "30013": "characteristicRead:",
                    }.get(command)
                    require(
                        required_domain is None
                        or (command, required_domain) in domains,
                        "success terminal before domain",
                    )
                terminals[command] = event
            elif kind == "advertisementObserved":
                require(
                    command is None and event.get("scanSessionId") == "30001",
                    "advertisement route",
                )
            elif kind == "adapterSnapshotChanged":
                require(
                    command in (None, "30000")
                    and event.get("adapterState") == "PoweredOn"
                    and event.get("scanAuthorization") == "Authorized"
                    and not event.get("error"),
                    "adapter readiness",
                )
            else:
                require(
                    command in COMMANDS.values()
                    and command not in terminals
                    and not event.get("error"),
                    "unknown/late domain event",
                )
                domain = kind + ":" + str(event.get("connectionState", ""))
                require((command, domain) not in domains, "duplicate domain event")
                domains.add((command, domain))
                expected_kind = {
                    "30002": "scanStarted",
                    "30003": "scanStopped",
                    "30005": "connectionStateChanged",
                    "30006": "connectionStateChanged",
                    "30008": "gattDiscovered",
                    "30009": "characteristicRead",
                    "30011": "characteristicRead",
                    "30012": "characteristicWritten",
                    "30013": "characteristicRead",
                }
                require(
                    kind == expected_kind.get(command),
                    "unexpected domain/success for rejected write",
                )
                if command in ("30002", "30003"):
                    require(event.get("scanSessionId") == "30001", "scan route")
                else:
                    require(event.get("connectionId") == "30004", "connection route")
                if command in ("30005", "30006"):
                    require(
                        event.get("connectionState")
                        in (
                            ("Connecting", "Connected")
                            if command == "30005"
                            else ("Disconnecting", "Disconnected")
                        ),
                        "connection state",
                    )
                    if command == "30006":
                        require(
                            (command, "connectionStateChanged:Disconnected")
                            not in domains
                            or event.get("connectionState") == "Disconnected",
                            "disconnect state after Disconnected",
                        )
                if command in ("30008", "30009", "30011", "30012", "30013"):
                    require(event.get("gattSessionId") == "30007", "GATT route")
                if command in ("30009", "30011", "30012", "30013"):
                    require(
                        characteristic is not None
                        and event.get("characteristicId") == characteristic,
                        "characteristic route",
                    )
                if command in ("30009", "30011", "30013"):
                    require(
                        event.get("value") == ([34] if command == "30013" else [0])
                        and all(type(v) is int for v in event["value"])
                        and event.get("valueTruncated") in (None, False)
                        and type(event.get("valueByteLength", 1)) is int
                        and event.get("valueByteLength", 1) == 1,
                        "readback bytes",
                    )
                if command in ("30011", "30012", "30013"):
                    require(
                        "30010" in terminals, "recovery domain before rejected terminal"
                    )
            for field, value in (
                ("connectionId", "30004"),
                ("gattSessionId", "30007"),
                ("scanSessionId", "30001"),
            ):
                require(event.get(field) in (None, value), "foreign optional route")
            if event.get("characteristicId") is not None:
                require(
                    event["characteristicId"] == characteristic,
                    "foreign optional characteristic",
                )
            events.append(event)
        elif raw.startswith("step:"):
            match = re.fullmatch(r"step:([^:]+):passed(?::(.*))?", raw)
            require(
                match is not None and nonce is not None and not closed,
                "failed/malformed step",
            )
            name = match[1]
            require(
                name
                in (
                    *STEPS,
                    "initialize",
                    "init",
                    "scan",
                    "scan-start",
                    "scan-stop",
                    "advertisement",
                )
                and name not in steps,
                "unknown/duplicate step",
            )
            fields = _fields(match[2]) if match[2] else {}
            if name in STEPS:
                require(
                    [n for n in steps if n in STEPS]
                    == list(STEPS[: STEPS.index(name)]),
                    "step order",
                )
            if name in STEPS[:-2]:
                require(fields.get("connectionId") == "30004", "step connection route")
            if name in STEPS[1:-2]:
                require(fields.get("gattSessionId") == "30007", "step GATT route")
                current = fields.get("characteristicId")
                require(
                    isinstance(current, str)
                    and bool(re.fullmatch(r"[1-9][0-9]{0,19}", current))
                    and int(current) < 1 << 64,
                    "step characteristic",
                )
                if characteristic is None:
                    characteristic = current
                require(characteristic == current, "step changed characteristic")
            if "commandId" in fields:
                expected_command = {
                    **COMMANDS,
                    "write-rejected": "30010",
                    "unchanged-read": "30011",
                }.get(name)
                require(fields["commandId"] == expected_command, "step command ID")
            if name in ("write-dispatched", "queued-read-dispatched"):
                require(
                    "30010" not in terminals and "30011" not in terminals,
                    "write/read terminal before held queue witness",
                )
            completed_command = {
                "connected": "30005",
                "gatt": "30008",
                "baseline-read": "30009",
                "write-rejected": "30010",
                "unchanged-read": "30011",
                "recovery-write": "30012",
                "recovery-read": "30013",
                "disconnected": "30006",
            }.get(name)
            if completed_command:
                require(completed_command in terminals, "step before native terminal")
            if name == "disconnected":
                require(fields.get("connectionId") == "30004", "disconnect step route")

            steps[name], positions[name] = fields, line_number
        elif raw.startswith("resources:phase=closed:snapshot="):
            require(
                resources is None and "disconnected" in steps and not closed,
                "resource snapshot order",
            )
            resources = _json(raw.split("snapshot=", 1)[1].encode())
            require(
                set(resources) == set(RESOURCE_FIELDS)
                and all(type(v) is int and v == 0 for v in resources.values()),
                "native close is not seventeen zeros",
            )
        elif raw == "passed":
            require(closed, "pass before runtime close")
            passed = True
        else:
            raise ValueError(f"write-reject: failed/unknown marker: {raw}")
    if complete:
        require(
            passed and closed and tuple(n for n in steps if n in STEPS) == STEPS,
            "incomplete runtime",
        )
        require(set(terminals) == set(COMMANDS.values()), "missing command terminal")
        required_domains = {
            ("30002", "scanStarted:"),
            ("30003", "scanStopped:"),
            ("30005", "connectionStateChanged:Connecting"),
            ("30005", "connectionStateChanged:Connected"),
            ("30006", "connectionStateChanged:Disconnected"),
            ("30008", "gattDiscovered:"),
            ("30009", "characteristicRead:"),
            ("30011", "characteristicRead:"),
            ("30012", "characteristicWritten:"),
            ("30013", "characteristicRead:"),
        }
        require(
            domains - {("30006", "connectionStateChanged:Disconnecting")}
            == required_domains,
            "missing/unexpected native domain",
        )
    return {
        "run_id": run_id,
        "nonce": nonce,
        "events": events,
        "steps": steps,
        "positions": positions,
        "resources": resources,
        "closed": closed,
        "passed": passed,
        "records": records,
    }


def validate_target(facts: dict[str, Any], service_uuid: str, target_uuid: str) -> None:
    """Bind App discovery to the profile target without inventing an ATT handle."""
    events = [e for e in facts["events"] if e["kind"] == "gattDiscovered"]
    require(len(events) == 1, "unique App GATT discovery")
    services = events[0].get("gattServices")
    require(
        isinstance(services, list)
        and len(services) == 1
        and services[0].get("uuid") == service_uuid,
        "App service UUID",
    )
    matches = [
        c
        for c in services[0].get("characteristics", [])
        if c.get("uuid") == target_uuid
    ]
    require(
        len(matches) == 1
        and matches[0].get("id") == facts["steps"]["gatt"]["characteristicId"]
        and matches[0].get("readable") is True
        and matches[0].get("writableWithResponse") is True,
        "App exact writable target",
    )


def verify_control_transcript(text: str, control: Control) -> dict[str, Any]:
    """Validate cross-boundary ordering, not merely each stream's own subsequence."""
    facts = runtime_facts(text, run_id=control.session)
    records = []
    positions = {}
    for number, line in enumerate(text.splitlines(), 1):
        if PREFIX in line:
            body = line.split(PREFIX, 1)[1].strip()
            if body.startswith("rf-control:"):
                records.append(body)
                positions[body] = number
    expected = control_records(control)
    require(
        records == expected[: len(records)] and len(records) <= len(expected),
        "control transcript order",
    )
    for index, (request, phase) in enumerate(zip(REQUESTS, PHASES, strict=True)):
        ready = control.marker("ready", request)
        observed = control.marker("observed", phase)
        if ready in positions:
            step = (
                "baseline-read",
                "write-dispatched",
                "queued-read-dispatched",
                "recovery-read",
            )[index]
            require(
                step in facts["positions"]
                and facts["positions"][step] < positions[ready],
                "control request before App witness",
            )
        next_step = (
            "write-dispatched",
            "queued-read-dispatched",
            "recovery-write",
            "disconnected",
        )[index]
        if next_step in facts["positions"]:
            require(
                observed in positions
                and positions[observed] < facts["positions"][next_step],
                "App action before control ACK",
            )
    return facts


def validate_status(
    status: Status, *, phase: Phase, ticket=None, peer: bytes | None = None
) -> None:
    require(
        status.ticket.operation is Operation.RESPONSE_WRITE
        and (ticket is None or status.ticket == ticket),
        "status ticket/operation",
    )
    require(peer is None or status.peer == peer, "status peer")
    require(
        status.before == status.after == BEFORE
        and status.normal_att_result == status.callback_result == 0,
        "callback/local receipt drift",
    )
    require(
        not status.flags
        & (
            F.CALLBACK_INVOKED
            | F.COMMIT_OBSERVED
            | F.NORMAL_PATH_EVALUATED
            | F.TX_FAILED
            | F.INFRASTRUCTURE_FAILED
        )
        and status.infrastructure_error is InfrastructureError.NONE,
        "write callback/commit/infrastructure failure",
    )
    require(status.phase is phase, "status phase")
    if phase is Phase.ARMED:
        require(
            status.winner is Winner.NONE
            and status.outcome is Outcome.PENDING
            and not status.flags & F.REQUEST_CAPTURED
            and status.observed == b""
            and status.remaining.arm_ms > 0,
            "ARMED receipt",
        )
    else:
        require(
            status.request_opcode == 0x12
            and status.offset == status.derived_write_flags == 0
            and status.observed == REQUESTED
            and bool(status.flags & F.REQUEST_CAPTURED),
            "exact write payload was not captured",
        )
        if phase is Phase.HELD:
            require(
                status.winner is Winner.NONE
                and status.outcome is Outcome.PENDING
                and status.remaining.hold_ms > 0
                and not status.flags & (F.RESPONSE_QUEUED | F.ANSWER_ORDERED),
                "HELD receipt",
            )
        elif phase is Phase.RETIRED:
            required = (
                F.REQUEST_CAPTURED
                | F.RESPONSE_QUEUED
                | F.ANSWER_ORDERED
                | F.TX_COMPLETED
                | F.REARMABLE
            )
            require(
                status.winner is Winner.REJECT
                and status.outcome is Outcome.INJECTED_REJECT
                and status.flags & required == required
                and cleanup_ready(status),
                "REJECT retirement/zero",
            )


def validate_boundary(document: dict[str, Any], control: Control, name: str) -> Status:
    require(
        document.get("schema_version") == 2
        and document.get("mode") == MODE
        and document.get("control") == asdict(control)
        and document.get("session_id") == control.session,
        "physical identity",
    )
    require(
        document.get("status") in ("running", "passed")
        and not document.get("primary_error")
        and not document.get("cleanup_errors"),
        "failed physical evidence",
    )
    rows = document.get("boundaries", [])
    matches = [r for r in rows if r.get("boundary") == name]
    armed = [r for r in rows if r.get("boundary") == "armed"]
    require(len(matches) == len(armed) == 1, "missing/duplicate physical boundary")
    original = status_from_document(armed[0]["status"])
    status = status_from_document(matches[0]["status"])
    require(
        original.ticket.owner_nonce == owner_nonce(control.session)
        and original.ticket.handle == document.get("target_handle"),
        "physical owner/handle",
    )
    validate_status(original, phase=Phase.ARMED)
    validate_status(
        status,
        phase=Phase.ARMED
        if name == "armed"
        else Phase.HELD
        if name == "held"
        else Phase.RETIRED,
        ticket=original.ticket,
        peer=original.peer,
    )
    if name in ("rejected", "recovery"):
        require(
            type(matches[0].get("local", {}).get("att_response")) is int
            and matches[0].get("local")
            == {
                "att_response": 0,
                "value": (RECOVERED if name == "recovery" else BEFORE).hex(),
            },
            "independent local readback",
        )
    return status


class WriteRejectRelay:
    """One ordered control exchange; .directory is the coordination root itself.

    pending() is idempotent until forwarded(ack). Capture must call forwarded
    immediately before its SDK relay, so an inline native/JS ACK is authorized.
    A failed SDK relay fails the attempt; it must not retry a consumed signal.
    """

    def __init__(self, root: Path) -> None:
        self.directory = self.root = root
        self.control = load_control(root / CONTROL_NAME)
        require(
            (self.control.flow, self.control.mode) == (10, RUNTIME_MODE),
            "relay control",
        )
        self.records: list[str] = []
        self.sent: list[str] = []
        self.seen: list[str] = []

    def observe(self, text: str) -> None:
        records = [
            line.split(PREFIX, 1)[1].strip()
            for line in text.splitlines()
            if PREFIX in line and line.split(PREFIX, 1)[1].startswith("rf-control:")
        ]
        expected = control_records(self.control)
        require(
            records == expected[: len(records)]
            and len(records) <= len(expected)
            and records[: len(self.records)] == self.records,
            "foreign/duplicate/early/dropped control record",
        )
        for index, phase in enumerate(PHASES):
            if self.control.marker("observed", phase) in records:
                require(
                    self.sent[: index + 1] == list(PHASES[: index + 1]),
                    "ACK before forwarding",
                )
        facts = verify_control_transcript(text, self.control)
        requests = [
            request
            for request in REQUESTS
            if self.control.marker("ready", request) in records
        ]
        for index in range(len(self.seen), len(requests)):
            require(index <= len(self.sent), "request before prior signal")
            needed = (
                "baseline-read",
                "write-dispatched",
                "queued-read-dispatched",
                "recovery-read",
            )[index]
            require(
                needed in facts["steps"], "request lacks App dispatch/readiness witness"
            )
        # Readiness is the publication barrier: a visible request must always
        # have its complete App witness available to the concurrent provider.
        temporary = self.root / "runtime.log.tmp"
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(self.root / "runtime.log")
        for request in requests[len(self.seen) :]:
            write_document(self.root / f"request-{request}.json", asdict(self.control))
        self.records, self.seen = records, requests

    def pending(self) -> str | None:
        require(
            load_control(self.root / CONTROL_NAME) == self.control, "control changed"
        )
        physical = self.root / PHYSICAL_NAME
        if physical.exists():
            document = read_live_document(physical)
            require(
                document.get("status") in ("running", "passed")
                and not document.get("primary_error")
                and not document.get("cleanup_errors"),
                "provider failed",
            )
        if len(self.sent) == len(PHASES):
            return None
        index = len(self.sent)
        phase = PHASES[index]
        path = self.root / f"signal-{phase}.json"
        if not path.exists():
            return None
        require(
            len(self.seen) > index and read_document(path) == asdict(self.control),
            "early/foreign provider signal",
        )
        document = read_live_document(physical)
        require(
            document.get("status") == "running",
            "completed attempt cannot authorize a new signal",
        )
        validate_boundary(document, self.control, BOUNDARIES[index])
        require(
            type(document.get("deadline")) in (int, float)
            and math.isfinite(document["deadline"]),
            "invalid signal deadline",
        )
        require(
            time.monotonic() < document["deadline"], "provider signal whole deadline"
        )
        if phase in ("armed", "held"):
            armed = next(r for r in document["boundaries"] if r["boundary"] == "armed")
            require(
                time.monotonic() < armed["reject_deadline"], "stale ARM/HELD signal"
            )
        return phase

    def forwarded(self, phase: str) -> None:
        require(
            phase in PHASES and self.pending() == phase and phase not in self.sent,
            "duplicate/out-of-order forwarding",
        )
        self.sent.append(phase)


def _limits(limits) -> dict[str, Any]:
    return {
        "boot_nonce": limits.boot_nonce.hex(),
        "engine_flags": int(limits.engine_flags),
        "peer_present": limits.peer_present,
        "peer": limits.peer.hex(),
        "connection_epoch": limits.connection_epoch,
        "bearer_epoch": limits.bearer_epoch,
        "database_epoch": limits.database_epoch,
    }


def _base(root: Path, plan: FixtureSession) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "mode": MODE,
        "session_id": plan.session_id,
        "control": asdict(validate_control(root, plan)),
        "service_uuid": plan.service_uuid,
        "resource_identity": plan.resource_identity,
        "contract_sha256": plan.contract_sha256,
        "profile_sha256": plan.profile_sha256,
        "fixture_pid": os.getpid(),
        "fixture_pgid": os.getpgid(0),
        "status": "running",
        "primary_error": None,
        "cleanup_errors": [],
        "boundaries": [],
    }


def record_start_failure(
    root: Path, plan: FixtureSession, error: BaseException
) -> None:
    document = _base(root, plan)
    document.update(status="failed", primary_error=f"{type(error).__name__}: {error}")
    write_document(root / PHYSICAL_NAME, document)


class WriteReject:
    """Own scenario facts, never the fixture's transport or a foreign connection."""

    def __init__(self, fixture: Any, root: Path, handles: dict[str, int]) -> None:
        self.fixture, self.root = fixture, root
        self.plan = require_multifunction(fixture._fixture_session)
        self.control = validate_control(root, self.plan)
        self.document = _base(root, self.plan)
        self.target = handles["read-write"]
        self.rw = next(
            c for c in fixture._profile.characteristics if c.role == "read-write"
        )
        self.fault = FaultController(fixture._session, self.plan.session_id)
        self.ticket = self.peer = None
        self.peers: set[bytes] = set()
        self.started = time.monotonic()
        self.deadline = self.started + WHOLE_SECONDS
        self.reject_deadline = self.deadline
        self.runtime_prefix = ""
        self.document.update(
            target_handle=self.target,
            target_uuid=self.rw.uuid,
            permissions=tester_characteristic_native_permissions(self.rw.permissions),
            deadline=self.deadline,
        )

    def publish(self) -> None:
        write_document(self.root / PHYSICAL_NAME, self.document)

    def record(self, name: str, **fields: Any) -> None:
        require(len(self.document["boundaries"]) < 256, "physical evidence overflow")
        self.document["boundaries"].append(
            {"boundary": name, "monotonic": time.monotonic(), **fields}
        )
        self.publish()

    def guard(self, *, cleanup: bool = False) -> None:
        self.fixture._transport.require()
        require(self.fixture._mutation_allowed, "fixture has no mutation authority")
        verify_receipt(self.plan, fixture_identity=(os.getpid(), os.getpgid(0)))
        require(
            validate_control(self.root, self.plan) == self.control,
            "control generation changed",
        )
        if not cleanup:
            require(
                not self.fixture._stop_requested and not (self.root / "abort").exists(),
                "parent stopped/aborted",
            )
            require(time.monotonic() < self.deadline, "whole deadline")

    def drain(self) -> None:
        session = self.fixture._session
        for _ in range(128):
            event = session.pop_event() or session.poll_event(0.001)
            if event is None:
                return
            if event.service == 1 and event.opcode in (
                GAP_DEVICE_CONNECTED,
                GAP_DEVICE_DISCONNECTED,
            ):
                require(
                    event.index == 0
                    and type(event.payload) is bytes
                    and len(event.payload)
                    == (13 if event.opcode == GAP_DEVICE_CONNECTED else 7)
                    and event.payload[0] in (0, 1, 2, 3),
                    "malformed GAP event",
                )
                peer = event.payload[:7]
                if event.opcode == GAP_DEVICE_CONNECTED:
                    require(
                        not self.peers and self.peer is None,
                        "duplicate/new GAP connection",
                    )
                    self.peers.add(peer)
                    self.peer = peer
                else:
                    require(peer in self.peers, "foreign/duplicate GAP detach")
                    self.peers.remove(peer)
                self.fixture._observe_gap_event(event)
                self.record("gap", opcode=event.opcode, peer=peer.hex())
        raise ValueError("write-reject: GAP drain overflow")

    def pause(self, *, cleanup: bool = False) -> None:
        self.guard(cleanup=cleanup)
        time.sleep(0.02)

    def snapshot(self) -> tuple[str, dict[str, Any]]:
        path = self.root / "runtime.log"
        if path.exists():
            require(not path.is_symlink(), "linked runtime snapshot")
            with path.open("rb") as stream:
                raw = stream.read(MAX_RUNTIME_BYTES + 1)
            require(len(raw) <= MAX_RUNTIME_BYTES, "runtime snapshot exceeds budget")
            text = raw.decode("utf-8")
        else:
            text = ""
        require(text.startswith(self.runtime_prefix), "runtime prefix changed")
        facts = verify_control_transcript(text, self.control)
        self.runtime_prefix = text
        return text, facts

    def wait_request(self, request: str, deadline: float) -> dict[str, Any]:
        path = self.root / f"request-{request}.json"
        while True:
            self.guard()
            self.drain()
            require(time.monotonic() < deadline, f"{request} deadline")
            _, facts = self.snapshot()
            if path.exists():
                # The request can appear after the loop's earlier snapshot.
                # Re-read after observing it; do not validate against old facts.
                _, facts = self.snapshot()
                require(read_document(path) == asdict(self.control), "foreign request")
                index = REQUESTS.index(request)
                needed = (
                    "baseline-read",
                    "write-dispatched",
                    "queued-read-dispatched",
                    "recovery-read",
                )[index]
                require(needed in facts["steps"], "request without App readiness")
                marker = self.control.marker("ready", request)
                require(
                    self.runtime_prefix.count(PREFIX + marker) == 1,
                    "request without unique control marker",
                )
                return facts
            self.pause()

    def signal(self, phase: str) -> None:
        validate_boundary(self.document, self.control, BOUNDARIES[PHASES.index(phase)])
        self.record("signal", phase=phase)
        write_document(self.root / f"signal-{phase}.json", asdict(self.control))

    def status(self) -> Status:
        require(self.ticket is not None, "no owned ticket")
        self.guard()
        status = self.fault.status(self.ticket)
        self.guard()
        self.drain()
        return status

    def local(self, expected: bytes) -> dict[str, Any]:
        self.guard()
        result = self.fixture._gatt.get_attribute_value(self.target)
        self.guard()
        require(
            result.att_response == 0 and result.value == expected,
            "local ATT/readback mismatch",
        )
        return {"att_response": result.att_response, "value": result.value.hex()}

    def retired(
        self,
        deadline: float,
        *,
        live: bool = True,
        resolution_deadline: float | None = None,
    ) -> Status:
        while True:
            status = self.status()
            require(time.monotonic() < deadline, "original retirement deadline")
            require(
                not live or self.peers == {self.peer}, "peer changed before recovery"
            )
            # A retired receipt may still count ordinary UART evidence. No other
            # owner or infrastructure failure can be waited into success.
            if status.phase is Phase.RETIRED:
                settled = Status(
                    **{**status.__dict__, "refs": RefCounters(0, 0, 0, 0, 0, 0)}
                )
                validate_status(
                    settled, phase=Phase.RETIRED, ticket=self.ticket, peer=self.peer
                )
                require(
                    status.refs.conn_refs
                    == status.refs.bearer_pins
                    == status.refs.response_buffers
                    == status.refs.resolver_work
                    == status.refs.tx_pending
                    == 0,
                    "retired fault refs remain",
                )
                if status.refs.evidence_pending == 0:
                    return status
            else:
                require(
                    resolution_deadline is not None, "retired owner became active again"
                )
                if status.phase is Phase.RESOLUTION_QUEUED:
                    require(
                        time.monotonic() < resolution_deadline,
                        "original resolution deadline",
                    )
                require(
                    status.phase in (Phase.RESOLUTION_QUEUED, Phase.RETIRING)
                    and status.winner is Winner.REJECT
                    and status.outcome is Outcome.INJECTED_REJECT
                    and status.infrastructure_error is InfrastructureError.NONE
                    and not status.flags
                    & (
                        F.CALLBACK_INVOKED | F.COMMIT_OBSERVED | F.INFRASTRUCTURE_FAILED
                    ),
                    "unexpected resolution owner/outcome",
                )
            self.pause()

    def execute(self) -> None:
        self.guard()
        initial = self.fault.require_idle()
        require(not self.fixture._connected_peers, "startup has unowned tracked peer")
        require(not initial.peer_present, "startup has unowned peer")
        self.drain()
        require(not self.peers, "startup GAP peer")
        self.record("no-peer", limits=_limits(initial))
        facts = self.wait_request("arm", self.deadline)
        validate_target(facts, self.plan.service_uuid, self.rw.uuid)
        require(
            self.peers == {self.peer} and self.peer is not None,
            "ARM needs one discovered peer",
        )
        attrs = self.fixture._gatt.get_attributes(attribute_uuid=self.rw.uuid)
        require(
            len(attrs) == 1
            and attrs[0].handle == self.target
            and attrs[0].uuid == self.rw.uuid
            and attrs[0].permissions == tester_characteristic_native_permissions(3)
            and self.rw.permissions == 3
            and self.rw.initial_value == BEFORE,
            "target handle/UUID/permissions",
        )
        local = self.local(BEFORE)
        limits = self.fault.require_idle()
        require(
            limits.boot_nonce == initial.boot_nonce
            and limits.peer_present
            and limits.peer == self.peer,
            "live ARM route/boot",
        )
        self.reject_deadline = time.monotonic() + REJECT_SECONDS
        common = ArmCommon(
            limits.boot_nonce,
            owner_nonce(self.plan.session_id),
            limits.connection_epoch,
            limits.bearer_epoch,
            limits.database_epoch,
            self.peer,
            self.target,
            min(ARM_MS, limits.arm_max_ms),
            min(HOLD_MS, limits.hold_max_ms),
        )
        request = ArmRequest(Operation.RESPONSE_WRITE, common, BEFORE, REQUESTED)
        self.guard()
        self.ticket = self.fault.arm(request).ticket
        status = self.status()
        validate_status(status, phase=Phase.ARMED, ticket=self.ticket, peer=self.peer)
        self.record(
            "armed",
            status=status_document(status),
            local=local,
            limits=_limits(limits),
            arm_request_wire=request.encode().hex(),
            reject_deadline=self.reject_deadline,
        )
        self.signal("armed")
        self.wait_request("write-dispatched", self.reject_deadline)
        while True:
            status = self.status()
            require(
                time.monotonic() < self.reject_deadline and self.peers == {self.peer},
                "HELD capture deadline/peer",
            )
            if status.phase is Phase.HELD:
                break
            validate_status(
                status, phase=Phase.ARMED, ticket=self.ticket, peer=self.peer
            )
            self.pause()
        validate_status(status, phase=Phase.HELD, ticket=self.ticket, peer=self.peer)
        self.record("held", status=status_document(status))
        self.signal("held")
        self.wait_request("reject", self.reject_deadline)
        status = self.status()
        validate_status(status, phase=Phase.HELD, ticket=self.ticket, peer=self.peer)
        require(
            time.monotonic() < self.reject_deadline and self.peers == {self.peer},
            "REJECT deadline/peer",
        )
        self.record("still-held", status=status_document(status))
        self.guard()
        submitted_at = time.monotonic()
        retirement_deadline = submitted_at + 2 + RETIRE_SECONDS
        ack = self.fault.resolve(self.ticket, Decision.REJECT)
        self.record(
            "resolve-dispatched",
            monotonic=submitted_at,
            decision=int(Decision.REJECT),
            ticket_wire=ack.ticket.encode().hex(),
            retirement_deadline=retirement_deadline,
        )
        status = self.retired(retirement_deadline, resolution_deadline=submitted_at + 2)
        self.record(
            "rejected", status=status_document(status), local=self.local(BEFORE)
        )
        self.signal("rejected")
        facts = self.wait_request("recovery-done", self.deadline)
        require(
            "write-rejected" in facts["steps"] and "unchanged-read" in facts["steps"],
            "recovery missing rejected write/queued read",
        )
        status = self.retired(min(self.deadline, time.monotonic() + RETIRE_SECONDS))
        self.record(
            "recovery", status=status_document(status), local=self.local(RECOVERED)
        )
        self.signal("close-allowed")
        while True:
            self.guard()
            self.drain()
            text, facts = self.snapshot()
            if facts["passed"] and not self.peers:
                runtime_facts(text, run_id=self.plan.session_id, complete=True)
                break
            self.pause()
        self.fixture._gap.stop_advertising()
        status = self.retired(
            min(self.deadline, time.monotonic() + RETIRE_SECONDS), live=False
        )
        limits = self.fault.require_idle()
        require(
            not limits.peer_present
            and limits.boot_nonce == self.ticket.boot_nonce
            and not self.peers,
            "final peer/boot/idle",
        )
        self.record(
            "closed",
            status=status_document(status),
            limits=_limits(limits),
            peer_count=0,
            resources=facts["resources"],
            runtime_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )

    def cleanup(self) -> None:
        self.guard(cleanup=True)
        self.drain()
        if self.ticket is not None:
            status = self.fault.status(self.ticket)
            require(
                status.ticket == self.ticket and status.peer == self.peer,
                "cleanup foreign ticket/peer",
            )
            if status.phase in (Phase.ARMED, Phase.HELD):
                limits = self.fault.get_limits()
                require(
                    limits.boot_nonce == self.ticket.boot_nonce
                    and limits.peer_present
                    and limits.peer == self.peer
                    and limits.connection_epoch == self.ticket.connection_epoch
                    and limits.bearer_epoch == self.ticket.bearer_epoch
                    and limits.database_epoch == self.ticket.database_epoch
                    and self.peers == {self.peer},
                    "cleanup lacks owned active route",
                )
                self.guard(cleanup=True)
                self.fault.abort(self.ticket)
        for peer in tuple(self.peers):
            self.guard(cleanup=True)
            require(peer == self.peer, "cleanup foreign peer")
            self.fixture._gap.disconnect(peer[1:][::-1].hex(), peer[0])
        deadline = time.monotonic() + RETIRE_SECONDS
        while True:
            self.guard(cleanup=True)
            self.drain()
            require(time.monotonic() < deadline, "cleanup drain deadline")
            status = self.fault.status(self.ticket) if self.ticket else None
            if not self.peers and (status is None or cleanup_ready(status)):
                break
            self.pause(cleanup=True)
        limits = self.fault.require_idle()
        require(not limits.peer_present, "cleanup live peer")
        self.fixture._gap.stop_advertising()
        self.record(
            "cleanup",
            peer_count=0,
            status=status_document(status) if status else None,
            limits=_limits(limits),
        )

    def run(self) -> None:
        try:
            self.publish()
            self.execute()
            self.document["status"] = "passed"
            self.publish()
        except BaseException as error:
            self.document.update(
                status="failed", primary_error=f"{type(error).__name__}: {error}"
            )
            try:
                self.publish()
            except BaseException as publication:  # noqa: BLE001 - retain the primary even during interrupted publication
                error.add_note(f"failed evidence publication: {publication}")
            try:
                self.cleanup()
            except BaseException as cleanup:  # noqa: BLE001 - cleanup must not replace the scenario failure
                self.document["cleanup_errors"].append(
                    f"{type(cleanup).__name__}: {cleanup}"
                )
                error.add_note(f"write-reject cleanup: {cleanup}")
            try:
                self.publish()
            except BaseException as publication:  # noqa: BLE001 - preserve primary plus cleanup errors
                error.add_note(f"final evidence publication: {publication}")
            raise


def record_failure(root: Path, error: BaseException, *, cleanup: bool = False) -> None:
    """Preserve scenario failure if the existing fixture owner fails after run()."""
    path = root / PHYSICAL_NAME
    document = read_document(path)
    document["status"] = "failed"
    if cleanup:
        document["cleanup_errors"].append(f"{type(error).__name__}: {error}")
    elif not document.get("primary_error"):
        document["primary_error"] = f"{type(error).__name__}: {error}"
    write_document(path, document)


def run(fixture: Any, root: Path, handles: dict[str, int]) -> None:
    try:
        owner = WriteReject(fixture, root, handles)
    except BaseException as error:
        try:
            record_start_failure(root, fixture._fixture_session, error)
        except BaseException as publication:  # noqa: BLE001 - construction/publication errors remain distinct
            error.add_note(f"write-reject startup publication: {publication}")
        raise
    owner.run()
