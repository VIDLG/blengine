"""Pure write-reject replay, not device control or RF reauthorization.

Provider and capture publish cooperative evidence. This checker binds raw native
projections, exact v2 receipt bytes, local readbacks and physical lifecycle; hashes
alone or a passed sidecar cannot substitute for those independent facts.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840 import fault_control as fault
from tests.peripherals.nrf52840 import fixture_session as session
from tests.peripherals.nrf52840 import write_reject as protocol
from tests.peripherals.nrf52840.cancellation_control import (
    load_control,
    read_document,
    require_multifunction,
)
from tests.peripherals.nrf52840.gap_commands import (
    GAP_DEVICE_CONNECTED,
    GAP_DEVICE_DISCONNECTED,
)
from tests.peripherals.nrf52840.gatt_commands import (
    tester_characteristic_native_permissions,
)

runtime_facts = protocol.runtime_facts
require = protocol.require


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
    """Validate one complete same-Probe/connection response-write rejection.

    Paths refer to frozen session inputs and the named schema-2 physical report.
    fixture_identity is the original positive PID/PGID, never selected by name.
    Return a portable semantic ledger; perform no process, device or BLE actions.
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
    rw = next(c for c in profile.characteristics if c.role == "read-write")
    root = physical_path.parent
    require(physical_path.name == protocol.PHYSICAL_NAME, "physical report name")
    control = protocol.validate_control(root, plan)
    document = read_document(physical_path)
    require(
        document.get("status") == "passed"
        and not document.get("primary_error")
        and document.get("cleanup_errors") == [],
        "physical verdict",
    )
    for field, expected in {
        "fixture_pid": fixture_identity[0],
        "fixture_pgid": fixture_identity[1],
        "resource_identity": plan.resource_identity,
        "contract_sha256": plan.contract_sha256,
        "profile_sha256": plan.profile_sha256,
        "service_uuid": service_uuid,
        "target_uuid": rw.uuid,
        "permissions": tester_characteristic_native_permissions(3),
    }.items():
        require(
            type(document.get(field)) is type(expected) and document[field] == expected,
            f"physical {field}",
        )
    require(
        rw.permissions == 3 and rw.initial_value == protocol.BEFORE,
        "profile RW baseline",
    )
    facts = runtime_facts(text, run_id=run_id, platform=platform, complete=True)
    protocol.verify_control_transcript(text, control)
    protocol.validate_target(facts, service_uuid, rw.uuid)
    records = [
        line.split(protocol.PREFIX, 1)[1].strip()
        for line in text.splitlines()
        if protocol.PREFIX in line
        and line.split(protocol.PREFIX, 1)[1].startswith("rf-control:")
    ]
    require(records == protocol.control_records(control), "complete control transcript")
    for request, phase in zip(protocol.REQUESTS, protocol.PHASES, strict=True):
        require(
            read_document(root / f"request-{request}.json") == asdict(control),
            "request identity",
        )
        require(
            read_document(root / f"signal-{phase}.json") == asdict(control),
            "signal identity",
        )
    rows = document.get("boundaries")
    require(isinstance(rows, list) and 1 <= len(rows) <= 256, "physical boundaries")
    times = [_time(r["monotonic"]) for r in rows]
    require(all(a <= b for a, b in pairwise(times)), "physical time reversed")

    def one(name: str) -> dict[str, Any]:
        matches = [r for r in rows if r.get("boundary") == name]
        require(len(matches) == 1, f"missing/duplicate {name}")
        return matches[0]

    expected_names = [
        "no-peer",
        "gap",
        "armed",
        "signal",
        "held",
        "signal",
        "still-held",
        "resolve-dispatched",
        "rejected",
        "signal",
        "recovery",
        "signal",
        "gap",
        "closed",
    ]
    require([r["boundary"] for r in rows] == expected_names, "physical action sequence")
    initial, armed, held, still, resolution, rejected, recovered, closed = (
        one(name)
        for name in (
            "no-peer",
            "armed",
            "held",
            "still-held",
            "resolve-dispatched",
            "rejected",
            "recovery",
            "closed",
        )
    )
    whole_deadline = _time(document["deadline"])
    require(
        0 < whole_deadline - initial["monotonic"] <= protocol.WHOLE_SECONDS
        and closed["monotonic"] < whole_deadline,
        "whole original deadline",
    )
    status = protocol.validate_boundary(document, control, "armed")
    ticket = status.ticket
    require(
        type(document.get("target_handle")) is int
        and ticket.handle == document["target_handle"],
        "target handle type",
    )
    protocol.validate_boundary(document, control, "held")
    rejected_status = protocol.validate_boundary(document, control, "rejected")
    recovered_status = protocol.validate_boundary(document, control, "recovery")
    protocol.validate_status(
        fault.status_from_document(still["status"]),
        phase=fault.Phase.HELD,
        ticket=ticket,
        peer=status.peer,
    )
    closed_status = fault.status_from_document(closed["status"])
    protocol.validate_status(
        closed_status, phase=fault.Phase.RETIRED, ticket=ticket, peer=status.peer
    )
    require(
        rejected_status == recovered_status == closed_status,
        "retained rejected receipt changed after normal I/O/close",
    )
    require(
        type(armed.get("local", {}).get("att_response")) is int
        and armed.get("local") == {"att_response": 0, "value": "00"},
        "local before",
    )
    request = fault.ArmRequest.decode(
        fault.Opcode.ARM_WRITE, bytes.fromhex(armed["arm_request_wire"])
    )
    require(
        request.operation is fault.Operation.RESPONSE_WRITE
        and request.before == protocol.BEFORE
        and request.requested == protocol.REQUESTED,
        "ARM bytes",
    )
    common = request.common
    require(
        (
            common.boot_nonce,
            common.owner_nonce,
            common.connection_epoch,
            common.bearer_epoch,
            common.database_epoch,
            common.peer,
            common.handle,
        )
        == (
            ticket.boot_nonce,
            ticket.owner_nonce,
            ticket.connection_epoch,
            ticket.bearer_epoch,
            ticket.database_epoch,
            status.peer,
            ticket.handle,
        ),
        "ARM ticket/live epochs",
    )
    require(
        100 <= common.arm_budget_ms <= protocol.ARM_MS
        and 100 <= common.hold_budget_ms <= protocol.HOLD_MS,
        "ARM safety budgets",
    )
    limits = armed["limits"]
    require(
        all(
            type(limits.get(k)) is int
            for k in (
                "engine_flags",
                "connection_epoch",
                "bearer_epoch",
                "database_epoch",
            )
        )
        and limits.get("peer_present") is True,
        "live limits field types",
    )
    require(
        limits
        == {
            "boot_nonce": ticket.boot_nonce.hex(),
            "engine_flags": 0,
            "peer_present": True,
            "peer": status.peer.hex(),
            "connection_epoch": ticket.connection_epoch,
            "bearer_epoch": ticket.bearer_epoch,
            "database_epoch": ticket.database_epoch,
        },
        "live ARM limits",
    )
    for row in (initial, closed):
        value = row["limits"]
        require(
            value["boot_nonce"] == ticket.boot_nonce.hex()
            and type(value["engine_flags"]) is int
            and value["engine_flags"] == 0
            and value["peer_present"] is False
            and value["peer"] == "00" * 7
            and all(
                type(value[k]) is int and value[k] == 0
                for k in ("connection_epoch", "bearer_epoch", "database_epoch")
            ),
            "no-peer idle limits",
        )
    gap = [r for r in rows if r["boundary"] == "gap"]
    require(
        [r["opcode"] for r in gap] == [GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED]
        and all(r["peer"] == status.peer.hex() for r in gap),
        "exact known-peer attach/detach",
    )
    require(
        resolution.get("decision") == 2
        and type(resolution.get("decision")) is int
        and resolution.get("ticket_wire") == ticket.encode().hex(),
        "single REJECT decision",
    )
    reject_deadline = _time(armed["reject_deadline"])
    require(
        0 < reject_deadline - _time(armed["monotonic"]) <= protocol.REJECT_SECONDS
        and resolution["monotonic"] < reject_deadline,
        "REJECT original deadline",
    )
    retirement_deadline = _time(resolution["retirement_deadline"])
    require(
        0 < retirement_deadline - resolution["monotonic"] <= 2 + protocol.RETIRE_SECONDS
        and rejected["monotonic"] < retirement_deadline,
        "retirement original deadline",
    )
    signals = [r for r in rows if r["boundary"] == "signal"]
    require([r["phase"] for r in signals] == list(protocol.PHASES), "signal sequence")
    for boundary, signal in zip(
        (armed, held, rejected, recovered), signals, strict=True
    ):
        require(
            rows.index(boundary) < rows.index(signal)
            and boundary["monotonic"] <= signal["monotonic"],
            "signal before published physical boundary",
        )
    require(
        type(closed.get("peer_count")) is int
        and closed["peer_count"] == 0
        and closed.get("resources") == facts["resources"]
        and all(type(v) is int for v in closed["resources"].values()),
        "physical/native close",
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    require(closed.get("runtime_sha256") == digest, "physical runtime snapshot drift")

    require(
        load_control(root / protocol.CONTROL_NAME) == control
        and session.verify_receipt(plan, fixture_identity=fixture_identity) == receipt,
        "inputs changed during replay",
    )
    return {
        "schema_version": 2,
        "scenario": "write-reject",
        "platform": platform,
        "run_id": run_id,
        "nonce": facts["nonce"],
        "service_uuid": service_uuid,
        "control": asdict(control),
        "ticket": fault.ticket_document(ticket),
        "runtime_sha256": digest,
        "native_terminal_count": len(COMMAND_IDS),
        "rejected_command_id": "30010",
        "outcome": rejected_status.outcome.name,
        "closed_resources": facts["resources"],
        "known_peers_remaining": 0,
    }


COMMAND_IDS = frozenset(protocol.COMMANDS.values())
