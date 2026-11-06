"""Offline CCC enable REJECT replay; never physical control or RF authorization.

Provider validators own CCC/native semantics. This ledger independently joins the
complete normalized transcript to every physical prefix, original deadlines,
frozen profile/installation receipt and exact peer lifecycle. Rehashing an edited
sidecar cannot replace the native notification or read/silence fences.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840 import ccc_enable_reject as protocol
from tests.peripherals.nrf52840 import fault_control as fault
from tests.peripherals.nrf52840 import fixture_session as session
from tests.peripherals.nrf52840.cancellation_control import (
    load_control,
    read_document,
    require_multifunction,
)
from tests.peripherals.nrf52840.gatt_commands import (
    tester_characteristic_native_permissions,
    uuid_to_le_bytes,
)
from tests.peripherals.nrf52840.profile import ProfilePlan

runtime_facts = protocol.runtime_facts
require = protocol.require
COMMAND_IDS = frozenset(protocol.COMMANDS.values())
PHYSICAL_ORDER = (
    "no-peer",
    "gap",
    "armed",
    "held",
    "still-held",
    "resolve-dispatched",
    "rejected",
    "value-sent",
    "disable-allowed",
    "silence-started",
    "close-allowed",
    "gap",
    "closed",
)


def _time(value: Any) -> float:
    require(
        type(value) in (int, float) and math.isfinite(value) and value >= 0,
        "invalid physical time",
    )
    return value


def _same(actual: Any, expected: dict[str, Any], label: str) -> None:
    """Typed subset binding: bool must never impersonate an epoch or counter."""
    require(isinstance(actual, dict), f"{label} object")
    for key, value in expected.items():
        require(
            type(actual.get(key)) is type(value) and actual[key] == value,
            f"{label} {key}",
        )


def _target(
    facts: dict[str, Any], document: dict[str, Any], profile: ProfilePlan
) -> None:
    """Bind opaque native IDs and captured declarations to the frozen profile.

    validate_boundary already resolves the CCC interval and exact FFF1/FFF2
    declaration associations. Check profile membership of all captured values and
    the native target/descriptor projection here, without inventing ATT IDs.
    """
    discoveries = [
        event for event in facts["events"] if event["kind"] == "gattDiscovered"
    ]
    require(len(discoveries) == 1, "unique native discovery")
    services = discoveries[0].get("gattServices")
    require(
        isinstance(services, list)
        and len(services) == 1
        and isinstance(services[0], dict)
        and services[0].get("uuid") == profile.service_uuid,
        "native profile service",
    )
    chars = services[0].get("characteristics")
    require(
        isinstance(chars, list)
        and len(chars) <= 512
        and all(isinstance(c, dict) for c in chars),
        "native characteristic topology",
    )
    step = facts["steps"]["gatt"]
    by_uuid = {c.uuid: c for c in profile.characteristics}
    require(
        all(
            protocol._id(c.get("id"))
            and isinstance(c.get("uuid"), str)
            and c["uuid"] in by_uuid
            for c in chars
        )
        and len({c["id"] for c in chars}) == len(chars)
        and len({c["uuid"] for c in chars}) == len(chars),
        "native profile instance identities",
    )
    metadata = document.get("topology_method") == protocol.TOPOLOGY_METHOD
    if metadata:
        require(services[0].get("primary") is True, "native primary service binding")
        for native in chars:
            definition = by_uuid[native["uuid"]]
            for flag, bit in (
                ("readable", 0x02),
                ("writableWithResponse", 0x08),
                ("writableWithoutResponse", 0x04),
                ("notifiable", 0x10),
                ("indicatable", 0x20),
            ):
                require(
                    native.get(flag) is bool(definition.properties & bit),
                    "native/profile properties for metadata topology",
                )
        binding = document["service_binding"]
        _same(
            binding,
            {"source": "native-gatt-unique-value-uuid", "uuid": services[0]["uuid"]},
            "native service binding",
        )
    for target, field, capabilities in (
        (
            protocol.UPDATES_UUID,
            "characteristicId",
            ("readable", "notifiable", "indicatable"),
        ),
        (
            protocol.READ_UUID,
            "readCharacteristicId",
            ("readable", "writableWithResponse"),
        ),
    ):
        matches = [c for c in chars if c.get("uuid") == target]
        require(
            len(matches) == 1
            and matches[0].get("id") == step[field]
            and all(matches[0].get(key) is True for key in capabilities),
            "native target/profile route",
        )
        definition = by_uuid[target]
        require(
            definition.initial_value == b"\x00"
            and definition.requires_ccc == (target == protocol.UPDATES_UUID),
            "profile target baseline/CCC",
        )
        if definition.requires_ccc:
            descriptors = matches[0].get("descriptors")
            require(
                isinstance(descriptors, list)
                and len(descriptors) <= 512
                and all(isinstance(d, dict) for d in descriptors),
                "native descriptor topology",
            )
            ccc = [
                d
                for d in descriptors
                if d.get("uuid") in ("2902", "00002902-0000-1000-8000-00805f9b34fb")
            ]
            require(
                len(ccc) == 1
                and protocol._id(ccc[0].get("id"))
                and ccc[0]["id"]
                not in (step["characteristicId"], step["readCharacteristicId"]),
                "native target CCC descriptor",
            )
    attrs = document["topology"]
    require(all(isinstance(a["uuid"], str) for a in attrs), "physical UUID field types")
    declarations = (
        document["declaration_associations"] if metadata else document["declarations"]
    )
    require(
        set(declarations)
        == {
            str(a["handle"])
            for a in attrs
            if a["uuid"] in (("2803",) if metadata else ("2800", "2801", "2803"))
        },
        "declaration inventory",
    )
    claimed: set[int] = set()
    declared: set[str] = set()
    for attr in attrs:
        if attr["uuid"] != "2803":
            continue
        if metadata:
            value_handle = declarations[str(attr["handle"])]
            entries = [a for a in attrs if a["handle"] == value_handle]
            require(
                len(entries) == 1 and entries[0]["uuid"] in by_uuid,
                "metadata value/profile association",
            )
            definition = by_uuid[entries[0]["uuid"]]
        else:
            encoded = bytes.fromhex(declarations[str(attr["handle"])])
            definitions = [
                c
                for c in profile.characteristics
                if encoded[3:] == uuid_to_le_bytes(c.uuid)
            ]
            require(
                len(encoded) == 19 and len(definitions) == 1, "declaration/profile UUID"
            )
            definition = definitions[0]
            value_handle = int.from_bytes(encoded[1:3], "little")
            require(
                encoded[0] == definition.properties, "declaration/profile properties"
            )
        require(
            definition.uuid not in declared and value_handle not in claimed,
            "duplicate profile declaration/value",
        )
        declared.add(definition.uuid)
        claimed.add(value_handle)
        values = [a for a in attrs if a["handle"] == value_handle]
        require(
            len(values) == 1
            and values[0]["uuid"] == definition.uuid
            and values[0]["permissions"]
            == tester_characteristic_native_permissions(definition.permissions),
            "declaration/profile value permissions",
        )
        end = next(
            (
                a["handle"]
                for a in attrs
                if a["handle"] > attr["handle"] and a["uuid"] == "2803"
            ),
            65536,
        )
        require(attr["handle"] < value_handle < end, "profile declaration interval")
        descriptors = [
            a for a in attrs if value_handle < a["handle"] < end and a["uuid"] == "2902"
        ]
        require(
            len(descriptors) == int(definition.requires_ccc)
            and all(a["permissions"] == 3 for a in descriptors),
            "profile descriptor association",
        )
    require(
        {a["handle"] for a in attrs if a["uuid"] in by_uuid} == claimed,
        "unassociated profile value",
    )


def _prefixes(text: str, document: dict[str, Any], control) -> list[dict[str, Any]]:
    """Replay each byte-exact prefix in its own ready-before-ACK interval."""
    raw = text.encode("utf-8")
    lines = text.splitlines(keepends=True)
    require(
        lines and text.endswith("\n") and "\r" not in text,
        "normalized LF transcript required",
    )
    ends: dict[str, int] = {}
    starts: dict[str, int] = {}
    offset = 0
    for line in lines:
        require(
            line.endswith("\n") and line.startswith(protocol.PREFIX),
            "non-normalized runtime line",
        )
        body = line[len(protocol.PREFIX) : -1]
        require(
            body == body.strip()
            and body.startswith(
                (protocol.RUNTIME_MODE + ":", "rf-control:", "rf-host:")
            ),
            "non-normalized runtime body",
        )
        if body.startswith("rf-control:"):
            require(body not in ends, "duplicate control offset")
            starts[body] = offset
            ends[body] = offset + len(line.encode("utf-8"))
        offset += len(line.encode("utf-8"))
    require(
        list(ends) == protocol.control_records(control), "complete original controls"
    )
    rows = {
        row["boundary"]: row
        for row in document["boundaries"]
        if row["boundary"] != "gap"
    }
    proofs = []
    previous = 0
    for index, phase in enumerate((*protocol.PHASES, "closed")):
        row = rows[phase]
        size = row["runtime_bytes"]
        require(
            previous < size <= len(raw) and raw[size - 1 : size] == b"\n",
            "prefix order/line boundary",
        )
        prefix_raw = raw[:size]
        require(
            hashlib.sha256(prefix_raw).hexdigest() == row["runtime_sha256"],
            "physical runtime prefix hash",
        )
        try:
            prefix = prefix_raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("ccc-enable-reject: prefix cuts UTF-8") from error
        facts = protocol._transcript(prefix, control)
        if phase == "closed":
            runtime_facts(prefix, run_id=control.session, complete=True)
            require(
                all(
                    line.startswith(protocol.PREFIX + "rf-host:")
                    for line in raw[size:].decode("utf-8").splitlines()
                ),
                "native records outside physical close prefix",
            )
        else:
            request = protocol.REQUESTS[index]
            ready = control.marker("ready", request)
            ack = control.marker("observed", phase)
            require(
                ends[ready] <= size <= starts[ack],
                "prefix outside original readiness/ACK interval",
            )
            require(
                protocol.READY_STEPS[index] in facts["steps"],
                "prefix missing App readiness witness",
            )
        proofs.append(
            {
                "boundary": phase,
                "runtime_bytes": size,
                "runtime_sha256": row["runtime_sha256"],
            }
        )
        previous = size
    return proofs


def verify(
    text: str,
    *,
    platform: str,
    run_id: str,
    service_uuid: str,
    physical_path: Path,
    fixture_request: Path,
    fixture_identity: tuple[int, int],
) -> dict[str, Any]:
    """Return a portable schema-2 ledger from complete frozen offline evidence.

    fixture_identity is the original positive PID/PGID, not a live process query.
    Neither a passed sidecar nor SDK/send admission substitutes for App delivery.
    """
    require(platform in ("android", "ios"), "platform")
    require(
        len(fixture_identity) == 2
        and all(type(v) is int and v > 0 for v in fixture_identity),
        "fixture process identity",
    )
    plan = require_multifunction(session.load_session(fixture_request))
    require(
        plan.session_id == run_id and plan.service_uuid == service_uuid,
        "fixture run/service",
    )
    receipt = session.verify_receipt(plan, fixture_identity=fixture_identity)
    profile = session.load_session_profile(plan.contract, plan.profile_path)
    root = physical_path.parent
    require(physical_path.name == protocol.PHYSICAL_NAME, "physical report name")
    control = protocol.validate_control(root, plan)
    document = read_document(physical_path)
    _same(
        document,
        {
            "schema_version": 2,
            "scenario": protocol.SCENARIO,
            "mode": protocol.MODE,
            "session_id": run_id,
            "service_uuid": service_uuid,
            "resource_identity": plan.resource_identity,
            "contract_sha256": plan.contract_sha256,
            "profile_sha256": plan.profile_sha256,
            "fixture_pid": fixture_identity[0],
            "fixture_pgid": fixture_identity[1],
            "status": "passed",
            "target_uuid": protocol.UPDATES_UUID,
        },
        "physical",
    )
    require(
        document.get("primary_error") is None and document.get("cleanup_errors") == [],
        "failed physical evidence",
    )
    facts = runtime_facts(text, run_id=run_id, platform=platform, complete=True)
    protocol._transcript(text, control)
    rows = document.get("boundaries")
    require(
        isinstance(rows, list)
        and len(rows) == len(PHYSICAL_ORDER)
        and all(isinstance(row, dict) for row in rows),
        "physical boundary inventory",
    )
    require(
        tuple(row.get("boundary") for row in rows) == PHYSICAL_ORDER,
        "physical action sequence",
    )
    times = [_time(row.get("monotonic")) for row in rows]
    require(all(a <= b for a, b in pairwise(times)), "physical time reversed")
    by_name = {row["boundary"]: row for row in rows if row["boundary"] != "gap"}
    (
        initial,
        armed,
        held,
        still,
        resolution,
        rejected,
        silence,
        close_allowed,
        closed,
    ) = (
        by_name[name]
        for name in (
            "no-peer",
            "armed",
            "held",
            "still-held",
            "resolve-dispatched",
            "rejected",
            "silence-started",
            "close-allowed",
            "closed",
        )
    )
    whole_deadline = _time(document.get("deadline"))
    require(
        0 < whole_deadline - initial["monotonic"] <= protocol.WHOLE_SECONDS
        and closed["monotonic"] < whole_deadline,
        "whole original deadline",
    )
    statuses = {
        name: protocol.validate_boundary(document, control, name)
        for name in (*protocol.PHASES, "closed")
    }
    status = statuses["armed"]
    ticket = status.ticket
    _target(facts, document, profile)
    request = fault.ArmRequest.decode(
        fault.Opcode.ARM_CCC, bytes.fromhex(armed["arm_request_wire"])
    )
    require(
        request.common.arm_budget_ms == protocol.ARM_MS
        and request.common.hold_budget_ms == protocol.HOLD_MS,
        "original ARM/hold budgets",
    )
    require(
        status.remaining.arm_ms <= request.common.arm_budget_ms
        and statuses["held"].remaining.hold_ms <= request.common.hold_budget_ms,
        "remaining budget exceeds ARM",
    )
    still_status = fault.status_from_document(still["status"])
    protocol.validate_status(
        still_status, fault.Phase.HELD, ticket=ticket, peer=status.peer
    )
    require(
        still_status.remaining.hold_ms <= statuses["held"].remaining.hold_ms,
        "HELD deadline renewed",
    )
    reject_deadline = _time(armed.get("reject_deadline"))
    anchor = reject_deadline - protocol.REJECT_SECONDS
    submitted = _time(resolution.get("submitted_at"))
    require(
        rows[1]["monotonic"]
        <= anchor
        <= armed["monotonic"]
        < held["monotonic"]
        <= still["monotonic"]
        <= submitted
        <= resolution["monotonic"]
        < reject_deadline,
        "original REJECT time/order",
    )
    retirement_deadline = (
        submitted + fault.RESOLUTION_MAX_MS / 1000 + protocol.RETIRE_SECONDS
    )
    require(
        rejected["monotonic"] < retirement_deadline
        and submitted - held["monotonic"] < request.common.hold_budget_ms / 1000,
        "original retirement/hold deadline",
    )
    _same(resolution, {"decision": 2, "ticket_wire": ticket.encode().hex()}, "REJECT")
    expected_initial = {
        "boot_nonce": ticket.boot_nonce.hex(),
        "supported_operations": 7,
        "bearer_policy": 0,
        "max_connections": 1,
        "max_live_slots": 1,
        "max_value_length": fault.VALUE_MAX,
        "engine_flags": 0,
        "arm_max_ms": fault.ARM_MAX_MS,
        "hold_max_ms": fault.HOLD_MAX_MS,
        "resolution_max_ms": fault.RESOLUTION_MAX_MS,
        "retirement_max_ms": fault.RETIREMENT_MAX_MS,
        "peer_present": False,
        "peer": bytes(7).hex(),
        "connection_epoch": 0,
        "bearer_epoch": 0,
        "database_epoch": 0,
    }
    _same(initial.get("limits"), expected_initial, "initial no-peer idle limits")
    require(set(initial["limits"]) == set(expected_initial), "initial limits schema")
    for name in ("armed", "rejected", "silence-started", "close-allowed"):
        local = by_name[name]["local"]
        _same(
            local,
            {"att_response": 0, "value": "0000", "peer": status.peer.hex()},
            "local CCC",
        )
        for route in (local["limits_before"], local["limits_after"]):
            require(
                type(route.get("peer_present")) is bool
                and all(
                    type(route.get(key)) is int
                    for key in (
                        "connection_epoch",
                        "bearer_epoch",
                        "database_epoch",
                        "engine_flags",
                    )
                ),
                "live limits field types",
            )
    _same(
        closed.get("limits"),
        {
            "boot_nonce": ticket.boot_nonce.hex(),
            "peer_present": False,
            "peer": bytes(7).hex(),
            "engine_flags": 0,
        },
        "closed no-peer idle limits",
    )
    _same(
        closed,
        {"peer_count": 0, "gap_attached": 1, "gap_detached": 1, "engine_flags": 0},
        "physical close",
    )
    require(
        closed["resources"] == facts["resources"], "physical/native resources differ"
    )
    for row in (rows[1], rows[-2]):
        require(
            type(row.get("opcode")) is int and row.get("peer") == status.peer.hex(),
            "physical GAP field types/peer",
        )
    started = _time(silence.get("silence_started"))
    require(
        by_name["disable-allowed"]["monotonic"]
        <= started
        <= silence["monotonic"]
        <= close_allowed["monotonic"]
        and close_allowed["monotonic"] - started >= 1,
        "original host silence fence",
    )
    proofs = _prefixes(text, document, control)
    controls = []
    for request_name, phase in zip(protocol.REQUESTS, protocol.PHASES, strict=True):
        for path in (
            root / f"request-{request_name}.json",
            root / f"signal-{phase}.json",
        ):
            require(
                read_document(path) == asdict(control),
                "request/signal original identity",
            )
            controls.append(path)
    require(
        load_control(root / protocol.CONTROL_NAME) == control
        and session.verify_receipt(plan, fixture_identity=fixture_identity) == receipt
        and read_document(physical_path) == document
        and all(read_document(path) == asdict(control) for path in controls),
        "inputs changed during replay",
    )
    return {
        "schema_version": 2,
        "scenario": protocol.SCENARIO,
        "platform": platform,
        "run_id": run_id,
        "nonce": facts["nonce"],
        "service_uuid": service_uuid,
        "control": asdict(control),
        "ticket": fault.ticket_document(ticket),
        "runtime_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "runtime_prefixes": proofs,
        "native_terminal_count": len(COMMAND_IDS),
        "rejected_command_id": "33010",
        "failed_subscription_id": "33009",
        "recovery_subscription_id": "33012",
        "target": {
            "characteristic_id": facts["steps"]["gatt"]["characteristicId"],
            "read_characteristic_id": facts["steps"]["gatt"]["readCharacteristicId"],
            "ccc_handle": document["target_handle"],
            "value_handle": document["value_handle"],
            "read_handle": document["read_handle"],
        },
        "outcome": statuses["rejected"].outcome.name,
        "warm_resources": facts["warm_resources"],
        "closed_resources": facts["resources"],
        "notification": {
            "subscription_id": "33012",
            "value": "ef",
            "delivery": "app-observed",
        },
        "silence": {
            "submission_value": "f0",
            "app_elapsed_ms": int(facts["steps"]["silence"]["elapsedMs"]),
            "host_elapsed_seconds": close_allowed["monotonic"] - started,
        },
        "known_peers_remaining": 0,
    }
