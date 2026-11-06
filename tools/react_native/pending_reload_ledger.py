"""Read-only final checker for the Android pending-read engine-reload gate.

The parent supplies normalized markers from its pinned-PID capture and confirmed
ADB actions. Call before stopping the fixture: the current session receipt and
immutable input hashes are revalidated. No process/device commands or output
writes occur here. This is cooperative evidence, not a security attestation.
"""

from __future__ import annotations

import math
import re
import uuid
from itertools import pairwise
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840 import fixture_session as session
from tests.peripherals.nrf52840 import pending_reload as physical
from tests.peripherals.nrf52840.cancellation_control import require_multifunction

__all__ = ["verify"]

_NATIVE_ORDER = (
    "read-accepted",
    "engine-invalidation-close-started",
    "dispose-returned",
    "late-submit-rejected",
    "engine-close-complete",
)
_U64 = (1 << 64) - 1


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _same(actual: Any, expected: Any, label: str) -> None:
    """Compare JSON recursively without accepting bool as int or extra fields."""
    if isinstance(expected, dict):
        _require(type(actual) is dict and actual.keys() == expected.keys(), label)
        for key, value in expected.items():
            _same(actual[key], value, f"{label}.{key}")
    elif isinstance(expected, list):
        _require(type(actual) is list and len(actual) == len(expected), label)
        for index, value in enumerate(expected):
            _same(actual[index], value, f"{label}[{index}]")
    else:
        _require(type(actual) is type(expected) and actual == expected, label)


def _integer(value: Any, maximum: int, label: str) -> int:
    _require(type(value) is int and 0 < value <= maximum, f"invalid {label}")
    return value


def _id(value: str, *, zero: bool = False) -> str:
    _require(
        re.fullmatch(r"(?:0|[1-9][0-9]{0,19})" if zero else r"[1-9][0-9]{0,19}", value)
        is not None,
        "noncanonical u64",
    )
    _require(0 <= int(value) <= _U64 and (zero or int(value) > 0), "invalid u64")
    return value


def _uuid(value: str, label: str) -> None:
    _require(type(value) is str, f"invalid {label}")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as error:
        raise RuntimeError(f"invalid {label}") from error
    _require(canonical == value, f"noncanonical {label}")


def _time(value: Any) -> float:
    _require(type(value) in (int, float), "monotonic time must be numeric, not bool")
    try:
        finite = math.isfinite(value)
    except OverflowError as error:
        raise RuntimeError("monotonic time is not finite") from error
    _require(finite and value >= 0, "invalid monotonic time")
    return value


def _ordered(indices: list[int], label: str) -> None:
    _require(all(a < b for a, b in pairwise(indices)), f"out of order: {label}")


def _runtime(
    text: str, run_id: str, service_uuid: str, *, platform: str = "android"
) -> dict[str, Any]:
    _require(type(text) is str, "runtime text must be str")
    lines = text.splitlines()
    _require(bool(lines), "missing runtime ledger")
    discovery_offset, read_offset, gatt_offset = physical.route_offsets(platform)
    facts = physical.RuntimeFacts(run_id, platform=platform)
    for line in lines:
        facts.observe(line)
    bodies = [line[len(physical.PREFIX) :] for line in lines]
    consumed: set[int] = set()

    def one(expression: str) -> tuple[int, re.Match[str]]:
        matches = [
            (index, match)
            for index, body in enumerate(bodies)
            if (match := re.fullmatch(expression, body)) is not None
        ]
        _require(
            len(matches) == 1, f"expected exactly one runtime marker: {expression}"
        )
        consumed.add(matches[0][0])
        return matches[0]

    generations = []
    for generation, base in ((1, 12000), (2, 13000)):
        prefix = f"generation={generation}:"

        def marker(expression: str, prefix: str = prefix) -> tuple[int, re.Match[str]]:
            return one(prefix + expression)

        start, _ = marker(rf"runtime-start:runId={re.escape(run_id)}:nonce=[^:\s]+")
        arm_listener, _ = marker("arm-linking-listener:registered-before-initialize")
        listener, _ = marker("listener:registered-before-initialize")
        ready, _ = marker("native-ready:passed")
        initialize, _ = marker(f"initialize:passed:commandId={base}")
        scan, scanned = marker(
            rf"scan:passed:serviceUuid={re.escape(service_uuid)}:"
            rf"peripheralId=(0|[1-9][0-9]*):scanSessionId={base + 1}"
        )
        peripheral = _id(scanned[1], zero=platform == "ios")
        connected, _ = marker(
            f"connected:passed:commandId={base + 5}:connectionId={base + 4}:"
            f"peripheralId={peripheral}"
        )
        gatt, _ = marker(
            f"gatt:passed:commandId={base + discovery_offset}:connectionId={base + 4}:gattSessionId={base + gatt_offset}"
        )
        awaiting, awaited = marker(
            f"awaiting-arm:commandId={base + read_offset}:connectionId={base + 4}:"
            rf"gattSessionId={base + gatt_offset}:characteristicId=([1-9][0-9]*)"
        )
        characteristic = _id(awaited[1])
        route = facts.routes[generation]
        arm, _ = marker("arm-js-observed:source=linking:url=matched")
        dispatch, _ = marker(
            "read-dispatched:"
            + re.escape(route)
            + (f":peripheralId={peripheral}" if generation == 1 else "")
        )
        indices = [
            start,
            arm_listener,
            listener,
            ready,
            initialize,
            scan,
            connected,
            gatt,
            awaiting,
            arm,
            dispatch,
        ]
        if generation == 2:
            read, _ = marker("read:passed:" + re.escape(route) + ":value=00")
            close, _ = marker("close:passed")
            indices.extend((read, close))
        _ordered(indices, f"generation {generation}")
        generations.append(
            {
                "generation": generation,
                "runtime_nonce": facts.nonces[generation],
                "characteristic_id": characteristic,
                "peripheral_id": peripheral,
                "read_route": route,
                "marker_indices": indices,
            }
        )

    native_indices = []
    for event in _NATIVE_ORDER:
        line = facts.seen.get("native:" + event)
        _require(line is not None, f"missing native event: {event}")
        index = lines.index(line)
        consumed.add(index)
        native_indices.append(index)
    _ordered(native_indices, "native close facts")
    old, new = generations
    _ordered(
        [
            old["marker_indices"][-2],
            native_indices[0],
            old["marker_indices"][-1],
            native_indices[1],
        ],
        "native acceptance/JS dispatch/engine invalidation",
    )
    _require(
        old["marker_indices"][-1] < new["marker_indices"][0],
        "generation 2 boot before old dispatch",
    )
    _require(
        native_indices[-1] < new["marker_indices"][3],
        "generation 2 native ready before old close complete",
    )
    # React teardown can report this, but beforeDestroy/native facts own disposal.
    cleanup = [
        i for i, body in enumerate(bodies) if body == "generation=1:cleanup-started"
    ]
    _require(len(cleanup) <= 1, "duplicate old JS cleanup")
    if cleanup:
        _require(
            old["marker_indices"][-1] < cleanup[0] < new["marker_indices"][3],
            "old JS cleanup out of order",
        )
        consumed.add(cleanup[0])
    passed, _ = one(f"passed:runId={re.escape(run_id)}")
    _require(
        passed == len(lines) - 1 and new["marker_indices"][-1] < passed,
        "final pass before new close or extra runtime markers",
    )
    _require(
        len(consumed) == len(lines), "unknown, extra, foreign or failed runtime marker"
    )
    return {
        "generations": generations,
        "probe_id": facts.probe_id,
        "native_close_facts": {
            "native:" + e: facts.seen["native:" + e] for e in _NATIVE_ORDER
        },
        "native_factual_order": [
            {"event": event, "marker_index": index, "marker": lines[index]}
            for event, index in zip(_NATIVE_ORDER, native_indices)
        ],
        "runtime_nonces": {str(k): v for k, v in facts.nonces.items()},
        "marker_count": len(lines),
    }


def _peer(value: Any) -> list[Any]:
    _require(type(value) is list and len(value) == 2, "invalid physical peer")
    _require(
        type(value[0]) is str and re.fullmatch(r"[0-9a-f]{12}", value[0]) is not None,
        "invalid peer address",
    )
    _require(
        type(value[1]) is int and value[1] in (0, 1, 2, 3), "invalid peer address type"
    )
    return value


def _boundaries(document: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    from tests.peripherals.nrf52840.fault_control import (
        Operation,
        Outcome,
        Phase,
        Ticket,
        Winner,
        cleanup_ready,
        decode_status,
        owner_nonce,
        read_completed,
        ticket_from_document,
    )

    records = document["boundaries"]
    _require(type(records) is list and bool(records), "missing physical boundaries")
    common = {"boundary", "generation", "monotonic", "peer"}
    identity_fields = {"wire_hex", "sequence", "handle"}
    times = []
    for record in records:
        _require(
            type(record) is dict and common <= record.keys(),
            "malformed physical boundary",
        )
        _require(
            type(record["generation"]) is int and record["generation"] in (1, 2),
            "invalid boundary generation",
        )
        times.append(_time(record["monotonic"]))
    _require(all(a <= b for a, b in pairwise(times)), "physical monotonic rewind")
    _require(
        times[-1] - times[0] < physical.WHOLE_SECONDS, "physical whole budget exceeded"
    )
    consumed: set[int] = set()

    def one(generation: int, boundary: str, extra: set[str], *, ticket: bool = True):
        matches = [
            (i, r)
            for i, r in enumerate(records)
            if r["generation"] == generation and r["boundary"] == boundary
        ]
        _require(
            len(matches) == 1, f"expected exactly one physical {generation}:{boundary}"
        )
        index, record = matches[0]
        _require(
            record.keys() == common | extra | (identity_fields if ticket else set()),
            f"unexpected fields: {boundary}",
        )
        consumed.add(index)
        return index, record

    def identity(record: dict[str, Any]) -> Ticket:
        value = ticket_from_document({key: record[key] for key in identity_fields})
        _require(
            value.operation is Operation.READ
            and value.handle == document["target_handle"],
            "wrong read fault target",
        )
        _require(
            value.owner_nonce == owner_nonce(document["run_id"]),
            "foreign fault owner UUID",
        )
        return value

    def receipt(record: dict[str, Any], ticket: Ticket, peer: list):
        raw = record["status_wire"]
        _require(
            type(raw) is str and raw == raw.lower(), "noncanonical STATUS evidence"
        )
        status = decode_status(bytes.fromhex(raw))
        _require(
            status.ticket == ticket and identity(record) == ticket,
            "STATUS full ticket drift",
        )
        _require(
            status.peer == bytes([peer[1]]) + bytes.fromhex(peer[0])[::-1],
            "STATUS exact peer drift",
        )
        _require(
            status.before == status.after == b"\x00", "before/after read value drift"
        )
        return status

    previous_ticket = None
    previous_end = -1
    summaries = []
    for generation in (1, 2):
        connected_i, connected = one(
            generation, "gap-connected", {"observed_peer"}, ticket=generation == 2
        )
        _same(connected["peer"], None, "connection peer")
        peer = _peer(connected["observed_peer"])
        if previous_ticket is not None:
            _require(
                identity(connected) == previous_ticket, "pre-arm retained ticket drift"
            )
        armed_i, armed = one(generation, "armed", {"armed_at", "route", "status_wire"})
        ticket = identity(armed)
        if previous_ticket is not None:
            _require(
                ticket.boot_nonce == previous_ticket.boot_nonce
                and ticket.sequence > previous_ticket.sequence,
                "boot drift or reused sequence",
            )
            _require(
                ticket.connection_epoch > previous_ticket.connection_epoch
                and ticket.bearer_epoch > previous_ticket.bearer_epoch,
                "connection/bearer reuse",
            )
        _same(
            armed["route"],
            runtime["generations"][generation - 1]["read_route"],
            "armed read route",
        )
        _require(
            receipt(armed, ticket, peer).phase is Phase.ARMED,
            "ARM receipt is not ARMED",
        )
        held_i, held = one(
            generation,
            "waiting",
            {"first_waiting_at", "read_dispatched", "status_wire"},
        )
        _require(
            receipt(held, ticket, peer).phase is Phase.HELD,
            "read was not captured HELD",
        )
        expected = (
            physical.PREFIX
            + f"generation={generation}:read-dispatched:"
            + armed["route"]
        )
        if generation == 1:
            expected += ":peripheralId=" + runtime["generations"][0]["peripheral_id"]
        _same(held["read_dispatched"], expected, "physical read dispatch/raw marker")
        retired_i, retired = one(generation, "retired", {"status_wire"})
        terminal = receipt(retired, ticket, peer)
        _require(
            cleanup_ready(terminal), "retained receipt is not resource-zero/rearmable"
        )
        gap_i, gap = one(generation, "gap-disconnected", {"observed_peer"})
        _require(identity(gap) == ticket, "disconnect ticket drift")
        _same(gap["observed_peer"], peer, "exact GAP disconnected peer")
        _same(gap["peer"], peer, "recorded GAP peer")
        _ordered(
            [previous_end, connected_i, armed_i, held_i],
            "physical connected/armed/held order",
        )
        for record in (armed, held, retired):
            _same(record["peer"], peer, "physical peer drift")
        armed_at, held_at = _time(armed["armed_at"]), _time(held["first_waiting_at"])
        _require(
            connected["monotonic"]
            <= armed_at
            <= armed["monotonic"]
            <= held_at
            <= held["monotonic"],
            "arm/held clock anchors",
        )
        _require(
            armed["monotonic"] - connected["monotonic"] < physical.PHASE_SECONDS,
            "pre-arm phase budget exceeded",
        )
        _require(
            held["monotonic"] - armed_at < physical.ARM_SECONDS,
            "arm to held budget exceeded",
        )
        if generation == 1:
            dispose_i, disposed = one(1, "native-dispose-observed", {"probe_id"})
            _same(disposed["probe_id"], runtime["probe_id"], "physical dispose probe")
            _require(
                identity(disposed) == ticket and disposed["peer"] == peer,
                "dispose ticket/peer drift",
            )
            _ordered([held_i, dispose_i, retired_i], "invalidation receipt order")
            _require(
                held_i < gap_i < retired_i,
                "invalidation must disconnect before retirement",
            )
            _require(
                terminal.winner is Winner.DISCONNECT
                and terminal.outcome is Outcome.DISCONNECTED_UNCOMMITTED,
                "invalidation outcome is not disconnected/uncommitted",
            )
            _require(
                retired["monotonic"] - held_at < physical.NATIVE_CLOSE_SECONDS,
                "native close budget exceeded",
            )
            recovered_i, recovered = one(1, "advertising-recovered", set())
            _require(
                identity(recovered) == ticket and recovered["peer"] is None,
                "recovery retained ticket/peer drift",
            )
            _require(
                retired_i < recovered_i, "advertising before resource-zero retirement"
            )
            end = recovered_i
        else:
            before_i, before = one(2, "before-resolve", {"status_wire"})
            _require(
                receipt(before, ticket, peer).phase is Phase.HELD,
                "resolve requires fresh HELD",
            )
            request_i, request = one(2, "resolve-requested", {"resolve_at"})
            _require(
                identity(request) == ticket and request["peer"] == peer,
                "resolve ticket/peer drift",
            )
            _ordered([held_i, before_i, request_i, retired_i], "read resolution order")
            _require(request_i < gap_i, "disconnect before resolution request")
            resolve_at = _time(request["resolve_at"])
            _require(
                before["monotonic"]
                <= resolve_at
                <= request["monotonic"]
                <= retired["monotonic"],
                "resolve clock anchor",
            )
            _require(
                request["monotonic"] - held_at < physical.NATIVE_CLOSE_SECONDS,
                "native close budget exceeded",
            )
            _require(
                retired["monotonic"] - resolve_at < physical.RELEASE_SECONDS,
                "resolve retirement budget exceeded",
            )
            _require(
                read_completed(terminal),
                "normal read must be NORMAL_NO_COMMIT with ordered response",
            )
            end = max(gap_i, retired_i)
        _require(
            records[end]["monotonic"] - retired["monotonic"] < physical.PHASE_SECONDS,
            "post-retirement phase budget exceeded",
        )
        summaries.append(
            {
                "generation": generation,
                **{k: retired[k] for k in identity_fields},
                "peer": peer,
                "armed_at": armed_at,
                "retired_at": retired["monotonic"],
                "gap_disconnected_at": gap["monotonic"],
            }
        )
        previous_ticket, previous_end = ticket, end
    cleanup_i, cleanup = one(
        2,
        "physical-cleanup",
        {
            "hold_inactive",
            "hold_state",
            "no_known_peers",
            "advertising_stopped",
            "status_wire",
        },
    )
    closed_i, closed = one(2, "closed", {"hold_inactive", "peer_count"})
    _ordered([previous_end, cleanup_i, closed_i], "final physical cleanup")
    _require(
        identity(cleanup) == identity(closed) == previous_ticket, "final ticket drift"
    )
    _require(
        cleanup_ready(receipt(cleanup, previous_ticket, summaries[-1]["peer"])),
        "final resources not zero",
    )
    _same(
        {key: cleanup[key] for key in document["physical_cleanup"]},
        document["physical_cleanup"],
        "cleanup boundary",
    )
    for record in (cleanup, closed):
        _same(record["hold_inactive"], True, "final inactive owners")
        _same(record["peer"], summaries[-1]["peer"], "final recorded peer")
    _same(closed["peer_count"], 0, "final peer count")
    _require(
        closed["monotonic"] - records[previous_end]["monotonic"]
        < physical.PHASE_SECONDS,
        "cleanup phase budget exceeded",
    )
    _require(
        len(consumed) == len(records) and closed_i == len(records) - 1,
        "extra failed/unknown physical boundary",
    )
    return {"generations": summaries, "physical_closed_at": closed["monotonic"]}


def verify(
    text: str,
    *,
    run_id: str,
    service_uuid: str,
    physical_path: Path,
    fixture_request: Path,
    fixture_identity: tuple[int, int],
    host_actions: tuple[str, ...],
    platform: str = "android",
) -> dict:
    """Validate current runtime/physical/session facts; return a JSON-safe ledger.

    ``host_actions`` must come from successful parent ADB commands, never JS.
    Receipt checking is read-only, not an independent PID/lease-liveness probe.
    Exceptions reject the attempt; no partial passing result is returned.
    """
    _uuid(run_id, "run_id")
    _uuid(service_uuid, "service_uuid")
    _require(
        type(host_actions) is tuple and host_actions == ("arm-1", "reload", "arm-2"),
        "expected confirmed host actions arm-1/reload/arm-2",
    )
    _require(
        type(fixture_identity) is tuple and len(fixture_identity) == 2,
        "invalid fixture identity",
    )
    for value in fixture_identity:
        _integer(value, (1 << 63) - 1, "fixture PID/PGID")
    plan = require_multifunction(session.load_session(fixture_request))
    _require(
        plan.session_id == run_id and plan.service_uuid == service_uuid,
        "foreign fixture run/service",
    )
    receipt = session.verify_receipt(plan, fixture_identity=fixture_identity)
    profile = session.load_session_profile(plan.contract, plan.profile_path)
    rw = next(c for c in profile.characteristics if c.role == "read-write")
    _require(
        rw.permissions == 0x03 and rw.initial_value == b"\x00",
        "not ordinary RW/00 profile",
    )
    native_identity = None
    runtime_text = text
    if platform == "ios":
        from tools.react_native import pending_reload_identity

        native_identity = pending_reload_identity.verify(text, run_id)
        runtime_text = "".join(
            line + "\n"
            for line in text.splitlines()
            if line.startswith(physical.PREFIX)
        )
    runtime = _runtime(runtime_text, run_id, service_uuid, platform=platform)
    document = session._json(session._read_bytes(physical_path))
    expected = {
        "schema_version": 2,
        "status": "passed",
        **({"platform": "ios"} if platform == "ios" else {}),
        "run_id": run_id,
        "session_id": run_id,
        "fixture_pid": fixture_identity[0],
        "fixture_pgid": fixture_identity[1],
        "profile_id": profile.profile_id,
        "contract_sha256": plan.contract_sha256,
        "profile_sha256": plan.profile_sha256,
        "resource_identity": plan.resource_identity,
        "live_receipt_path": str(plan.receipt_path),
        "port": plan.port,
        "service_uuid": service_uuid,
        "target_uuid": rw.uuid,
        "target_permissions": {
            "profile": 0x03,
            "expected_native": 0x43,
            "native": 0x43,
        },
        "cleanup_errors": [],
        "native_close_facts": runtime["native_close_facts"],
        "runtime_nonces": runtime["runtime_nonces"],
        "physical_cleanup": {
            "status_wire": document["physical_cleanup"].get("status_wire"),
            "hold_inactive": True,
            "hold_state": "RETIRED",
            "no_known_peers": True,
            "advertising_stopped": True,
        },
    }
    _require(
        set(document)
        == set(expected) | {"target_handle", "boundaries", "budgets_seconds"},
        "physical schema keys",
    )
    for key, value in expected.items():
        _same(document[key], value, f"physical {key}")
    _integer(document["target_handle"], 0xFFFF, "target ATT handle")
    budgets = {
        "whole": physical.WHOLE_SECONDS,
        "phase": physical.PHASE_SECONDS,
        "arm_to_held": physical.ARM_SECONDS,
        "resolve_to_retired": physical.RELEASE_SECONDS,
        "native_close": physical.NATIVE_CLOSE_SECONDS,
    }
    _require(
        type(document["budgets_seconds"]) is dict
        and document["budgets_seconds"].keys() == budgets.keys(),
        "physical budget keys",
    )
    for key, value in budgets.items():
        _require(
            _time(document["budgets_seconds"][key]) == value,
            f"physical budget mismatch: {key}",
        )
    physical_facts = _boundaries(document, runtime)
    return {
        "schema_version": 1,
        "status": "passed",
        "mode": physical.IOS_MODE if platform == "ios" else physical.MODE,
        **(
            {"platform": "ios", "native_identity": native_identity}
            if platform == "ios"
            else {}
        ),
        "run_id": run_id,
        "service_uuid": service_uuid,
        "host_actions": list(host_actions),
        "old_characteristic_id": runtime["generations"][0]["characteristic_id"],
        "new_characteristic_id": runtime["generations"][1]["characteristic_id"],
        **runtime,
        "fixture": {
            "pid": fixture_identity[0],
            "pgid": fixture_identity[1],
            "request_path": str(fixture_request),
            "receipt_path": str(plan.receipt_path),
            "receipt": receipt,
            "contract_sha256": plan.contract_sha256,
            "profile_sha256": plan.profile_sha256,
            "resource_identity": plan.resource_identity,
            "profile_id": profile.profile_id,
            "target_uuid": rw.uuid,
            "target_handle": document["target_handle"],
            "target_permissions": document["target_permissions"],
        },
        "physical": {
            "path": str(physical_path),
            "budgets_seconds": budgets,
            **physical_facts,
        },
        "explicit_limits": {
            "pid_provenance": "Parent owns the console and official install/process/payload receipts; native PID/boot/owners are checked, not an installed-binary hash."
            if platform == "ios"
            else "Parent owns same PINNED PID logcat capture and foreign-PID rejection; normalized text cannot independently prove its PID source.",
            "host_actions": "Caller owns successful SDK payload URL receipts; native and JS acknowledgements are separately checked."
            if platform == "ios"
            else "Caller attests successful ADB commands; JS arm acknowledgement is not host command proof.",
            "late_submit": "Synthetic old-handoff CLOSED rejection only; no observed late CoreBluetooth callback is claimed."
            if platform == "ios"
            else "Synthetic old-handoff CLOSED rejection only; no observed late BluetoothGattCallback is claimed.",
            "generation_2_events": "JS guards old-route/error events and post-read silence; individual native event bodies are not recorded or independently audited here.",
            "physical_semantics": "Audit of provider records and current cooperative live receipt, not independent BLE/BTP replay or signed attestation.",
            "timing": "Recorded monotonic spans satisfy declared budgets; unrecorded phase starts and intermediate samples remain provider-owned.",
            "liveness": "Receipt revalidated before fixture stop; parent owns current process/lease liveness and capture/bundle bindings.",
        },
    }
