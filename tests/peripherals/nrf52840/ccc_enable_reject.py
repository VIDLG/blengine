"""Provider-owned opt-in CCC enable REJECT; one leased session, no firmware changes.

The seven barriers distinguish notification submission from App receipt. Local CCC
reads only assert disabled state; enabled readback in the installed Tester has a
userdata/key-size defect. Recovery never replaces the rejected ticket.
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
from .ccc_common import _resolve_topology, _resolve_uuid_topology, resolve_target
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
from .fault_control import (
    StatusFlags as F,
)
from .fixture_session import FixtureSession, _json
from .gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED
from .gatt_commands import GattAttribute

uuid_to_le_bytes = common.uuid_to_le_bytes

FLOW = 11
HELD_REQUESTS = ("enable-dispatched", "reject")
SCENARIO = "ccc-enable-reject"
RUNTIME_MODE = "rf-ccc-enable-reject"
MODE = "ccc-enable-reject-attested"
CONTROL_NAME = "ccc-enable-reject-control.json"
PHYSICAL_NAME = "ccc-enable-reject-physical.json"
PREFIX = "BLEHUB_NITRO_RUNTIME "
REQUESTS = (
    "arm",
    "enable-dispatched",
    "reject",
    "recovery-enabled",
    "recovery-notified",
    "disabled",
    "silence-done",
)
PHASES = (
    "armed",
    "held",
    "rejected",
    "value-sent",
    "disable-allowed",
    "silence-started",
    "close-allowed",
)
BOUNDARIES = PHASES
READY_STEPS = (
    "baseline-read",
    "enable-dispatched",
    "queued-read-dispatched",
    "recovery-enabled",
    "recovery-notified",
    "post-disable-read",
    "end-silence-read",
)
NEXT_STEPS = (
    "enable-dispatched",
    "queued-read-dispatched",
    "enable-rejected",
    "recovery-notified",
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
    "enable-dispatched",
    "queued-read-dispatched",
    "enable-rejected",
    "queued-read",
    "recovery-enabled",
    "recovery-notified",
    "disabled",
    "post-disable-read",
    "silence",
    "end-silence-read",
    "disconnected",
    "close",
)
COMMANDS = {
    "initialize": "33000",
    "scan-start": "33002",
    "scan": "33003",
    "connected": "33005",
    "disconnected": "33006",
    "gatt": "33008",
    "enable-dispatched": "33010",
    "enable-rejected": "33010",
    "queued-read-dispatched": "33011",
    "queued-read": "33011",
    "recovery-enabled": "33013",
    "disabled": "33014",
    "post-disable-read": "33015",
    "end-silence-read": "33016",
    "baseline-read": "33017",
}
READ_COMMANDS = {"33011", "33015", "33016", "33017"}
SUBSCRIPTIONS = {"33010": "33009", "33013": "33012", "33014": "33012"}
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
UPDATES_UUID = "fff20000-0000-4000-8000-000000000000"
READ_UUID = "fff10000-0000-4000-8000-000000000000"
BEFORE, REQUESTED = bytes.fromhex("0000"), bytes.fromhex("0100")
ARM_MS, HOLD_MS = 7000, 9000
REJECT_SECONDS, RETIRE_SECONDS, WHOLE_SECONDS = 7.0, 5.0, 180.0
MAX_RUNTIME_BYTES = 8 * 1024 * 1024
REJECT_FLAGS = (
    F.REQUEST_CAPTURED
    | F.RESPONSE_QUEUED
    | F.TX_COMPLETED
    | F.REARMABLE
    | F.ANSWER_ORDERED
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"{SCENARIO}: {message}")


def prepare_control(root: Path, plan: FixtureSession) -> Control:
    require_multifunction(plan)
    control = Control(plan.session_id, 11, RUNTIME_MODE, str(uuid.uuid4()))
    require(not (root / CONTROL_NAME).exists(), "refusing existing control")
    write_document(root / CONTROL_NAME, asdict(control))
    return control


def validate_control(root: Path, plan: FixtureSession | None) -> Control:
    plan = require_multifunction(plan)
    control = load_control(root / CONTROL_NAME)
    require(
        (control.flow, control.mode, control.session)
        == (11, RUNTIME_MODE, plan.session_id),
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
        event.get("valueTruncated") in (None, False)
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
            and value["gattAttributes"] > 0
            and all(
                value[k] == 0
                for k in (
                    "activeScans",
                    "subscriptions",
                    "pendingIo",
                    "watchdogs",
                    "handoffPending",
                )
            ),
            "failed-enable resources are not warm/idle",
        )


def runtime_facts(
    text: str, *, run_id: str, platform: str | None = None, complete: bool = False
) -> dict[str, Any]:
    """Validate a bounded immutable-prefix App transcript, including inline value races.

    A retry value may precede value-sent ACK, but never the recovery-enabled ready
    marker. Rejected enable has no success domain and failedSub never receives a
    value. Silence is both an App duration witness and a separate host boundary.
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
    notified = False
    control = None
    controls: list[str] = []
    expected_domains = {
        "33002": "scanStarted",
        "33003": "scanStopped",
        "33005": "connectionStateChanged",
        "33006": "connectionStateChanged",
        "33008": "gattDiscovered",
        **dict.fromkeys(READ_COMMANDS, "characteristicRead"),
        "33013": "notificationStateChanged",
        "33014": "notificationStateChanged",
    }
    prerequisites = {
        "33000": None,
        "33002": "initialize",
        "33003": "initialize",
        "33005": "scan",
        "33008": "connected",
        "33017": "gatt",
        "33010": "queued-read-dispatched",
        "33011": "queued-read-dispatched",
        "33013": "queued-read",
        "33014": "recovery-notified",
        "33015": "disabled",
        "33016": "silence",
        "33006": "end-silence-read",
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
                and fields.get("flow") == "11"
                and fields.get("mode") == RUNTIME_MODE,
                "foreign control route",
            )
            if control is None:
                control = Control(run_id, 11, RUNTIME_MODE, fields["token"])
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
                ("connectionId", "33004"),
                ("gattSessionId", "33007"),
                ("scanSessionId", "33001"),
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
                    event["subscriptionId"] == SUBSCRIPTIONS.get(command, "33012"),
                    "foreign subscription",
                )
            if kind == "notificationReceived":
                require(
                    command is None
                    and "recovery-enabled" in ready
                    and "33013" in terminals
                    and "33014" not in terminals
                    and "disabled" not in steps
                    and not notified
                    and not event.get("error"),
                    "early/late/duplicate notification",
                )
                require(
                    event.get("connectionId") == "33004"
                    and event.get("gattSessionId") == "33007"
                    and event.get("characteristicId") == characteristic
                    and event.get("subscriptionId") == "33012",
                    "notification route",
                )
                _bytes(event, 0xEF)
                notified = True
            elif kind == "advertisementObserved":
                require(
                    command is None
                    and event.get("scanSessionId") == "33001"
                    and "33002" in terminals
                    and "33003" not in terminals,
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
                if command == "33010":
                    require(
                        "reject" in ready and kind == "commandCompleted",
                        "rejected enable has success domain/early terminal",
                    )
                if command in ("33011", "33013"):
                    require("33010" in terminals, "recovery before failure")
                if command == "33014":
                    require("disable-allowed" in observed, "disable before host ACK")
                if command == "33006":
                    require("close-allowed" in observed, "disconnect before host ACK")
                if kind == "commandCompleted":
                    require(command not in terminals, "duplicate terminal")
                    error = event.get("error")
                    if command == "33010":
                        require(
                            isinstance(error, dict) and error.get("category") == "gatt",
                            "rejected enable category",
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
                            if command == "33000"
                            else expected_domains[command]
                        )
                        state = (
                            "Connected"
                            if command == "33005"
                            else "Disconnected"
                            if command == "33006"
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
                        if command == "33000"
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
                    elif command in ("33002", "33003"):
                        require(event.get("scanSessionId") == "33001", "scan route")
                    else:
                        require(
                            event.get("connectionId") == "33004", "connection route"
                        )
                        if command in ("33005", "33006"):
                            allowed = (
                                ("Connecting", "Connected")
                                if command == "33005"
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
                            require(event.get("gattSessionId") == "33007", "GATT route")
                    if command in READ_COMMANDS:
                        require(
                            event.get("characteristicId") == read_characteristic,
                            "read route",
                        )
                        _bytes(event, 0)

                    if command in ("33013", "33014"):
                        require(
                            event.get("characteristicId") == characteristic
                            and event.get("subscriptionId") == "33012",
                            "state route",
                        )
                        require(
                            event.get("notificationState")
                            == ("Enabled" if command == "33013" else "Disabled"),
                            "notification state",
                        )
                        if command == "33013":
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
                if name not in ("enable-dispatched", "queued-read-dispatched"):
                    require(COMMANDS[name] in terminals, "step before terminal")
            if name in ("enable-dispatched", "queued-read-dispatched"):
                require(
                    "33010" not in terminals and "33011" not in terminals,
                    "dispatch after terminal",
                )
                require(
                    ("armed" if name == "enable-dispatched" else "held") in observed,
                    "dispatch before host ACK",
                )
            if name not in ("initialize", "scan", "close"):
                require(fields.get("connectionId") == "33004", "step connection")
            if (
                STEPS.index("gatt")
                <= STEPS.index(name)
                <= STEPS.index("end-silence-read")
            ):
                require(fields.get("gattSessionId") == "33007", "step GATT")
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
                "enable-dispatched",
                "enable-rejected",
                "recovery-enabled",
                "recovery-notified",
                "disabled",
            ):
                require(
                    fields.get("subscriptionId")
                    == (
                        "33009"
                        if name in ("enable-dispatched", "enable-rejected")
                        else "33012"
                    ),
                    "step subscription",
                )
            if name == "recovery-enabled":
                require(warm_resources is not None, "missing failed-enable resources")
                require(fields.get("actualMode") == "Notification", "recovery mode")
            if name == "recovery-notified":
                require(
                    notified and "value-sent" in observed,
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
                r"resources:phase=(failed-enable|closed):snapshot=(.*)", raw
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
                    and "enable-rejected" in steps
                    and "recovery-enabled" not in steps
                    and ("33013", "notificationStateChanged:") not in domains,
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


def validate_boundary(document: dict[str, Any], control: Control, name: str) -> Status:
    require(
        name in (*BOUNDARIES, "closed")
        and document.get("schema_version") == 2
        and document.get("scenario") == SCENARIO
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
    require(isinstance(rows, list) and len(rows) <= 128, "physical boundary budget")
    require(all(isinstance(row, dict) for row in rows), "malformed physical boundary")
    protocol = [row["boundary"] for row in rows if row.get("boundary") in BOUNDARIES]
    require(protocol == list(BOUNDARIES[: len(protocol)]), "physical protocol order")
    row, armed = _one(rows, name), _one(rows, "armed")
    require(
        type(row.get("runtime_bytes")) is int
        and 0 < row["runtime_bytes"] <= MAX_RUNTIME_BYTES
        and isinstance(row.get("runtime_sha256"), str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", row["runtime_sha256"])),
        "runtime prefix evidence",
    )
    require(
        document.get("target_uuid") == UPDATES_UUID
        and all(
            type(document.get(key)) is int and 0 < document[key] <= 65535
            for key in ("target_handle", "value_handle", "read_handle")
        )
        and len(
            {document[key] for key in ("target_handle", "value_handle", "read_handle")}
        )
        == 3,
        "physical target handles",
    )
    try:
        handles = {
            "updates": document["value_handle"],
            "read-write": document["read_handle"],
        }
        if "topology_method" in document:
            require(
                document["topology_method"] == TOPOLOGY_METHOD
                and "declarations" not in document,
                "unknown/mixed topology proof method",
            )
            inventory = tuple(
                GattAttribute(**entry) for entry in document["attribute_inventory"]
            )
            resolved = _resolve_uuid_topology(
                inventory, document["uuid_handles"], handles, document["service_uuid"]
            )
            for key, value in resolved.items():
                require(
                    json.dumps(document.get(key), sort_keys=True)
                    == json.dumps(value, sort_keys=True),
                    f"physical UUID topology {key}",
                )
        else:
            require(
                not any(
                    key in document
                    for key in (
                        "attribute_inventory",
                        "uuid_handles",
                        "service_binding",
                        "declaration_associations",
                    )
                ),
                "metadata topology requires its method discriminator",
            )
            topology = tuple(GattAttribute(**entry) for entry in document["topology"])
            declarations = {
                handle: bytes.fromhex(value)
                for handle, value in document["declarations"].items()
            }
            resolved = _resolve_topology(
                topology, declarations, handles, document["service_uuid"]
            )
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError(f"{SCENARIO}: malformed topology evidence: {error}") from error
    require(
        resolved["target_handle"] == document["target_handle"],
        "physical descriptor interval",
    )
    original = status_from_document(armed["status"])
    require(
        original.ticket.owner_nonce == owner_nonce(control.session)
        and original.ticket.handle == document.get("target_handle")
        and document.get("value_handle") != original.ticket.handle,
        "physical owner/descriptor",
    )
    validate_status(original, Phase.ARMED)
    request = ArmRequest.decode(
        Opcode.ARM_CCC, bytes.fromhex(armed["arm_request_wire"])
    )
    common, ticket = request.common, original.ticket
    require(
        request.before == BEFORE
        and request.requested == REQUESTED
        and (
            common.boot_nonce,
            common.owner_nonce,
            common.connection_epoch,
            common.bearer_epoch,
            common.database_epoch,
            common.handle,
            common.peer,
        )
        == (
            ticket.boot_nonce,
            ticket.owner_nonce,
            ticket.connection_epoch,
            ticket.bearer_epoch,
            ticket.database_epoch,
            ticket.handle,
            original.peer,
        ),
        "ARM wire/receipt binding",
    )
    current = status_from_document(row["status"])
    validate_status(
        current,
        Phase.ARMED
        if name == "armed"
        else Phase.HELD
        if name == "held"
        else Phase.RETIRED,
        ticket=original.ticket,
        peer=original.peer,
    )
    if name in ("armed", "rejected", "silence-started", "close-allowed"):
        require(
            isinstance(row.get("local"), dict)
            and row["local"].get("att_response") == 0
            and type(row["local"]["att_response"]) is int
            and row["local"].get("value") == "0000"
            and row["local"].get("peer") == original.peer.hex(),
            "independent per-peer disabled readback",
        )
        database = ticket.database_epoch + (
            1 if name == "silence-started" else 2 if name == "close-allowed" else 0
        )
        expected_route = {
            "boot_nonce": ticket.boot_nonce.hex(),
            "peer": original.peer.hex(),
            "peer_present": True,
            "connection_epoch": ticket.connection_epoch,
            "bearer_epoch": ticket.bearer_epoch,
            "database_epoch": database,
            "engine_flags": 0,
        }
        require(
            row["local"].get("limits_before")
            == row["local"].get("limits_after")
            == expected_route,
            "local read lacks stable live GET_LIMITS identity",
        )
    if name not in ("armed", "held"):
        require(
            row["status"] == _one(rows, "rejected")["status"],
            "retained receipt changed",
        )
        resolved_row = _one(rows, "resolve-dispatched")
        require(
            resolved_row.get("decision") == 2
            and resolved_row.get("ticket_wire") == ticket.encode().hex(),
            "exact single REJECT submission",
        )
    if name == "close-allowed":
        started = _one(rows, "silence-started").get("silence_started")
        require(
            type(started) in (int, float)
            and math.isfinite(started)
            and row["monotonic"] - started >= 1,
            "physical silence duration",
        )
    if name in ("value-sent", "silence-started"):
        require(
            row.get("submission")
            == {
                "handle": document.get("value_handle"),
                "value": "ef" if name == "value-sent" else "f0",
                "result": "accepted",
                "delivery": "not-proven",
            },
            "value submission is not delivery",
        )
    if name == "closed":
        require(protocol == list(BOUNDARIES), "close before seven barriers")
        _resources(row.get("resources"), closed=True)
        gap = [entry for entry in rows if entry.get("boundary") == "gap"]
        require(
            [(entry.get("opcode"), entry.get("peer")) for entry in gap]
            == [
                (GAP_DEVICE_CONNECTED, original.peer.hex()),
                (GAP_DEVICE_DISCONNECTED, original.peer.hex()),
            ],
            "closed exact GAP pair",
        )
        require(
            row.get("peer_count") == 0
            and row.get("gap_attached") == row.get("gap_detached") == 1
            and row.get("engine_flags") == 0,
            "closed physical counts",
        )
        limits = row.get("limits", {})
        require(
            limits.get("boot_nonce") == ticket.boot_nonce.hex()
            and limits.get("peer_present") is False
            and limits.get("peer") == bytes(7).hex()
            and limits.get("engine_flags") == 0,
            "closed no-peer idle proof",
        )
    return current


class CccEnableRejectRelay(common.CccRelay):
    def __init__(self, root: Path) -> None:
        super().__init__(root, sys.modules[__name__])


TOPOLOGY_METHOD = "tester-uuid-interval-v1"


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


class CccEnableReject(common.CccOwner):
    def __init__(self, fixture: Any, root: Path, handles: dict[str, int]) -> None:
        super().__init__(fixture, root, handles, sys.modules[__name__])

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
        facts = self.wait("arm")
        discovery = [e for e in facts["events"] if e["kind"] == "gattDiscovered"]
        require(len(discovery) == 1, "unique App discovery")
        services = discovery[0].get("gattServices")
        require(
            isinstance(services, list)
            and len(services) == 1
            and services[0].get("uuid") == self.plan.service_uuid
            and services[0].get("primary") is True,
            "App target primary service",
        )
        chars = services[0].get("characteristics", [])
        for target, field, capability in (
            (UPDATES_UUID, "characteristicId", "notifiable"),
            (READ_UUID, "readCharacteristicId", "writableWithResponse"),
        ):
            matches = [c for c in chars if c.get("uuid") == target]
            require(
                len(matches) == 1
                and matches[0].get("id") == facts["steps"]["gatt"][field]
                and matches[0].get("readable") is True
                and matches[0].get(capability) is True,
                "App target characteristic",
            )
        self.document.update(
            resolve_target(self.fixture._gatt, self.handles, self.plan.service_uuid)
        )
        local = self.local()
        limits = self.live()
        self.database_epoch = limits.database_epoch
        common = ArmCommon(
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
        request = ArmRequest(Operation.CCC_WRITE, common, BEFORE, REQUESTED)
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
        self.wait("enable-dispatched")
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
        self.boundary("rejected", self.retained, local=self.local())
        self.wait("recovery-enabled")
        self.boundary(
            "value-sent",
            self.retired(time.monotonic() + RETIRE_SECONDS),
            submission=self.emit(b"\xef"),
        )
        self.wait("recovery-notified")
        self.live()
        self.boundary(
            "disable-allowed", self.retired(time.monotonic() + RETIRE_SECONDS)
        )
        self.wait("disabled")
        local = self.local()
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
            time.monotonic() - silence_started >= 1.0,
            "host silence shorter than one second",
        )
        self.boundary(
            "close-allowed",
            self.retired(time.monotonic() + RETIRE_SECONDS),
            local=self.local(),
        )
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
            "final no-peer/idle/boot",
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
            runtime_bytes=len(text.encode("utf-8")),
        )
        validate_boundary(self.document, self.control, "closed")


def run(fixture: Any, root: Path, handles: dict[str, int]) -> None:
    try:
        owner = CccEnableReject(fixture, root, handles)
    except BaseException as error:
        try:
            record_start_failure(root, fixture._fixture_session, error)
        except BaseException as publication:  # noqa: BLE001 - construction failure remains primary
            error.add_note(f"{SCENARIO} startup publication: {publication}")
        raise
    owner.run()
