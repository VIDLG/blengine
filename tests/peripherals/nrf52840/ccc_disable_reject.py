"""Opt-in CCC disable REJECT; original enabled subscription, one leased session.

Eight barriers distinguish SET_VALUE submission from actual ef/e1 App delivery.
Enabled per-peer CCC reads must succeed with 0100; getter errors are not evidence.
The rejected ticket retains before/after 0100 even after the normal retry disables.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import ccc_common as common
from .cancellation_control import (
    Control,
    load_control,
    read_document,
    require_multifunction,
    write_document,
)
from .ccc_common import resolve_target
from .fault_control import (
    ArmCommon,
    ArmRequest,
    Decision,
    Opcode,
    Operation,
    Phase,
    Status,
    owner_nonce,
    status_document,
    status_from_document,
)
from .fixture_session import FixtureSession, _json
from .gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED
from .gatt_commands import GattAttribute

FLOW = 12
HELD_REQUESTS = ("disable-dispatched", "reject")
SCENARIO = "ccc-disable-reject"
RUNTIME_MODE = "rf-ccc-disable-reject"
MODE = "ccc-disable-reject-attested"
CONTROL_NAME = "ccc-disable-reject-control.json"
PHYSICAL_NAME = "ccc-disable-reject-physical.json"
PREFIX = common.PREFIX
REQUESTS = (
    "baseline-enabled",
    "arm",
    "disable-dispatched",
    "reject",
    "reject-observed",
    "continued-notified",
    "disabled",
    "silence-done",
)
PHASES = (
    "baseline-value-sent",
    "armed",
    "held",
    "rejected",
    "continued-value-sent",
    "retry-allowed",
    "silence-started",
    "close-allowed",
)
BOUNDARIES = PHASES
READY_STEPS = (
    "baseline-enabled",
    "baseline-notified",
    "disable-dispatched",
    "queued-read-dispatched",
    "queued-read",
    "continued-notified",
    "post-disable-read",
    "end-silence-read",
)
NEXT_STEPS = (
    "baseline-notified",
    "disable-dispatched",
    "queued-read-dispatched",
    "disable-rejected",
    "continued-notified",
    "disabled",
    "silence",
    "disconnected",
)
STEPS = (
    "initialize",
    "scan",
    "connected",
    "gatt",
    "baseline-read",
    "baseline-enabled",
    "baseline-notified",
    "disable-dispatched",
    "queued-read-dispatched",
    "disable-rejected",
    "queued-read",
    "continued-notified",
    "disabled",
    "post-disable-read",
    "silence",
    "end-silence-read",
    "disconnected",
    "close",
)
COMMANDS = {
    "initialize": "34000",
    "scan-start": "34002",
    "scan": "34003",
    "connected": "34005",
    "disconnected": "34006",
    "gatt": "34008",
    "baseline-enabled": "34010",
    "disable-dispatched": "34011",
    "disable-rejected": "34011",
    "queued-read-dispatched": "34012",
    "queued-read": "34012",
    "disabled": "34013",
    "post-disable-read": "34014",
    "end-silence-read": "34015",
    "baseline-read": "34016",
}
READ_COMMANDS = {"34012", "34014", "34015", "34016"}
SUBSCRIPTIONS = {"34010": "34009", "34011": "34009", "34013": "34009"}
RESOURCE_FIELDS = common.RESOURCE_FIELDS
UPDATES_UUID, READ_UUID = common.UPDATES_UUID, common.READ_UUID
BEFORE, REQUESTED = bytes.fromhex("0100"), bytes.fromhex("0000")
ARM_MS, HOLD_MS = common.ARM_MS, common.HOLD_MS
REJECT_SECONDS, RETIRE_SECONDS, WHOLE_SECONDS = (
    common.REJECT_SECONDS,
    common.RETIRE_SECONDS,
    common.WHOLE_SECONDS,
)
MAX_RUNTIME_BYTES, REJECT_FLAGS = common.MAX_RUNTIME_BYTES, common.REJECT_FLAGS
TOPOLOGY_METHOD = common.TOPOLOGY_METHOD


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"{SCENARIO}: {message}")


def prepare_control(root: Path, plan: FixtureSession) -> Control:
    require_multifunction(plan)
    control = Control(plan.session_id, 12, RUNTIME_MODE, str(uuid.uuid4()))
    require(not (root / CONTROL_NAME).exists(), "refusing existing control")
    write_document(root / CONTROL_NAME, asdict(control))
    return control


def validate_control(root: Path, plan: FixtureSession | None) -> Control:
    plan = require_multifunction(plan)
    control = load_control(root / CONTROL_NAME)
    require(
        (control.flow, control.mode, control.session)
        == (12, RUNTIME_MODE, plan.session_id),
        "foreign control/session",
    )
    return control


def _fields(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in raw.split(":"):
        key, separator, value = part.partition("=")
        require(
            bool(key and separator and value) and key not in fields,
            "malformed/duplicate fields",
        )
        fields[key] = value
    return fields


def _id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", value))
        and int(value) < 1 << 64
    )


def _bytes(event: dict[str, Any], value: int) -> None:
    require(
        event.get("value") == [value] and all(type(v) is int for v in event["value"]),
        "wrong value bytes",
    )
    require(
        (event.get("valueTruncated") is None or event.get("valueTruncated") is False)
        and type(event.get("valueByteLength", 1)) is int
        and event.get("valueByteLength", 1) == 1,
        "truncated value",
    )


def _resources(value: Any, *, closed: bool) -> None:
    require(
        isinstance(value, dict)
        and set(value) == set(RESOURCE_FIELDS)
        and all(type(v) is int and 0 <= v <= 65535 for v in value.values()),
        "resource schema",
    )
    if closed:
        require(not any(value.values()), "closed resources are not seventeen zeros")
    else:
        require(
            value["connections"] == 1
            and value["subscriptions"] == 1
            and value["gattAttributes"] > 0
            and all(
                value[k] == 0
                for k in (
                    "activeScans",
                    "pendingIo",
                    "watchdogs",
                    "handoffPending",
                )
            ),
            "failed-disable resources are not warm/idle",
        )


def runtime_facts(
    text: str, *, run_id: str, platform: str | None = None, complete: bool = False
) -> dict[str, Any]:
    """Validate a bounded immutable-prefix App transcript, including inline value races.

    Values may race their host ACK, but only after their matching readiness:
    baseline-enabled for ef, reject-observed for e1. No re-enable or Disabled domain
    is admitted for the rejected disable. App and host silence fences are separate.
    """
    require(
        len(text.encode("utf-8")) <= MAX_RUNTIME_BYTES
        and platform in (None, "android", "ios"),
        "runtime budget/platform",
    )
    nonce = None
    closed = passed = False
    events: list[dict[str, Any]] = []
    records: list[str] = []
    steps: dict[str, dict[str, str]] = {}
    positions: dict[str, int] = {}
    terminals: dict[str, Any] = {}
    domains: set[tuple[str, str]] = set()
    ready: set[str] = set()
    observed: set[str] = set()
    resources = warm_resources = None
    characteristic = read_characteristic = None
    notifications: set[str] = set()
    control = None
    controls: list[str] = []
    expected_domains = {
        "34002": "scanStarted",
        "34003": "scanStopped",
        "34005": "connectionStateChanged",
        "34006": "connectionStateChanged",
        "34008": "gattDiscovered",
        **dict.fromkeys(READ_COMMANDS, "characteristicRead"),
        "34010": "notificationStateChanged",
        "34013": "notificationStateChanged",
    }
    prerequisites = {
        "34000": None,
        "34002": "initialize",
        "34003": "initialize",
        "34005": "scan",
        "34008": "connected",
        "34016": "gatt",
        "34011": "queued-read-dispatched",
        "34012": "queued-read-dispatched",
        "34010": "baseline-read",
        "34013": "continued-notified",
        "34014": "disabled",
        "34015": "silence",
        "34006": "end-silence-read",
    }
    for number, line in enumerate(text.splitlines(), 1):
        if PREFIX not in line:
            continue
        body = line.split(PREFIX, 1)[1].strip()
        if body.startswith("rf-control:"):
            require(nonce is not None and not closed, "control outside runtime")
            fields = _fields(body.split(":", 2)[2])
            require(
                set(fields) == {"session", "flow", "mode", "token", "phase"}
                and fields.get("session") == run_id
                and fields.get("flow") == "12"
                and fields.get("mode") == RUNTIME_MODE,
                "foreign control route",
            )
            if control is None:
                control = Control(run_id, 12, RUNTIME_MODE, fields["token"])
            expected_controls = control_records(control)
            require(
                len(controls) < len(expected_controls)
                and body == expected_controls[len(controls)],
                "foreign/duplicate/out-of-order control",
            )
            controls.append(body)
            kind = body.split(":", 2)[1]
            if kind == "ready":
                require(
                    fields.get("phase") in REQUESTS
                    and READY_STEPS[REQUESTS.index(fields["phase"])] in steps,
                    "readiness before App witness",
                )
                if fields["phase"] == "reject-observed":
                    require(
                        warm_resources is not None,
                        "missing failed-disable warm snapshot",
                    )
                ready.add(fields["phase"])
            elif kind == "observed":
                require(fields.get("phase") in PHASES, "unknown ACK")
                observed.add(fields["phase"])
            else:
                require(
                    kind == "listener" and fields.get("phase") == "registered",
                    "failed/unknown control",
                )
            continue
        if not body.startswith(RUNTIME_MODE + ":"):
            continue
        require(not passed, "record after passed")
        records.append(body)
        raw = body[len(RUNTIME_MODE) + 1 :]
        if raw.startswith("runtime:"):
            fields = _fields(raw[8:])
            require(
                set(fields) == {"session", "nonce", "phase"}
                and fields["session"] == run_id
                and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,160}", fields["nonce"])),
                "runtime identity",
            )
            if fields["phase"] == "start":
                require(nonce is None and len(records) == 1, "restarted runtime")
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
            kind, command = event.get("kind"), event.get("commandId")
            require(command is None or isinstance(command, str), "command ID type")
            require(
                event.get("runId") == run_id
                and event.get("nonce") == nonce
                and isinstance(kind, str),
                "event identity",
            )
            for field, value in (
                ("connectionId", "34004"),
                ("gattSessionId", "34007"),
                ("scanSessionId", "34001"),
            ):
                require(event.get(field) in (None, value), "foreign optional route")
            if event.get("characteristicId") is not None:
                require(
                    event["characteristicId"]
                    == (
                        read_characteristic
                        if command in READ_COMMANDS
                        else characteristic
                    ),
                    "foreign characteristic",
                )
            if event.get("subscriptionId") is not None:
                require(
                    event["subscriptionId"] == SUBSCRIPTIONS.get(command, "34009"),
                    "foreign subscription",
                )
            if kind == "notificationReceived":
                stage = "continued" if "reject-observed" in ready else "baseline"
                require(
                    command is None
                    and "baseline-enabled" in ready
                    and "34010" in terminals
                    and "34013" not in terminals
                    and "disabled" not in steps
                    and stage not in notifications
                    and not event.get("error")
                    and (
                        (stage == "baseline" and "arm" not in ready)
                        or (
                            stage == "continued"
                            and "34011" in terminals
                            and warm_resources is not None
                        )
                    ),
                    "early/late/duplicate notification",
                )
                require(
                    event.get("connectionId") == "34004"
                    and event.get("gattSessionId") == "34007"
                    and event.get("characteristicId") == characteristic
                    and event.get("subscriptionId") == "34009",
                    "notification route",
                )
                _bytes(event, 0xEF if stage == "baseline" else 0xE1)
                notifications.add(stage)
            elif kind == "advertisementObserved":
                require(
                    command is None
                    and event.get("scanSessionId") == "34001"
                    and "34002" in terminals
                    and "34003" not in terminals,
                    "advertisement route/order",
                )
            else:
                require(
                    command in prerequisites
                    and (
                        prerequisites[command] is None
                        or prerequisites[command] in steps
                    ),
                    "event before prerequisite/unknown command",
                )
                if command == "34011":
                    require(
                        "reject" in ready and kind == "commandCompleted",
                        "rejected disable has success domain/early terminal",
                    )
                if command == "34012":
                    require("34011" in terminals, "recovery before failure")
                if command == "34013":
                    require("retry-allowed" in observed, "disable before host ACK")
                if command == "34006":
                    require("close-allowed" in observed, "disconnect before host ACK")
                if kind == "commandCompleted":
                    require(command not in terminals, "duplicate terminal")
                    error = event.get("error")
                    if command == "34011":
                        require(
                            isinstance(error, dict) and error.get("category") == "gatt",
                            "rejected disable category",
                        )
                        native = error.get("platformDetails")
                        expected = {
                            "android": ("platform_android", "BluetoothGatt"),
                            "ios": ("platform_apple", "CBATTErrorDomain"),
                        }
                        require(
                            isinstance(native, dict)
                            and native.get("code") == "14"
                            and (native.get("platform"), native.get("name"))
                            in (
                                [expected[platform]] if platform else expected.values()
                            ),
                            "native error is not ATT 14",
                        )
                    else:
                        require(error is None, "unexpected terminal error")
                        domain = (
                            "adapterSnapshotChanged"
                            if command == "34000"
                            else expected_domains[command]
                        )
                        state = (
                            "Connected"
                            if command == "34005"
                            else "Disconnected"
                            if command == "34006"
                            else ""
                        )
                        require(
                            (command, domain + ":" + state) in domains,
                            "terminal before domain",
                        )
                    terminals[command] = event
                else:
                    require(
                        command not in terminals and not event.get("error"),
                        "late/failed domain",
                    )
                    expected = (
                        "adapterSnapshotChanged"
                        if command == "34000"
                        else expected_domains.get(command)
                    )
                    require(kind == expected, "unexpected domain")
                    state = event.get("connectionState", "")
                    require(isinstance(state, str), "connection state type")
                    require(
                        (command, kind + ":" + state) not in domains, "duplicate domain"
                    )
                    if kind == "adapterSnapshotChanged":
                        require(
                            event.get("adapterState") == "PoweredOn"
                            and event.get("scanAuthorization") == "Authorized",
                            "adapter not ready",
                        )
                    elif command in ("34002", "34003"):
                        require(event.get("scanSessionId") == "34001", "scan route")
                    else:
                        require(
                            event.get("connectionId") == "34004", "connection route"
                        )
                        if command in ("34005", "34006"):
                            allowed = (
                                ("Connecting", "Connected")
                                if command == "34005"
                                else ("Disconnecting", "Disconnected")
                            )
                            require(
                                state in allowed
                                and (command, kind + ":" + allowed[-1]) not in domains,
                                "connection state order",
                            )
                            if state == "Connected":
                                require(
                                    (command, kind + ":Connecting") in domains,
                                    "missing Connecting",
                                )
                        else:
                            require(event.get("gattSessionId") == "34007", "GATT route")
                    if command in READ_COMMANDS:
                        require(
                            event.get("characteristicId") == read_characteristic,
                            "read route",
                        )
                        _bytes(event, 0)

                    if command in ("34010", "34013"):
                        require(
                            event.get("characteristicId") == characteristic
                            and event.get("subscriptionId") == "34009",
                            "state route",
                        )
                        require(
                            event.get("notificationState")
                            == ("Enabled" if command == "34010" else "Disabled"),
                            "notification state",
                        )
                        if command == "34010":
                            require(
                                event.get("actualNotificationMode") == "Notification",
                                "not Notification mode",
                            )
                    domains.add((command, kind + ":" + state))
            events.append(event)
        elif raw.startswith("step:"):
            match = re.fullmatch(r"step:([^:]+):passed(?::(.*))?", raw)
            require(
                match is not None and nonce is not None and not closed,
                "failed/malformed step",
            )
            name = match[1]
            require(
                len(steps) < len(STEPS) and name == STEPS[len(steps)],
                "unknown/duplicate/out-of-order step",
            )
            fields = _fields(match[2]) if match[2] else {}
            if name in COMMANDS:
                require(fields.get("commandId") == COMMANDS[name], "step command")
                if name not in ("disable-dispatched", "queued-read-dispatched"):
                    require(COMMANDS[name] in terminals, "step before terminal")
            if name in ("disable-dispatched", "queued-read-dispatched"):
                require(
                    "34011" not in terminals and "34012" not in terminals,
                    "dispatch after terminal",
                )
                require(
                    ("armed" if name == "disable-dispatched" else "held") in observed,
                    "dispatch before host ACK",
                )
            if name not in ("initialize", "scan", "close"):
                require(fields.get("connectionId") == "34004", "step connection")
            if (
                STEPS.index("gatt")
                <= STEPS.index(name)
                <= STEPS.index("end-silence-read")
            ):
                require(fields.get("gattSessionId") == "34007", "step GATT")
                if name == "gatt":
                    characteristic, read_characteristic = (
                        fields.get("characteristicId"),
                        fields.get("readCharacteristicId"),
                    )
                    require(
                        _id(characteristic)
                        and _id(read_characteristic)
                        and characteristic != read_characteristic,
                        "distinct characteristic IDs",
                    )
                read_step = name in (
                    "baseline-read",
                    "queued-read-dispatched",
                    "queued-read",
                    "post-disable-read",
                    "end-silence-read",
                )
                require(
                    fields.get("characteristicId")
                    == (read_characteristic if read_step else characteristic),
                    "step characteristic route",
                )
            if name in (
                "disable-dispatched",
                "disable-rejected",
                "baseline-enabled",
                "baseline-notified",
                "continued-notified",
                "disabled",
            ):
                require(
                    fields.get("subscriptionId") == ("34009"),
                    "step subscription",
                )
            if name == "baseline-enabled":
                require(fields.get("actualMode") == "Notification", "baseline mode")
            if name in ("baseline-notified", "continued-notified"):
                stage = "baseline" if name == "baseline-notified" else "continued"
                require(
                    stage in notifications
                    and (
                        "baseline-value-sent"
                        if stage == "baseline"
                        else "continued-value-sent"
                    )
                    in observed,
                    "notification step before receipt/ACK",
                )
            if name == "silence":
                require(
                    "silence-started" in observed
                    and bool(re.fullmatch(r"[0-9]{1,9}", fields.get("elapsedMs", "")))
                    and int(fields["elapsedMs"]) >= 1000,
                    "silence duration/ACK",
                )
            if name in NEXT_STEPS:
                require(
                    PHASES[NEXT_STEPS.index(name)] in observed, "step before host ACK"
                )
            steps[name], positions[name] = fields, number
        elif raw.startswith("resources:"):
            match = re.fullmatch(
                r"resources:phase=(failed-disable|closed):snapshot=(.*)", raw
            )
            require(
                match is not None and nonce is not None and not closed,
                "resource marker",
            )
            value = _json(match[2].encode())
            is_closed = match[1] == "closed"
            _resources(value, closed=is_closed)
            if is_closed:
                require(
                    resources is None and "disconnected" in steps,
                    "closed resources order",
                )
                resources = value
            else:
                require(
                    warm_resources is None
                    and "disable-rejected" in steps
                    and "continued-notified" not in steps
                    and "reject-observed" not in ready,
                    "warm resource order",
                )
                warm_resources = value
        elif raw == "passed":
            require(closed, "pass before runtime close")
            passed = True
        else:
            raise ValueError(f"{SCENARIO}: failed/unknown record: {raw}")
        if "close" in steps:
            require(resources is not None, "close step before resource snapshot")
    if complete:
        require(
            passed
            and closed
            and tuple(steps) == STEPS
            and warm_resources is not None
            and set(terminals) == set(COMMANDS.values())
            and control is not None
            and controls == control_records(control),
            "incomplete runtime",
        )
    return {
        "run_id": run_id,
        "nonce": nonce,
        "events": events,
        "steps": steps,
        "positions": positions,
        "resources": resources,
        "warm_resources": warm_resources,
        "closed": closed,
        "passed": passed,
        "records": records,
    }


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


def _transcript(text: str, control: Control) -> dict[str, Any]:
    facts = runtime_facts(text, run_id=control.session)
    controls = [
        (n, line.split(PREFIX, 1)[1].strip())
        for n, line in enumerate(text.splitlines(), 1)
        if PREFIX in line and line.split(PREFIX, 1)[1].startswith("rf-control:")
    ]
    records = [body for _, body in controls]
    expected = control_records(control)
    require(
        records == expected[: len(records)] and len(records) <= len(expected),
        "foreign/duplicate/early control transcript",
    )
    positions = {body: n for n, body in controls}
    for request, phase, before, after in zip(
        REQUESTS, PHASES, READY_STEPS, NEXT_STEPS, strict=True
    ):
        ready, observed = (
            control.marker("ready", request),
            control.marker("observed", phase),
        )
        if ready in positions:
            require(
                before in facts["positions"]
                and facts["positions"][before] < positions[ready],
                "request before App witness",
            )
        if after in facts["positions"]:
            require(
                observed in positions
                and positions[observed] < facts["positions"][after],
                "App action before ACK",
            )
    return facts


def validate_status(
    status: Status, phase: Phase, *, ticket=None, peer: bytes | None = None
) -> None:
    common.validate_status(
        status, phase, ticket=ticket, peer=peer, before=BEFORE, requested=REQUESTED
    )


def _one(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [row for row in rows if row.get("boundary") == name]
    require(len(matches) == 1, f"missing/duplicate {name} boundary")
    return matches[0]


def _base(root: Path, plan: FixtureSession) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "scenario": SCENARIO,
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


def record_failure(root: Path, error: BaseException, *, cleanup: bool = False) -> None:
    document = read_document(root / PHYSICAL_NAME)
    document["status"] = "failed"
    if cleanup:
        document["cleanup_errors"].append(f"{type(error).__name__}: {error}")
    elif not document.get("primary_error"):
        document["primary_error"] = f"{type(error).__name__}: {error}"
    write_document(root / PHYSICAL_NAME, document)


def run(fixture: Any, root: Path, handles: dict[str, int]) -> None:
    try:
        owner = CccDisableReject(fixture, root, handles)
    except BaseException as error:
        try:
            record_start_failure(root, fixture._fixture_session, error)
        except BaseException as publication:  # noqa: BLE001 - construction failure remains primary
            error.add_note(f"{SCENARIO} startup publication: {publication}")
        raise
    owner.run()


def _local_proof(
    local: Any, value: bytes, route: dict[str, Any] | None = None
) -> dict[str, Any]:
    common._same(local, {"att_response": 0, "value": value.hex()}, "per-peer CCC")
    before, after = local.get("limits_before"), local.get("limits_after")
    require(isinstance(before, dict) and before == after, "unstable CCC read route")
    common._same(after, before, "CCC limits-after types")
    require(
        set(before)
        == {
            "boot_nonce",
            "peer",
            "peer_present",
            "connection_epoch",
            "bearer_epoch",
            "database_epoch",
            "engine_flags",
        },
        "CCC route schema",
    )
    require(
        before["peer_present"] is True and before["engine_flags"] == 0,
        "CCC route not live/idle",
    )
    require(
        all(
            type(before[k]) is int and before[k] > 0
            for k in ("connection_epoch", "bearer_epoch", "database_epoch")
        )
        and type(before["engine_flags"]) is int,
        "CCC route epoch types",
    )
    require(
        isinstance(before["peer"], str)
        and isinstance(before["boot_nonce"], str)
        and local.get("peer") == before["peer"]
        and bool(re.fullmatch(r"[0-9a-f]{14}", before["peer"]))
        and bool(re.fullmatch(r"[0-9a-f]{32}", before["boot_nonce"])),
        "CCC peer/boot encoding",
    )
    if route is not None:
        require(before == route and after == route, "CCC read live ticket epochs")
    return before


def _topology(document: dict[str, Any]) -> None:
    require(
        document.get("topology_method") == TOPOLOGY_METHOD
        and "declarations" not in document,
        "disable requires inventory UUID interval proof",
    )
    resolved = common._resolve_uuid_topology(
        tuple(GattAttribute(**entry) for entry in document["attribute_inventory"]),
        document["uuid_handles"],
        {"updates": document["value_handle"], "read-write": document["read_handle"]},
        document["service_uuid"],
    )
    for key, value in resolved.items():
        require(
            json.dumps(document.get(key), sort_keys=True)
            == json.dumps(value, sort_keys=True),
            f"topology {key}",
        )


def validate_boundary(
    document: dict[str, Any], control: Control, name: str
) -> Status | None:
    require(name in (*PHASES, "closed"), "unknown boundary")
    common._same(
        document,
        {
            "schema_version": 2,
            "scenario": SCENARIO,
            "mode": MODE,
            "session_id": control.session,
        },
        "physical identity",
    )
    require(
        document.get("control") == asdict(control)
        and (control.flow, control.mode) == (FLOW, RUNTIME_MODE),
        "physical control",
    )
    require(
        document.get("status") in ("running", "passed")
        and document.get("primary_error") is None
        and document.get("cleanup_errors") == [],
        "failed physical evidence",
    )
    rows = document.get("boundaries")
    require(
        isinstance(rows, list)
        and len(rows) <= 128
        and all(isinstance(r, dict) for r in rows),
        "boundary budget/schema",
    )
    phases = [r["boundary"] for r in rows if r.get("boundary") in PHASES]
    require(phases == list(PHASES[: len(phases)]), "physical phase order")
    row = _one(rows, name)
    require(
        type(row.get("runtime_bytes")) is int
        and 0 < row["runtime_bytes"] <= MAX_RUNTIME_BYTES
        and bool(re.fullmatch(r"[0-9a-f]{64}", row.get("runtime_sha256", ""))),
        "runtime prefix proof",
    )
    _topology(document)
    if name == "baseline-value-sent":
        _local_proof(row.get("local"), BEFORE)
        require("status" not in row, "baseline cannot claim a fault ticket")
    else:
        armed = _one(rows, "armed")
        original = status_from_document(armed["status"])
        ticket = original.ticket
        validate_status(original, Phase.ARMED)
        require(
            ticket.owner_nonce == owner_nonce(control.session)
            and ticket.handle == document["target_handle"],
            "ARM owner/descriptor",
        )
        request = ArmRequest.decode(
            Opcode.ARM_CCC, bytes.fromhex(armed["arm_request_wire"])
        )
        require(
            request.operation is Operation.CCC_WRITE
            and request.before == BEFORE
            and request.requested == REQUESTED,
            "ARM CCC bytes",
        )
        c = request.common
        require(
            (
                c.boot_nonce,
                c.owner_nonce,
                c.connection_epoch,
                c.bearer_epoch,
                c.database_epoch,
                c.peer,
                c.handle,
            )
            == (
                ticket.boot_nonce,
                ticket.owner_nonce,
                ticket.connection_epoch,
                ticket.bearer_epoch,
                ticket.database_epoch,
                original.peer,
                ticket.handle,
            ),
            "ARM ticket/live epochs",
        )
        require(
            c.arm_budget_ms == ARM_MS and c.hold_budget_ms == HOLD_MS,
            "ARM bounded budgets",
        )
        current = status_from_document(row["status"])
        validate_status(
            current,
            Phase.ARMED
            if name == "armed"
            else Phase.HELD
            if name == "held"
            else Phase.RETIRED,
            ticket=ticket,
            peer=original.peer,
        )
        route = {
            "boot_nonce": ticket.boot_nonce.hex(),
            "peer": original.peer.hex(),
            "peer_present": True,
            "connection_epoch": ticket.connection_epoch,
            "bearer_epoch": ticket.bearer_epoch,
            "database_epoch": ticket.database_epoch,
            "engine_flags": 0,
        }
        baseline_route = route | {"database_epoch": ticket.database_epoch - 1}
        _local_proof(_one(rows, "baseline-value-sent")["local"], BEFORE, baseline_route)
        shifts = {
            "armed": 0,
            "rejected": 0,
            "continued-value-sent": 0,
            "retry-allowed": 1,
            "silence-started": 1,
            "close-allowed": 2,
        }
        if name in shifts:
            _local_proof(
                row.get("local"),
                REQUESTED if name in ("silence-started", "close-allowed") else BEFORE,
                route | {"database_epoch": ticket.database_epoch + shifts[name]},
            )
        if name not in ("armed", "held"):
            require(
                row["status"] == _one(rows, "rejected")["status"],
                "retained receipt changed",
            )
            common._same(
                _one(rows, "resolve-dispatched"),
                {"decision": 2, "ticket_wire": ticket.encode().hex()},
                "single REJECT",
            )
        if name == "closed":
            require(phases == list(PHASES), "close before eight barriers")
            _resources(row.get("resources"), closed=True)
            gaps = [r for r in rows if r.get("boundary") == "gap"]
            require(
                [(r.get("opcode"), r.get("peer")) for r in gaps]
                == [
                    (GAP_DEVICE_CONNECTED, original.peer.hex()),
                    (GAP_DEVICE_DISCONNECTED, original.peer.hex()),
                ],
                "exact GAP attach/detach",
            )
            common._same(
                row,
                {
                    "peer_count": 0,
                    "gap_attached": 1,
                    "gap_detached": 1,
                    "engine_flags": 0,
                },
                "physical close",
            )
            common._same(
                row.get("limits"),
                {
                    "boot_nonce": ticket.boot_nonce.hex(),
                    "peer_present": False,
                    "peer": bytes(7).hex(),
                    "engine_flags": 0,
                },
                "closed no-peer idle",
            )
        if name == "close-allowed":
            started = _one(rows, "silence-started").get("silence_started")
            require(
                type(started) in (int, float)
                and math.isfinite(started)
                and row["monotonic"] - started >= 1,
                "host silence duration",
            )
    submitted_values = {
        "baseline-value-sent": "ef",
        "continued-value-sent": "e1",
        "silence-started": "f0",
    }
    if name in submitted_values:
        require(
            row.get("submission")
            == {
                "handle": document["value_handle"],
                "value": submitted_values[name],
                "result": "accepted",
                "delivery": "not-proven",
            },
            "SET_VALUE submission is not delivery",
        )
    return None if name == "baseline-value-sent" else current


class CccDisableRejectRelay(common.CccRelay):
    def __init__(self, root: Path) -> None:
        super().__init__(root, sys.modules[__name__])


class CccDisableReject(common.CccOwner):
    """Scenario facts: rejected disable retains the original enabled subscription."""

    def __init__(self, fixture: Any, root: Path, handles: dict[str, int]) -> None:
        super().__init__(fixture, root, handles, sys.modules[__name__])

    def baseline(self, **fields: Any) -> None:
        self.record(
            "baseline-value-sent",
            runtime_bytes=len(self.prefix.encode()),
            runtime_sha256=hashlib.sha256(self.prefix.encode()).hexdigest(),
            **fields,
        )
        validate_boundary(self.document, self.control, "baseline-value-sent")
        write_document(
            self.root / "signal-baseline-value-sent.json", asdict(self.control)
        )

    def execute(self) -> None:
        self.guard()
        initial = self.fault.require_idle()
        require(
            not initial.peer_present and not self.fixture._connected_peers,
            "startup unowned peer",
        )
        self.boot = initial.boot_nonce
        self.drain()
        require(not self.peers, "startup GAP peer")
        self.record(
            "no-peer",
            limits=asdict(initial)
            | {"boot_nonce": initial.boot_nonce.hex(), "peer": initial.peer.hex()},
        )
        facts = self.wait("baseline-enabled")
        self.document.update(
            resolve_target(self.fixture._gatt, self.handles, self.plan.service_uuid)
        )
        profile = self.fixture._profile
        common.validate_native_target(facts, self.document, profile)
        local = self.local(BEFORE)
        self.database_epoch = self.live().database_epoch
        self.baseline(local=local, submission=self.emit(b"\xef"))
        self.wait("arm")
        local = self.local(BEFORE)
        limits = self.live()
        common_arm = ArmCommon(
            limits.boot_nonce,
            owner_nonce(self.plan.session_id),
            limits.connection_epoch,
            limits.bearer_epoch,
            limits.database_epoch,
            self.peer,
            self.document["target_handle"],
            ARM_MS,
            HOLD_MS,
        )
        request = ArmRequest(Operation.CCC_WRITE, common_arm, BEFORE, REQUESTED)
        self.reject_deadline = time.monotonic() + REJECT_SECONDS
        self.ticket = self.fault.arm(request).ticket
        status = self.status()
        validate_status(status, Phase.ARMED, ticket=self.ticket, peer=self.peer)
        self.boundary(
            "armed",
            status,
            local=local,
            arm_request_wire=request.encode().hex(),
            reject_deadline=self.reject_deadline,
        )
        self.wait("disable-dispatched")
        while True:
            status = self.status()
            require(
                time.monotonic() < self.reject_deadline and self.peers == {self.peer},
                "HELD deadline/peer",
            )
            if status.phase is Phase.HELD:
                break
            validate_status(status, Phase.ARMED, ticket=self.ticket, peer=self.peer)
            self.pause()
        validate_status(status, Phase.HELD, ticket=self.ticket, peer=self.peer)
        self.boundary("held", status)
        self.wait("reject")
        status = self.status()
        validate_status(status, Phase.HELD, ticket=self.ticket, peer=self.peer)
        require(
            time.monotonic() < self.reject_deadline and self.peers == {self.peer},
            "REJECT deadline/peer",
        )
        self.record("still-held", status=status_document(status))
        submitted = time.monotonic()
        ack = self.fault.resolve(self.ticket, Decision.REJECT)
        self.record(
            "resolve-dispatched",
            ticket_wire=ack.ticket.encode().hex(),
            decision=2,
            submitted_at=submitted,
        )
        self.retained = self.retired(submitted + 2 + RETIRE_SECONDS)
        self.boundary("rejected", self.retained, local=self.local(BEFORE))
        self.wait("reject-observed")
        local = self.local(BEFORE)
        self.boundary(
            "continued-value-sent",
            self.retired(time.monotonic() + RETIRE_SECONDS),
            local=local,
            submission=self.emit(b"\xe1"),
        )
        self.wait("continued-notified")
        self.boundary(
            "retry-allowed",
            self.retired(time.monotonic() + RETIRE_SECONDS),
            local=self.local(BEFORE),
        )
        self.wait("disabled")
        local = self.local(REQUESTED)
        submission = self.emit(b"\xf0")
        silence_started = time.monotonic()
        self.boundary(
            "silence-started",
            self.retired(time.monotonic() + RETIRE_SECONDS),
            local=local,
            submission=submission,
            silence_started=silence_started,
        )
        self.wait("silence-done")
        require(
            time.monotonic() - silence_started >= 1,
            "host silence shorter than one second",
        )
        self.boundary(
            "close-allowed",
            self.retired(time.monotonic() + RETIRE_SECONDS),
            local=self.local(REQUESTED),
        )
        self.finish()

    def finish(self) -> None:
        while True:
            self.guard()
            self.drain()
            text, facts = self.snapshot()
            if facts["passed"] and not self.peers:
                runtime_facts(text, run_id=self.plan.session_id, complete=True)
                break
            self.pause()
        require(
            self.attached == self.detached == 1 and not self.fixture._connected_peers,
            "exact physical GAP pair",
        )
        self.fixture._gap.stop_advertising()
        status = self.retired(time.monotonic() + RETIRE_SECONDS)
        limits = self.fault.require_idle()
        require(
            not limits.peer_present and limits.boot_nonce == self.boot,
            "final no-peer idle boot",
        )
        self.record(
            "closed",
            status=status_document(status),
            peer_count=0,
            gap_attached=self.attached,
            gap_detached=self.detached,
            engine_flags=int(limits.engine_flags),
            resources=facts["resources"],
            limits={
                "boot_nonce": limits.boot_nonce.hex(),
                "peer_present": limits.peer_present,
                "peer": limits.peer.hex(),
                "engine_flags": int(limits.engine_flags),
            },
            runtime_sha256=hashlib.sha256(text.encode()).hexdigest(),
            runtime_bytes=len(text.encode()),
        )
        validate_boundary(self.document, self.control, "closed")
