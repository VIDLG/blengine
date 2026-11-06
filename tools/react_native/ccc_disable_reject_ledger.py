"""Offline CCC disable REJECT replay; never physical control or RF authorization.

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

from tests.peripherals.nrf52840 import ccc_common as common
from tests.peripherals.nrf52840 import ccc_disable_reject as protocol
from tests.peripherals.nrf52840 import fault_control as fault
from tests.peripherals.nrf52840 import fixture_session as session
from tests.peripherals.nrf52840.cancellation_control import (
    load_control,
    read_document,
    require_multifunction,
)

runtime_facts = protocol.runtime_facts
require = protocol.require
COMMAND_IDS = frozenset(protocol.COMMANDS.values())
PHYSICAL_ORDER = (
    "no-peer",
    "gap",
    "baseline-value-sent",
    "armed",
    "held",
    "still-held",
    "resolve-dispatched",
    "rejected",
    "continued-value-sent",
    "retry-allowed",
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
    common._same(
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
    common.validate_native_target(facts, document, profile)
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
    common._same(
        resolution, {"decision": 2, "ticket_wire": ticket.encode().hex()}, "REJECT"
    )
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
    common._same(initial.get("limits"), expected_initial, "initial no-peer idle limits")
    require(set(initial["limits"]) == set(expected_initial), "initial limits schema")
    for name in (
        "baseline-value-sent",
        "armed",
        "rejected",
        "continued-value-sent",
        "retry-allowed",
        "silence-started",
        "close-allowed",
    ):
        local = by_name[name]["local"]
        common._same(
            local,
            {
                "att_response": 0,
                "value": "0000"
                if name in ("silence-started", "close-allowed")
                else "0100",
                "peer": status.peer.hex(),
            },
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
    common._same(
        closed.get("limits"),
        {
            "boot_nonce": ticket.boot_nonce.hex(),
            "peer_present": False,
            "peer": bytes(7).hex(),
            "engine_flags": 0,
        },
        "closed no-peer idle limits",
    )
    common._same(
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
        by_name["retry-allowed"]["monotonic"]
        <= started
        <= silence["monotonic"]
        <= close_allowed["monotonic"]
        and close_allowed["monotonic"] - started >= 1,
        "original host silence fence",
    )
    proofs = common.verify_prefixes(text, document, control, protocol)
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
        "rejected_command_id": "34011",
        "subscription_id": "34009",
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
        "baseline_notification": {
            "subscription_id": "34009",
            "value": "ef",
            "delivery": "app-observed",
        },
        "notification": {
            "subscription_id": "34009",
            "value": "e1",
            "delivery": "app-observed",
        },
        "silence": {
            "submission_value": "f0",
            "app_elapsed_ms": int(facts["steps"]["silence"]["elapsedMs"]),
            "host_elapsed_seconds": close_allowed["monotonic"] - started,
        },
        "known_peers_remaining": 0,
    }
