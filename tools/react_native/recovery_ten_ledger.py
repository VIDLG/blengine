"""Read-only recovery-ten gate, called by the parent before stopping the fixture.

The wire authority is rf_recovery_ten.tsx, rf_contract.ts and the provider's
recovery_ten.py, not threadtime prefixes or inferred native request IDs. One App /
JS runtime contains ten fresh Hub/Probe lifecycles. This audits cooperative source
records; it neither runs RF nor proves OS memory, PID provenance or signatures.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from itertools import pairwise
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840 import fault_control as fault
from tests.peripherals.nrf52840 import fixture_session as session
from tests.peripherals.nrf52840 import recovery_ten as physical
from tests.peripherals.nrf52840.cancellation_control import require_multifunction
from tests.peripherals.nrf52840.profile import PROPERTY_BITS, ProfilePlan

_COMMANDS = (0, 2, 3, 5, 7, 9, 11, 13, 15, 12)
_CCC = "00002902-0000-1000-8000-00805f9b34fb"
_PROPERTIES = {
    "readable": "read",
    "writableWithResponse": "write",
    "writableWithoutResponse": "write_without_response",
    "notifiable": "notify",
    "indicatable": "indicate",
}
# rf_contract.ts EXPECTED_RF_GATT_CHARACTERISTICS, also checked against the
# current hash-bound profile (never a default fixture name/service assumption).
_SHAPE = {
    "read-write": ("fff10000", 0x0E),
    "updates": ("fff20000", 0x32),
    "indications": ("fff50000", 0x22),
    "read-only": ("fff30000", 0x02),
    "write-only": ("fff40000", 0x0C),
}
_EVENT_FIELDS = {
    "kind",
    "commandId",
    "scanSessionId",
    "peripheralId",
    "connectionId",
    "connectionState",
    "gattSessionId",
    "characteristicId",
    "subscriptionId",
    "gattServices",
    "value",
    "notificationState",
    "actualNotificationMode",
    "rssi",
    "localName",
    "advertisedServices",
    "adapterState",
    "scanAuthorization",
    "error",
    "runId",
    "nonce",
    "cycle",
}


def _require(condition: bool, label: str) -> None:
    if not condition:
        raise RuntimeError(label)


def _same(actual: Any, expected: Any, label: str) -> None:
    """JSON equality with exact keys/types; bool is never a count or identity."""
    _require(type(actual) is type(expected), label)
    if type(expected) is dict:
        _require(actual.keys() == expected.keys(), label)
        for key in expected:
            _same(actual[key], expected[key], f"{label}.{key}")
    elif type(expected) is list:
        _require(len(actual) == len(expected), label)
        for left, right in zip(actual, expected, strict=True):
            _same(left, right, label)
    else:
        _require(actual == expected, label)


def _integer(value: Any, maximum: int, label: str) -> int:
    _require(type(value) is int and 0 < value <= maximum, label)
    return value


def _id(value: Any, *, positive: bool = False) -> str:
    _require(
        type(value) is str
        and re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", value) is not None,
        "noncanonical allocated u64 ID",
    )
    _require(
        int(value) <= (1 << 64) - 1 and (not positive or int(value) > 0), "u64 range"
    )
    return value


def _uuid(value: Any) -> None:
    _require(type(value) is str, "UUID must be text")
    try:
        _require(str(uuid.UUID(value)) == value, "noncanonical UUID")
    except ValueError as error:
        raise RuntimeError("invalid UUID") from error


def _time(value: Any) -> float:
    _require(type(value) in (int, float), "time must be numeric, not bool")
    try:
        _require(math.isfinite(value) and value >= 0, "nonfinite/negative time")
    except OverflowError as error:
        raise RuntimeError("nonfinite time") from error
    return value


def _ordered(indices: list[int], label: str) -> None:
    _require(all(a < b for a, b in pairwise(indices)), f"out of order: {label}")


def _gatt(event: dict, profile: ProfilePlan) -> dict[str, str]:
    services = event.get("gattServices")
    _require(type(services) is list and len(services) == 1, "projected fixture service")
    service = services[0]
    _require(type(service) is dict, "GATT service must be object")
    _same(set(service), {"id", "uuid", "primary", "characteristics"}, "service keys")
    _same(service["uuid"], profile.service_uuid, "GATT service UUID")
    _same(service["primary"], True, "primary GATT service")
    ids = {_id(service["id"])}
    characteristics = service["characteristics"]
    _require(
        type(characteristics) is list and len(characteristics) == 5,
        "five characteristics",
    )
    roles = {}
    for declared in profile.characteristics:
        matches = [
            c
            for c in characteristics
            if type(c) is dict and c.get("uuid") == declared.uuid
        ]
        _require(len(matches) == 1, "missing/duplicate declared characteristic UUID")
        characteristic = matches[0]
        _same(
            set(characteristic),
            {"id", "uuid", "descriptors", *_PROPERTIES},
            "characteristic keys",
        )
        allocated = _id(characteristic["id"])
        _require(allocated not in ids, "duplicate database ID")
        ids.add(allocated)
        roles[declared.role] = allocated
        for name, prop in _PROPERTIES.items():
            _same(
                characteristic[name],
                bool(declared.properties & PROPERTY_BITS[prop]),
                f"GATT {name}",
            )
        descriptors = characteristic["descriptors"]
        _require(
            type(descriptors) is list
            and len(descriptors) == int(declared.requires_ccc),
            "CCCD count",
        )
        for descriptor in descriptors:
            _require(type(descriptor) is dict, "descriptor object")
            _same(set(descriptor), {"id", "uuid"}, "descriptor keys")
            _same(descriptor["uuid"], _CCC, "CCCD UUID")
            allocated = _id(descriptor["id"])
            _require(allocated not in ids, "duplicate descriptor ID")
            ids.add(allocated)
    return roles


def _cycle_events(
    cycle: int,
    events: list[tuple[int, dict]],
    markers: dict,
    profile: ProfilePlan,
    platform: str,
) -> dict:
    base = 10000 + cycle * 100
    ident = lambda offset: str(base + offset)
    commands = {ident(offset) for offset in _COMMANDS}
    steps = markers["steps"]
    positions = markers["positions"]
    consumed: set[int] = set()

    def one(kind: str, offset: int | None, **fields: Any) -> tuple[int, dict]:
        command = None if offset is None else ident(offset)
        matches = [
            (i, e)
            for i, e in events
            if e["kind"] == kind
            and e.get("commandId") == command
            and all(e.get(k) == v for k, v in fields.items())
        ]
        _require(
            len(matches) == 1, f"cycle {cycle}: exactly one {kind}/{command}/{fields}"
        )
        i, event = matches[0]
        consumed.add(i)
        for key, value in fields.items():
            _same(event.get(key), value, f"event {key}")
        return i, event

    def window(indices: list[int], left: str, right: str) -> None:
        _ordered(
            [positions[left], *indices, positions[right]],
            f"cycle {cycle}: {left}/{right}",
        )

    def step(name: str, fields: str) -> None:
        _same(steps[name], fields, f"cycle {cycle}: {name} fields")

    routes = {
        "scanSessionId": [ident(1)],
        "connectionId": [ident(4), ident(10)],
        "gattSessionId": [ident(6), ident(14)],
        "subscriptionId": [ident(8)],
    }
    for _, event in events:
        _require(
            set(event) <= _EVENT_FIELDS and type(event.get("kind")) is str,
            "unknown event fields/kind",
        )
        for key in ("commandId", "peripheralId", "characteristicId", *routes):
            if event.get(key) is not None:
                _id(event[key])
        _require(
            event.get("commandId") is None or event["commandId"] in commands,
            "foreign/out-of-range command ID",
        )
        for key, allowed in routes.items():
            _require(
                event.get(key) is None or event[key] in allowed,
                f"old-cycle/foreign {key}",
            )
        # Null filler fields are native projection details; non-null fields must
        # belong to this event variant, not a spliced foreign route/body.
        allowed = {
            "adapterSnapshotChanged": {
                "commandId",
                "adapterState",
                "scanAuthorization",
            },
            "scanStarted": {"commandId", "scanSessionId"},
            "scanStopped": {"commandId", "scanSessionId"},
            "advertisementObserved": {
                "scanSessionId",
                "peripheralId",
                "localName",
                "advertisedServices",
                "rssi",
            },
            "connectionStateChanged": {
                "commandId",
                "connectionId",
                "connectionState",
                "error",
            },
            "gattDiscovered": {
                "commandId",
                "connectionId",
                "gattSessionId",
                "gattServices",
            },
            "notificationStateChanged": {
                "commandId",
                "connectionId",
                "gattSessionId",
                "subscriptionId",
                "characteristicId",
                "notificationState",
                "actualNotificationMode",
            },
            "notificationReceived": {
                "connectionId",
                "gattSessionId",
                "subscriptionId",
                "characteristicId",
                "value",
            },
            "characteristicRead": {
                "commandId",
                "connectionId",
                "gattSessionId",
                "characteristicId",
                "value",
            },
            "commandCompleted": {"commandId"},
        }
        _require(event["kind"] in allowed, "unknown/unexpected native event")
        _require(
            all(
                v is None
                or k in allowed[event["kind"]] | {"kind", "runId", "nonce", "cycle"}
                for k, v in event.items()
            ),
            "foreign fields on event variant",
        )
        passive = (
            event["kind"] == "connectionStateChanged" and event.get("commandId") is None
        )
        _require(
            event.get("error") is None or passive,
            "unexpected native error",
        )

    terminals = {}
    for offset in _COMMANDS:
        terminals[offset] = one("commandCompleted", offset)[0]
    adapters = [(i, e) for i, e in events if e["kind"] == "adapterSnapshotChanged"]
    _require(bool(adapters), "missing initialized adapter root")
    _require(
        any(
            e.get("adapterState") == "PoweredOn"
            and e.get("scanAuthorization") == "Authorized"
            for _, e in adapters
        ),
        "adapter not RF ready",
    )
    for i, event in adapters:
        _require(
            event.get("commandId") in (None, ident(0)),
            "adapter command association must be initialize",
        )
        _require(
            positions["listener"] < i < terminals[0],
            "adapter root outside initialization/before terminal",
        )
        _require(
            event.get("adapterState")
            in ("Unknown", "Unavailable", "PoweredOff", "PoweredOn")
            and event.get("scanAuthorization")
            in ("NotRequired", "NotDetermined", "Restricted", "Denied", "Authorized"),
            "unknown adapter snapshot",
        )
        consumed.add(i)
    window([terminals[0]], "listener", "initialize")
    step("initialize", f"commandId={ident(0)}")
    scan = one("scanStarted", 2, scanSessionId=ident(1))[0]
    stop = one("scanStopped", 3, scanSessionId=ident(1))[0]
    window([scan, terminals[2], stop, terminals[3]], "baseline", "scan")
    if platform == "android":
        _ordered(
            [
                positions[name]
                for name in (
                    "baseline",
                    "scan-budget-wait",
                    "scan-budget-dispatch",
                    "scan-budget-started",
                    "scan",
                )
            ],
            f"cycle {cycle}: scan budget scheduling",
        )
        window([scan, terminals[2]], "scan-budget-dispatch", "scan-budget-started")
        window([stop, terminals[3]], "scan-budget-started", "scan")
    advertisements = [(i, e) for i, e in events if e["kind"] == "advertisementObserved"]
    _require(bool(advertisements), "missing fixture advertisement")
    peripheral = _id(advertisements[0][1].get("peripheralId"))
    for i, event in advertisements:
        _require(scan < i < stop, "advertisement before scan/after stop")
        _same(event.get("scanSessionId"), ident(1), "advertisement scan route")
        _same(event.get("peripheralId"), peripheral, "single fixture peripheral")
        rssi = event.get("rssi")
        _require(
            rssi is None or type(rssi) is int and -32768 <= rssi <= 32767,
            "advertisement RSSI must be an optional i16",
        )
        services = event.get("advertisedServices")
        _require(
            type(services) is list
            and 0 < len(services) <= 128
            and profile.service_uuid in services,
            "advertised fixture service",
        )
        for service in services:
            _uuid(service)
        # The parent freezes both the configured name and whether it is required.
        local_name = event.get("localName")
        if profile.require_local_name or local_name is not None:
            _same(local_name, profile.local_name, "configured fixture local name")
        consumed.add(i)
    step(
        "scan",
        f"scanSessionId={ident(1)}:startCommandId={ident(2)}:stopCommandId={ident(3)}:peripheralId={peripheral}:serviceUuid={profile.service_uuid}:silenceMs=2000",
    )
    databases = {}
    for offset, route, left, name in (
        (5, 4, "scan", "connected"),
        (11, 10, "recovery-ready", "recovery-connected"),
    ):
        connecting = one(
            "connectionStateChanged",
            offset,
            connectionId=ident(route),
            connectionState="Connecting",
        )[0]
        connected = one(
            "connectionStateChanged",
            offset,
            connectionId=ident(route),
            connectionState="Connected",
        )[0]
        window([connecting, connected, terminals[offset]], left, name)
        step(
            name,
            f"commandId={ident(offset)}:connectionId={ident(route)}:peripheralId={peripheral}",
        )
        discover = offset + 2
        gatt_route = 6 if offset == 5 else 14
        gatt_name = "gatt" if offset == 5 else "recovery-gatt"
        i, event = one(
            "gattDiscovered",
            discover,
            connectionId=ident(route),
            gattSessionId=ident(gatt_route),
        )
        databases[gatt_name] = _gatt(event, profile)
        window([i, terminals[discover]], name, gatt_name)
        step(
            gatt_name,
            f"commandId={ident(discover)}:connectionId={ident(route)}:gattSessionId={ident(gatt_route)}:serviceCount=1:characteristicCount=5:descriptorCount=2",
        )
    updates = _id(databases["gatt"]["updates"], positive=True)
    common = {
        "connectionId": ident(4),
        "gattSessionId": ident(6),
        "subscriptionId": ident(8),
        "characteristicId": updates,
    }
    enabled = one(
        "notificationStateChanged",
        9,
        **common,
        notificationState="Enabled",
        actualNotificationMode="Notification",
    )[0]
    window([enabled, terminals[9]], "gatt", "notification-enable")
    step(
        "notification-enable",
        f"commandId={ident(9)}:connectionId={ident(4)}:gattSessionId={ident(6)}:subscriptionId={ident(8)}:characteristicId={updates}:actualMode=Notification",
    )
    i, event = one("notificationReceived", None, **common)
    _same(event.get("value"), [239], "exact EF notification")
    window([i], "active", "notification-value")
    passive_i, event = one("connectionStateChanged", None, connectionId=ident(4))
    passive_error = physical.passive_disconnect_error(event, ident(4), platform)
    step(
        "passive-disconnect",
        f"commandId=none:connectionId={ident(4)}:error={passive_error}",
    )
    window([passive_i], "notification-value", "passive-disconnect")
    _require(
        positions["native-signal"] < positions["recovery-ready"],
        "JS ACK before native signal",
    )
    rw = _id(databases["recovery-gatt"]["read-write"], positive=True)
    i, event = one(
        "characteristicRead",
        15,
        connectionId=ident(10),
        gattSessionId=ident(14),
        characteristicId=rw,
    )
    _same(event.get("value"), [0], "exact remote recovery read 00")
    window([i, terminals[15]], "recovery-gatt", "recovery-read")
    step(
        "recovery-read",
        f"commandId={ident(15)}:connectionId={ident(10)}:gattSessionId={ident(14)}:characteristicId={rw}:value=00",
    )
    disconnected = one(
        "connectionStateChanged",
        12,
        connectionId=ident(10),
        connectionState="Disconnected",
    )[0]
    disconnecting = [
        (i, e)
        for i, e in events
        if e["kind"] == "connectionStateChanged"
        and e.get("commandId") == ident(12)
        and e.get("connectionState") == "Disconnecting"
    ]
    _require(len(disconnecting) <= 1, "duplicate optional Disconnecting")
    indices = []
    for i, event in disconnecting:
        _same(event.get("connectionId"), ident(10), "logical Disconnecting route")
        consumed.add(i)
        indices.append(i)
    window(
        [*indices, disconnected, terminals[12]],
        "recovery-read",
        "recovery-disconnected",
    )
    _require(
        len(consumed) == len(events),
        "duplicate, late, foreign or unconsumed native event",
    )
    return {
        "cycle": cycle,
        "peripheral_id": peripheral,
        "command_ids": [ident(o) for o in _COMMANDS],
        "route_ids": routes,
        "native_attribute_ids": databases,
        "terminal_count": 10,
        "event_count": len(events),
        "passive_event_index": passive_i,
        "marker_indices": positions,
    }


def _runtime(
    text: str, run_id: str, profile: ProfilePlan, platform: str
) -> tuple[physical.RuntimeFacts, list[dict]]:
    _require(
        type(text) is str and text.endswith("\n"),
        "complete normalized runtime text required",
    )
    _require(
        len(text.encode("utf-8")) <= 8 * 1024 * 1024, "runtime text exceeds budget"
    )
    lines = text.splitlines()
    facts = physical.RuntimeFacts(run_id, profile.service_uuid)
    markers = [{"positions": {}, "steps": {}} for _ in range(10)]
    events: dict[int, list[tuple[int, dict]]] = {c: [] for c in range(10)}
    for index, line in enumerate(lines):
        _require(
            line.startswith(physical.PREFIX), "non-normalized/foreign runtime line"
        )
        body = line[len(physical.PREFIX) :]
        if body.startswith("native:signal:"):
            # host_cycles is separately checked against parent-confirmed SDK
            # actions. Text alone cannot provide physical authorization.
            facts.authorized = facts.cycle
        facts.observe(line)
        cycle = facts.cycle
        if body.startswith("native:"):
            if body.startswith("native:bootstrap:"):
                _same(facts.bootstrap["source"], "launch", "native bootstrap source")
                _same(
                    physical.BUNDLES[facts.bootstrap["bundle_id"]],
                    platform,
                    "native platform",
                )
            else:
                _same(
                    facts.signals[cycle]["source"],
                    "payload-url",
                    "native signal source",
                )
                markers[cycle]["positions"]["native-signal"] = index
        elif body.startswith("event:"):
            _require(
                0 <= cycle < 10
                and f"{cycle}:listener" in facts.seen
                and f"{cycle}:closed" not in facts.seen,
                "event outside live Probe listener",
            )
            events[cycle].append((index, session._json(body[6:].encode())))
        elif body == "listener:registered-before-initialize":
            markers[cycle]["positions"]["listener"] = index
        elif body.startswith("resources:"):
            phase = re.fullmatch(
                r"resources:cycle=[0-9]:phase=([^:]+):snapshot=.+", body
            )[1]
            markers[cycle]["positions"][phase] = index
        elif body.startswith("cycle:"):
            tail = body.split(":", 2)[2]
            name = tail.split(":", 1)[0]
            markers[cycle]["positions"][name] = index
            if ":passed:" in tail:
                markers[cycle]["steps"][name] = tail.split(":passed:", 1)[1]
        if "passed" in facts.seen:
            _require(index == len(lines) - 1, "extra records after final pass")
    _require(
        facts.cycle == 9
        and {"runtime-start", "close", "runtime-close", "passed"} <= facts.seen.keys(),
        "incomplete ten-cycle runtime",
    )
    _same(sorted(facts.signals), list(range(10)), "ten native signals")
    _same(sorted(facts.resources), list(range(10)), "ten snapshot cycles")
    cycles = []
    for cycle in range(10):
        _same(
            list(facts.resources[cycle]),
            list(physical.RESOURCE_PHASES),
            "five ordered resource phases",
        )
        _require(f"{cycle}:passed" in facts.seen, "missing cycle pass/closed")
        summary = _cycle_events(cycle, events[cycle], markers[cycle], profile, platform)
        summary["resources"] = facts.resources[cycle]
        cycles.append(summary)
    return facts, cycles


def _hold(value: Any, ticket: fault.Ticket | None = None) -> None:
    if ticket is None:
        _same(
            value,
            {"protocol_version": 2, "engine_flags": 0},
            "v2 fault engine is not idle/resource-free",
        )
        return
    _require(type(value) is dict, "retained hold object")
    _same(
        set(value),
        {"protocol_version", "engine_flags", "boot_nonce", "retained_status"},
        "retained hold keys",
    )
    _same(value["protocol_version"], 2, "retained hold version")
    _same(value["engine_flags"], 0, "retained idle engine")
    _same(value["boot_nonce"], ticket.boot_nonce.hex(), "retained limits boot")
    status = fault.status_from_document(value["retained_status"])
    _require(
        status.ticket == ticket
        and fault.cleanup_ready(status)
        and status.winner is fault.Winner.DISCONNECT
        and status.outcome is fault.Outcome.DISCONNECTED_UNCOMMITTED,
        "retained checkpoint requires exact all-six-zero disconnect receipt",
    )


def _retained_samples(document: dict) -> fault.Ticket | None:
    if "retained_ticket" not in document:
        return None
    ticket = fault.ticket_from_document(document["retained_ticket"])
    _require(
        ticket.owner_nonce == fault.owner_nonce(document["session_id"])
        and ticket.operation is fault.Operation.READ
        and ticket.handle == document["handles"]["read-write"],
        "retained witness session/READ/handle binding",
    )
    samples = document["fault_samples"]
    _require(
        type(samples) is list and 0 < len(samples) <= physical.FAULT_SAMPLES_MAX,
        "bounded retained read-only samples required",
    )
    previous = 0.0
    for sample in samples:
        _require(type(sample) is dict, "retained sample object")
        _same(
            set(sample),
            {
                "cycle",
                "phase",
                "monotonic",
                "deadline",
                "status_opcode",
                "status_wire",
                "limits",
                "error",
            },
            "retained sample keys",
        )
        instant = _time(sample["monotonic"])
        _require(
            previous <= instant < _time(sample["deadline"]),
            "retained sample original deadline/clock",
        )
        previous = instant
        _require(
            type(sample["cycle"]) is int and -1 <= sample["cycle"] <= 9,
            "retained sample cycle",
        )
        _require(sample["phase"] in physical.PROGRESS_PHASES, "retained sample phase")
        _same(
            sample["error"], None, "successful recovery has no failed retained samples"
        )
        _same(
            sample["status_opcode"],
            int(fault.Opcode.STATUS),
            "exact retained STATUS response",
        )
        raw = sample["status_wire"]
        _require(type(raw) is str, "retained STATUS hex")
        status = fault.decode_status(bytes.fromhex(raw))
        _same(fault.encode_status(status).hex(), raw, "canonical retained STATUS")
        _require(
            status.ticket == ticket
            and status.phase is fault.Phase.RETIRED
            and status.winner is fault.Winner.DISCONNECT
            and status.outcome is fault.Outcome.DISCONNECTED_UNCOMMITTED
            and bool(status.flags & fault.StatusFlags.REARMABLE)
            and not status.flags & fault.StatusFlags.INFRASTRUCTURE_FAILED
            and status.infrastructure_error is fault.InfrastructureError.NONE
            and status.refs
            == fault.RefCounters(0, 0, 0, 0, 0, status.refs.evidence_pending),
            "only exact retired witness Tester evidence may drain",
        )
        limits = sample["limits"]
        _require(type(limits) is dict, "sampled retained limits")
        _same(
            set(limits),
            {
                "boot_nonce",
                "engine_flags",
                "peer_present",
                "peer",
                "connection_epoch",
                "bearer_epoch",
                "database_epoch",
            },
            "sampled limits keys",
        )
        _same(limits["boot_nonce"], ticket.boot_nonce.hex(), "sampled limits same boot")
        _require(
            type(limits["engine_flags"]) is int
            and limits["engine_flags"] in (0, int(fault.EngineFlags.BUSY)),
            "sampled limits not quarantined",
        )
        _require(
            type(limits["peer_present"]) is bool
            and type(limits["peer"]) is str
            and re.fullmatch(r"0[0-3][0-9a-f]{12}", limits["peer"]) is not None,
            "sampled limits peer wire",
        )
        epochs = [
            limits[k] for k in ("connection_epoch", "bearer_epoch", "database_epoch")
        ]
        _require(
            all(type(v) is int and 0 <= v <= 0xFFFFFFFF for v in epochs),
            "sampled limits epochs",
        )
        _require(
            all(v > 0 for v in epochs)
            if limits["peer_present"]
            else limits["peer"] == "00" * 7 and not any(epochs),
            "sampled limits peer/epochs binding",
        )
    _require(
        fault.cleanup_ready(status) and limits["engine_flags"] == 0,
        "last retained sample must reach all-six-zero and fresh idle limits",
    )
    _same(
        document["hold_status"]["retained_status"],
        fault.status_document(status),
        "final retained checkpoint is the latest sampled receipt",
    )
    return ticket


def _peer(value: Any) -> list:
    _require(type(value) is list and len(value) == 2, "physical peer schema")
    _require(
        type(value[0]) is str and re.fullmatch(r"[0-9a-f]{12}", value[0]) is not None,
        "physical peer address",
    )
    _require(
        type(value[1]) is int and value[1] in (0, 1, 2, 3), "physical peer address type"
    )
    return value


def _physical(document: dict, facts: physical.RuntimeFacts) -> dict:
    retained = _retained_samples(document)
    records = document["boundaries"]
    _require(type(records) is list and bool(records), "missing physical boundaries")
    for record in records:
        _require(
            type(record) is dict
            and {"boundary", "cycle", "monotonic"} <= record.keys(),
            "physical boundary schema",
        )
        _require(
            type(record["cycle"]) is int and -1 <= record["cycle"] <= 9,
            "physical boundary cycle",
        )
        _time(record["monotonic"])
    times = [r["monotonic"] for r in records]
    _require(all(a <= b for a, b in pairwise(times)), "physical monotonic rewind")
    _require(
        times[-1] - times[0] < physical.WHOLE_SECONDS, "absolute whole budget exceeded"
    )
    consumed = set()

    def one(cycle: int, name: str, fields: dict) -> tuple[int, dict]:
        matches = [
            (i, r)
            for i, r in enumerate(records)
            if r["cycle"] == cycle and r["boundary"] == name
        ]
        _require(len(matches) == 1, f"physical {cycle}:{name} coverage")
        i, record = matches[0]
        _same(
            set(record),
            {"boundary", "cycle", "monotonic", *fields},
            f"physical {name} keys",
        )
        for key, value in fields.items():
            _same(record[key], value, f"physical {name}.{key}")
        consumed.add(i)
        return i, record

    no_peer = {"hold_inactive": True, "peer_count": 0}
    previous, _ = one(-1, "prepared", no_peer)
    _require(previous == 0, "prepared must be first physical boundary")
    cycles = document["cycles"]
    _require(type(cycles) is list and len(cycles) == 10, "ten physical cycles")
    summaries = []
    for cycle, row in enumerate(cycles):
        _require(type(row) is dict, "physical cycle object")
        _same(
            set(row),
            {
                "cycle",
                "status",
                "boundaries",
                "initial_hold_status",
                "old_peer",
                "new_peer",
                "readiness_ack_seconds",
                "gap_events",
                "resources",
                "notification_value_hex",
                "remote_read_value_hex",
                "final_hold_status",
                "cleanup",
            },
            "physical cycle keys",
        )
        for key, expected in {
            "cycle": cycle,
            "status": "passed",
            "notification_value_hex": "ef",
            "remote_read_value_hex": "00",
            "resources": facts.resources[cycle],
            "cleanup": {**no_peer, "advertising_stop_confirmed": True},
        }.items():
            _same(row[key], expected, f"physical cycle {key}")
        _hold(row["initial_hold_status"], retained)
        _hold(row["final_hold_status"], retained)
        _require(
            _time(row["readiness_ack_seconds"]) < physical.ACK_SECONDS,
            "absolute native+JS ACK budget exceeded",
        )
        local = [
            r
            for r in records
            if r["cycle"] == cycle
            and r["boundary"] not in ("runtime-closed", "physical-closed")
        ]
        _same(row["boundaries"], local, "raw cycle/global boundaries")
        links = row["gap_events"]
        _require(
            type(links) is list and len(links) == 4, "exact two GAP pairs per cycle"
        )
        _require(all(type(e) is dict for e in links), "GAP event objects")
        _same(
            [e.get("kind") for e in links],
            ["connected", "disconnected", "connected", "disconnected"],
            "GAP pair order",
        )
        link_indices = []
        for link in links:
            _require(
                type(link) is dict
                and set(link) == {"kind", "peer", "payload_hex", "monotonic"},
                "GAP event schema",
            )
            peer = _peer(link["peer"])
            _time(link["monotonic"])
            payload = link["payload_hex"]
            size = 13 if link["kind"] == "connected" else 7
            _require(
                type(payload) is str
                and re.fullmatch(r"[0-9a-f]{" + str(size * 2) + "}", payload)
                is not None,
                "GAP payload length/hex",
            )
            wire = bytes.fromhex(payload)
            _same([wire[1:7][::-1].hex(), wire[0]], peer, "GAP wire peer")
            matching = [
                (i, r)
                for i, r in enumerate(records)
                if r == {"boundary": "gap-" + link["kind"], "cycle": cycle, **link}
            ]
            _require(len(matching) == 1, "GAP raw boundary match")
            i, raw = matching[0]
            _same(
                raw,
                {"boundary": "gap-" + link["kind"], "cycle": cycle, **link},
                "typed GAP raw match",
            )
            consumed.add(i)
            link_indices.append(i)
        _same(links[0]["peer"], links[1]["peer"], "old link exact disconnected peer")
        _same(
            links[2]["peer"], links[3]["peer"], "recovered link exact disconnected peer"
        )
        _same(row["old_peer"], links[0]["peer"], "old peer")
        _same(row["new_peer"], links[2]["peer"], "recovery peer")
        entries = {}
        for name, fields in {
            "cycle-before-start": no_peer,
            "subscription-enabled": {},
            "notification-submitted": {
                "handle": document["handles"]["updates"],
                "value_hex": "ef",
            },
            "notification-observed": {},
            "passive-fault-requested": {"peer": links[0]["peer"]},
            "passive-observed": {},
            "recovery-ready": {},
            "recovery-read": {},
            "cycle-closed": {},
            "physical-cycle-closed": no_peer,
        }.items():
            entries[name] = one(cycle, name, fields)[0]
        advertising = [
            (i, r)
            for i, r in enumerate(records)
            if r["cycle"] == cycle and r["boundary"] == "advertising-started"
        ]
        _require(len(advertising) == 2, "initial/recovery physical advertising")
        for i, record in advertising:
            _same(
                record,
                {
                    "boundary": "advertising-started",
                    "cycle": cycle,
                    "monotonic": record["monotonic"],
                    "peer_count": 0,
                },
                "advertising no-peer boundary",
            )
            consumed.add(i)
        _ordered(
            [
                previous,
                entries["cycle-before-start"],
                advertising[0][0],
                link_indices[0],
                entries["subscription-enabled"],
                entries["notification-submitted"],
                entries["notification-observed"],
                entries["passive-fault-requested"],
                link_indices[1],
                entries["passive-observed"],
                advertising[1][0],
            ],
            "passive fault to exact no-peer recovery readiness",
        )
        _ordered(
            [
                advertising[1][0],
                link_indices[2],
                entries["recovery-read"],
                entries["cycle-closed"],
                entries["physical-cycle-closed"],
            ],
            "physical recovered cycle closure",
        )
        _ordered(
            [advertising[1][0], entries["recovery-ready"], entries["recovery-read"]],
            "physical ACK/read gating",
        )
        _ordered(
            [link_indices[2], link_indices[3], entries["cycle-closed"]],
            "physical logical disconnect",
        )
        # GAP can be drained before recovery-ready or recovery-read is committed;
        # do not impose a still-live peer at the provider's cumulative sample.
        for left, right in (
            ("cycle-before-start", "subscription-enabled"),
            ("notification-submitted", "notification-observed"),
            ("passive-fault-requested", "passive-observed"),
            ("recovery-ready", "recovery-read"),
            ("recovery-read", "cycle-closed"),
            ("cycle-closed", "physical-cycle-closed"),
        ):
            _require(
                times[entries[right]] - times[entries[left]] < physical.PHASE_SECONDS,
                f"absolute physical phase budget: {left}/{right}",
            )
        _require(
            times[entries["recovery-ready"]] - times[advertising[1][0]]
            < physical.PHASE_SECONDS,
            "recovery advertising phase budget",
        )
        previous = entries["physical-cycle-closed"]
        summaries.append(
            {
                "cycle": cycle,
                "old_peer": row["old_peer"],
                "recovery_peer": row["new_peer"],
                "gap_events": links,
                "readiness_ack_seconds": row["readiness_ack_seconds"],
                "physical_closed_at": times[previous],
            }
        )
    runtime_closed, _ = one(9, "runtime-closed", {})
    closed, _ = one(9, "physical-closed", {**no_peer, "advertising_stopped": True})
    _ordered([previous, runtime_closed, closed], "final physical closure")
    _require(
        times[closed] - times[previous] < physical.PHASE_SECONDS,
        "runtime-closed phase budget",
    )
    _require(
        closed == len(records) - 1 and len(consumed) == len(records),
        "unknown/duplicate/reset/cleanup physical boundary",
    )
    _hold(document["hold_status"], retained)
    return {
        "cycles": summaries,
        "closed_at": times[closed],
        "observed_seconds": times[-1] - times[0],
    }


def verify(
    text: str,
    *,
    platform: str,
    run_id: str,
    service_uuid: str,
    physical_path: Path,
    fixture_request: Path,
    fixture_identity: tuple[int, int],
    host_cycles: tuple[int, ...],
) -> dict:
    """Return a JSON-safe ledger or reject; never write, dispatch or stop anything.

    Caller owns current provider PID/PGID/lease and native SDK launch/action proof.
    ``host_cycles`` is the confirmed parent SDK actions, not JS ACKs. Invoke before
    fixture stop and write the returned object to attempt/recovery-ten-ledger.json;
    the evidence bundle also requires the physical document and capture bindings.
    """
    _require(platform in ("ios", "android"), "unsupported recovery platform")
    _uuid(run_id)
    _uuid(service_uuid)
    _require(
        type(host_cycles) is tuple
        and len(host_cycles) == 10
        and all(type(c) is int for c in host_cycles)
        and host_cycles == tuple(range(10)),
        "confirmed parent host cycles must be exactly 0..9",
    )
    _require(
        type(fixture_identity) is tuple and len(fixture_identity) == 2,
        "fixture PID/PGID tuple",
    )
    for value in fixture_identity:
        _integer(value, (1 << 63) - 1, "fixture PID/PGID")
    plan = require_multifunction(session.load_session(fixture_request))
    _require(
        plan.session_id == run_id and plan.service_uuid == service_uuid,
        "foreign run/service fixture session",
    )
    receipt = session.verify_receipt(plan, fixture_identity=fixture_identity)
    profile = session.load_session_profile(plan.contract, plan.profile_path)
    _same(
        {c.role: (c.uuid, c.properties) for c in profile.characteristics},
        {
            role: (prefix + "-0000-4000-8000-000000000000", bits)
            for role, (prefix, bits) in _SHAPE.items()
        },
        "profile versus JS frozen GATT shape",
    )
    _require(
        profile.notification_value == b"\xef"
        and next(
            c for c in profile.characteristics if c.role == "read-write"
        ).initial_value
        == b"\x00",
        "profile EF / remote RW 00",
    )
    facts, cycles = _runtime(text, run_id, profile, platform)
    raw = session._read_bytes(physical_path)
    document = session._json(raw)
    groups = {
        "cycles": 10,
        "connections": 20,
        "disconnections": 20,
        "closed_snapshots": 10,
    }
    expected = {
        "schema_version": 1,
        "mode": physical.MODE,
        "status": "passed",
        "run_id": run_id,
        "session_id": run_id,
        "fixture_pid": fixture_identity[0],
        "fixture_pgid": fixture_identity[1],
        "contract_sha256": plan.contract_sha256,
        "profile_sha256": plan.profile_sha256,
        "resource_identity": plan.resource_identity,
        "port": plan.port,
        "service_uuid": service_uuid,
        "profile_id": profile.profile_id,
        "profile": session._json(session._read_bytes(plan.profile_path)),
        "live_receipt_path": str(plan.receipt_path),
        "errors": [],
        "primary_error": None,
        "cleanup_errors": [],
        "native_bootstrap": facts.bootstrap,
        "native_signals": {str(c): value for c, value in facts.signals.items()},
        "resource_scope": "logical native owners; OS phone peer cache excluded",
        "final_groups": groups,
        "cleanup": {
            "hold_inactive": True,
            "peer_count": 0,
            "advertising_stop_confirmed": True,
        },
    }
    _same(
        set(document),
        {
            *expected,
            "handles",
            "budgets_seconds",
            "cycles",
            "boundaries",
            "hold_status",
            *(
                {"retained_ticket", "fault_samples"}
                if "retained_ticket" in document
                else set()
            ),
        },
        "current physical schema keys",
    )
    for key, value in expected.items():
        _same(document[key], value, f"physical {key}")
    handles = document["handles"]
    _require(
        type(handles) is dict and set(handles) == set(_SHAPE),
        "provider profile handles",
    )
    for value in handles.values():
        _integer(value, 0xFFFF, "provider ATT handle")
    _require(len(set(handles.values())) == 5, "distinct provider ATT handles")
    budgets = {
        "phase": physical.PHASE_SECONDS,
        "whole": physical.WHOLE_SECONDS,
        "ack": physical.ACK_SECONDS,
        "cleanup": physical.CLEANUP_SECONDS,
    }
    _require(
        type(document["budgets_seconds"]) is dict
        and set(document["budgets_seconds"]) == set(budgets),
        "physical budget keys",
    )
    for key, value in budgets.items():
        _require(
            _time(document["budgets_seconds"][key]) == value,
            f"provider absolute budget {key}",
        )
    proof = _physical(document, facts)
    # Revalidate after the potentially large audit; retain the same cooperative
    # receipt, immutable input hashes and resource tuple at the return boundary.
    _same(
        session.verify_receipt(plan, fixture_identity=fixture_identity),
        receipt,
        "receipt changed during verification",
    )
    return {
        "schema_version": 1,
        "status": "passed",
        "mode": physical.MODE,
        "platform": platform,
        "run_id": run_id,
        "service_uuid": service_uuid,
        "runtime_nonce": facts.nonce,
        "host_cycles": list(host_cycles),
        "source_contracts": [
            "tests/consumers/react-native-internal/src/rf_recovery_ten.tsx",
            "tests/consumers/react-native-internal/src/rf_contract.ts",
            "integrations/react-native-internal/specs/BleHubProbe.nitro.ts",
            "tests/peripherals/nrf52840/recovery_ten.py",
        ],
        "native_bootstrap": facts.bootstrap,
        "native_signals": {str(c): value for c, value in facts.signals.items()},
        "cycles": cycles,
        "final_groups": groups,
        "counts": {
            "app_runtimes": 1,
            "hub_probe_lifecycles": 10,
            "commands": 100,
            "terminals": 100,
            "events": facts.event_count,
            "resource_snapshots": 50,
            "closed_zero_snapshots": 10,
            "notifications": 10,
            "passive_disconnects": 10,
            "remote_reads": 10,
        },
        "closed_resource_totals": {
            key: sum(c["resources"]["closed"][key] for c in cycles)
            for key in sorted(physical.RESOURCE_FIELDS)
        },
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
            "handles": handles,
        },
        "physical": {
            "path": str(physical_path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "budgets_seconds": budgets,
            **proof,
        },
        "explicit_limits": {
            "pid_provenance": "Parent owns same pinned PID capture and official SDK launch/install/executable identity; normalized text cannot independently prove PID provenance.",
            "host_authority": "Parent confirms SDK actions 0..9 only after provider recovery-ready progress; native signal then JS ACK are corroborating records, not independent host-action proof.",
            "timing": "The event projection has no per-event timestamps; normalized threadtime is not accepted as timing authority. JS 2000/1000 ms silence stage claims and event ordering are captured, not independently timed. Finite recorded physical spans/ACK satisfy absolute budgets; unrecorded phase starts and whole 900/phase 90/ACK 2 deadlines remain provider-owned, never polling-renewed.",
            "resources": "17 logical adapter-owned native/SPI runtime counters after awaited native close, not OS memory or CoreBluetooth-internal callbacks. Apple's warm GATT identity/value barriers before close are not erased or relabeled leaks.",
            "native_ids": "Native internal GATT/attribute allocation IDs are opaque u64, not request IDs; zero is valid where the source permits it.",
            "physical_semantics": "Current cooperative BTP/session/receipt records with exact GAP pairs and no reset boundary; parent owns process/lease liveness and stable configuration. Provider cycle-closed is committed after sampling JS passed; clocks are not cross-correlated to invent a physical-closed-before-JS-passed assertion. Exact GAP disconnection and physical close before the next provider cycle are mandatory. No independent radio replay or signature/security claim.",
            "positive_control": "Active connection/subscription/GATT and EF/read00 are required from the actual RF native path, not FakeBackend. Synthetic unit tests prove verifier behavior only, never RF.",
            "packaging": "Parent/bundle must bind raw physical proof, capture identity, SDK actions and source candidate. No x86 execution or otherwise-unused artifact-map coverage is inferred.",
        },
    }
