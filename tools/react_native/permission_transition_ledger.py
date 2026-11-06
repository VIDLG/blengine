"""Pure permission host audit. Persisted evidence never reauthorizes device I/O.

RuntimeFacts owns the native/JS wire grammar. Host boundaries (marker offsets)
are replayed only after validating provider handshakes and independent OS proof;
no bootstrap, pass, or synthetic marker can authorize process death.
"""

from __future__ import annotations

import hashlib
import math
import re
from itertools import pairwise
from pathlib import Path
from typing import Any

from tests.peripherals.nrf52840 import permission_transition as wire
from tests.peripherals.nrf52840.fixture_session import _json
from tools.android.permission_control import (
    CONNECT,
    PACKAGE,
    PERMISSIONS,
    ROOT_ID_ARGS,
    _start_ticks,
    parse_exit_history,
    parse_permissions,
    permission_mutation_arguments,
)
from tools.react_native.capture_io import MAX_BYTES, THREADTIME

PREFIX = wire.PREFIX
RAW_PREFIX = "BLEHUB_NITRO_RUNTIME "
BOUNDARIES = (
    "authorize_active",
    "authorize_arm",
    "mark_waiting",
    "confirm_loss",
    "authorize_restored",
)


def require(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(f"permission evidence: {message}")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def moment(value: Any) -> float:
    require(
        type(value) in (int, float) and math.isfinite(value) and value >= 0,
        "invalid monotonic time",
    )
    return value


def markers(text: str) -> list[str]:
    require(len(text.encode()) <= MAX_BYTES, "runtime exceeds capture budget")
    result = []
    for line in text.splitlines(keepends=True):
        if "BLEHUB_NITRO_RUNTIME" not in line:
            continue
        require(
            line.endswith("\n")
            and line.count("BLEHUB_NITRO_RUNTIME") == 1
            and line.count(RAW_PREFIX) == 1
            and PREFIX in line,
            "foreign/partial/ambiguous runtime marker",
        )
        require(len(line.encode()) <= wire.MAX_LINE_BYTES, "oversized runtime line")
        result.append(line.removesuffix("\n"))
    require(len(result) <= wire.MAX_MARKERS, "runtime marker budget exceeded")
    return result


from tests.peripherals.nrf52840.fault_control import (
    Operation,
    Outcome,
    Phase,
    Winner,
    cleanup_ready,
    owner_nonce,
    status_from_document,
    ticket_from_document,
)


def verify_readiness(
    value: dict[str, Any],
    name: str,
    *,
    facts: wire.RuntimeFacts,
    receipt_sha256: str,
    armed: dict[str, Any] | None = None,
    request_raw: bytes | None = None,
    ready_raw: bytes | None = None,
) -> None:
    """Exact provider publish() shapes, including the same hold/route/peer generation."""
    phases = {
        wire.ARM_READY_NAME: "armed",
        wire.REVOKE_READY_NAME: "waiting",
        wire.REVOKE_AUTHORIZED_NAME: "revoke-authorized",
        wire.RESTORED_READY_NAME: "restored",
    }
    extra = {
        wire.REVOKE_AUTHORIZED_NAME: {
            "request_sha256",
            "ready_sha256",
            "request_nonce",
        },
        wire.RESTORED_READY_NAME: {
            "hold_inactive",
            "peer_count",
            "canonical_value_hex",
        },
    }.get(name, set())
    require(
        name in phases
        and set(value)
        == {
            "schema_version",
            "run_id",
            "phase",
            "receipt_sha256",
            "native_bootstrap",
            "route",
            "hold",
            "fault_status",
            "peer",
        }
        | extra,
        "provider readiness schema",
    )
    require(
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["run_id"] == facts.run_id
        and value["phase"] == phases[name],
        "provider phase/session",
    )
    require(
        value["receipt_sha256"] == receipt_sha256
        and value["native_bootstrap"] == facts.bootstraps.get("active")
        and value["route"] == facts.route
        and facts.route is not None,
        "provider receipt/bootstrap/route",
    )
    hold, peer = value["hold"], value["peer"]
    ticket = ticket_from_document(hold)
    status = status_from_document(value["fault_status"])
    require(
        status.ticket == ticket
        and ticket.owner_nonce == owner_nonce(facts.run_id)
        and ticket.operation is Operation.READ,
        "owned v2 ticket/operation/receipt",
    )
    if name == wire.RESTORED_READY_NAME:
        require(
            cleanup_ready(status)
            and status.winner is Winner.DISCONNECT
            and status.outcome is Outcome.DISCONNECTED_UNCOMMITTED,
            "restored requires retained disconnect resource-zero receipt",
        )
    else:
        require(
            status.phase
            is (Phase.ARMED if name == wire.ARM_READY_NAME else Phase.HELD),
            "fresh v2 fault phase",
        )
    require(
        type(peer) is list
        and len(peer) == 2
        and type(peer[0]) is str
        and re.fullmatch(r"[0-9a-f]{12}", peer[0]) is not None
        and type(peer[1]) is int
        and peer[1] in range(4),
        "exact physical peer",
    )
    if armed is not None:
        require(
            all(
                value[k] == armed[k]
                for k in ("hold", "peer", "route", "native_bootstrap")
            ),
            "provider hold/peer generation changed",
        )
    if name == wire.REVOKE_AUTHORIZED_NAME:
        require(
            request_raw is not None and ready_raw is not None,
            "authorization lacks fresh challenge",
        )
        request = _json(request_raw)
        require(
            set(request)
            == {"schema_version", "run_id", "route", "ready_sha256", "request_nonce"}
            and type(request["schema_version"]) is int
            and request["schema_version"] == 1
            and request["run_id"] == facts.run_id
            and request["route"] == facts.route
            and request["ready_sha256"] == digest(ready_raw)
            and wire._uuid(request["request_nonce"]),
            "revoke request schema/hash/nonce",
        )
        require(
            value["request_sha256"] == digest(request_raw)
            and value["ready_sha256"] == digest(ready_raw)
            and value["request_nonce"] == request["request_nonce"],
            "authorization challenge hashes/nonce",
        )
    if name == wire.RESTORED_READY_NAME:
        require(
            type(value["hold_inactive"]) is bool
            and value["hold_inactive"]
            and type(value["peer_count"]) is int
            and value["peer_count"] == 0
            and value["canonical_value_hex"] == "00",
            "restored fixture not canonical/inactive",
        )


def verify_death(
    proof_raw: bytes,
    os_raw: bytes,
    *,
    identity: dict[str, Any],
    run_id: str,
    device: str,
    authorized_at: float,
) -> dict[str, Any]:
    """Audit the ORIGINAL controller journal snapshot plus original dumpsys stdout.

    The snapshot is taken immediately after await_permission_death, before grant
    or cleanup can add receipts. It is not a reserialized record/fingerprint.
    """
    require(len(proof_raw) <= MAX_BYTES and len(os_raw) <= MAX_BYTES, "OS proof budget")
    journal = _json(proof_raw)
    require(
        journal.get("run_id") == run_id
        and journal.get("device") == device
        and journal.get("package") == PACKAGE
        and journal.get("uid") == identity["uid"]
        and journal.get("user_id") == 0,
        "OS journal identity",
    )
    use_root = permission_command_mode(journal, device)
    actions = journal.get("actions")
    require(type(actions) is list and len(actions) <= 256, "OS journal actions")
    selected = {}
    last = 0.0
    for action in actions:
        start, end = (
            moment(action["started_monotonic"]),
            moment(action["finished_monotonic"]),
        )
        require(
            last <= start <= end < moment(action["deadline_monotonic"])
            and action["status"] == "confirmed",
            "failed/late OS action",
        )
        last = end
        receipts = action["receipts"]
        require(type(receipts) is list and receipts, "OS receipts missing")
        previous = start
        for receipt in receipts:
            began, ended = (
                moment(receipt["started_monotonic"]),
                moment(receipt["finished_monotonic"]),
            )
            require(
                previous <= began <= ended <= end
                and 0 < moment(receipt["timeout_seconds"]) <= 5,
                "OS receipt budget/order",
            )
            output = receipt["stdout"]
            require(
                type(output) is str
                and receipt["stdout_complete"] is True
                and receipt["stdout_bytes"] == len(output.encode())
                and receipt["stdout_sha256"] == digest(output.encode()),
                "OS original stdout integrity",
            )
            command = receipt["command"]
            require(
                type(command) is list
                and len(command) >= 5
                and re.split(r"[\\/]", command[0])[-1] in ("adb", "adb.exe")
                and command[1:4] == ["-s", device, "shell"],
                "OS receipt device/command",
            )
            previous = ended
        if action["label"] in ("initial", "revoke-connected", "await-permission-death"):
            require(action["label"] not in selected, "duplicate OS death action")
            selected[action["label"]] = action
    require(
        set(selected) == {"initial", "revoke-connected", "await-permission-death"}
        and actions[-2:]
        == [selected["revoke-connected"], selected["await-permission-death"]],
        "missing/followed OS death gate",
    )
    initial, revoke, death = (
        selected[k] for k in ("initial", "revoke-connected", "await-permission-death")
    )
    for action in (initial, revoke):
        dumps = [
            r
            for r in action["receipts"]
            if r["command"][4:] == ["dumpsys", "package", PACKAGE]
        ]
        require(bool(dumps), "permission dump missing")
        uid, grants = parse_permissions(dumps[-1]["stdout"])
        require(
            uid == identity["uid"]
            and (all(grants.values()) if action is initial else not grants[CONNECT]),
            "OS permission readback UID/grants",
        )
    initial_history = [
        r
        for r in initial["receipts"]
        if r["command"][4:] == ["dumpsys", "activity", "exit-info", PACKAGE]
    ]
    initial_clock = [
        r for r in initial["receipts"] if r["command"][4:] == ["date", "+%s%3N,%z"]
    ]
    require(
        len(initial_history) == len(initial_clock) == 1,
        "original initial history/clock required",
    )
    offset = initial_clock[0]["stdout"].strip().split(",")[-1]
    require(
        parse_exit_history(initial_history[0]["stdout"], offset)
        == initial["exit_records"]
        == journal["baseline_exit_records"],
        "initial raw exit baseline changed",
    )
    require(
        journal.get("api", 0) >= 31
        and journal.get("initial_grants") == dict.fromkeys(PERMISSIONS, True),
        "initial permissions/API",
    )
    require(
        revoke.get("old_identity") == identity
        and death.get("old_instance_gone") is True
        and death.get("observed_start_ticks") != identity["start_ticks"],
        "OS old instance binding",
    )
    require(
        authorized_at <= revoke["started_monotonic"] < authorized_at + 4
        and revoke["finished_monotonic"] < authorized_at + 4,
        "authorization-to-revoke 4s budget",
    )
    mutations = [r for r in revoke["receipts"] if r["command"][4] in ("pm", "su")]
    require(
        len(mutations) == 1
        and tuple(mutations[0]["command"][4:])
        == permission_mutation_arguments("revoke", CONNECT, use_root=use_root)
        and mutations[0]["returncode"] == 0,
        "exact CONNECT revoke",
    )
    revoked_at = mutations[0]["started_monotonic"]
    require(
        death["finished_monotonic"] < revoked_at + 6,
        "death exceeded total 6s from revoke",
    )
    stat = [
        r
        for r in revoke["receipts"]
        if r["command"][4:] == ["cat", f"/proc/{identity['pid']}/stat"]
    ]
    require(
        len(stat) == 2
        and all(
            r["returncode"] == 0
            and _start_ticks(r["stdout"], identity["pid"]) == identity["start_ticks"]
            for r in stat
        ),
        "revoke procstart binding",
    )
    status = [
        r
        for r in revoke["receipts"]
        if r["command"][4:] == ["cat", f"/proc/{identity['pid']}/status"]
    ]
    require(
        len(status) == 1
        and re.findall(
            r"(?m)^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", status[0]["stdout"]
        )
        == [(str(identity["uid"]),) * 4],
        "revoke UID binding",
    )
    cmdline = [
        r
        for r in revoke["receipts"]
        if r["command"][4:] == ["cat", f"/proc/{identity['pid']}/cmdline"]
    ]
    require(
        len(cmdline) == 1 and cmdline[0]["stdout"].rstrip("\0") == PACKAGE,
        "revoke process name",
    )
    dumps = [
        r
        for r in death["receipts"]
        if r["command"][4:] == ["dumpsys", "activity", "exit-info", PACKAGE]
    ]
    clocks = [r for r in death["receipts"] if r["command"][4:] == ["date", "+%s%3N,%z"]]
    require(
        dumps
        and clocks
        and dumps[-1]["stdout"].encode() == os_raw
        and dumps[-1]["returncode"] == 0,
        "original raw OS dump mismatch",
    )
    clock = re.fullmatch(r"(\d{13}),([+-]\d{4})\s*", clocks[-1]["stdout"])
    require(clock is not None, "OS clock/offset")
    records = parse_exit_history(os_raw.decode(), clock[2])
    baseline = {r["fingerprint"] for r in initial["exit_records"]}
    candidates = [
        r
        for r in records
        if r["pid"] == identity["pid"] and r["fingerprint"] not in baseline
    ]
    require(len(candidates) == 1, "stale/ambiguous OS exit record")
    record = candidates[0]
    require(
        record == death.get("exit_record")
        and record["real_uid"] == record["package_uid"] == identity["uid"]
        and record["user_id"] == identity["user_id"] == 0
        and record["process_name"] == PACKAGE
        and record["reason"] == 8
        and record["reason_name"] == "PERMISSION CHANGE",
        "OS reason8 PID/UID/user identity",
    )
    require(
        identity["native_started_unix_ms"]
        <= revoke["revoke_unix_ms"]
        <= record["timestamp_unix_ms"]
        <= int(clock[1])
        <= revoke["revoke_unix_ms"] + 6000,
        "OS native start/revoke/death timestamp window",
    )
    gone = [
        r
        for r in death["receipts"]
        if r["command"][4:] == ["cat", f"/proc/{identity['pid']}/stat"]
    ][-1]
    require(
        (
            gone["returncode"] == 1
            and gone["stdout"].strip()
            == f"cat: /proc/{identity['pid']}/stat: No such file or directory"
        )
        or (
            gone["returncode"] == 0
            and _start_ticks(gone["stdout"], identity["pid"]) != identity["start_ticks"]
        ),
        "OS process survival/unknown disappearance",
    )
    return record


def verify_runtime(
    text: str,
    run_id: str,
    service_uuid: str,
    log_path: Path | str,
    *,
    capture_result: dict[str, Any],
    documents: dict[str, bytes],
    receipt_sha256: str,
    death_proof: bytes,
    os_raw: bytes,
    local_name: str,
) -> dict[str, Any]:
    """Replay exact recorded boundaries; neither markers nor reason-only claims suffice."""
    rows = markers(text)
    facts = wire.RuntimeFacts(run_id, service_uuid, local_name)
    require(
        capture_result.get("run_id") == run_id
        and capture_result.get("status") == "passed",
        "capture result session/status",
    )
    boundaries = capture_result["boundaries"]
    require(
        [b["kind"] for b in boundaries] == list(BOUNDARIES),
        "host evidence boundaries/order",
    )
    require(
        all(
            type(b["after_marker"]) is int and 0 <= b["after_marker"] <= len(rows)
            for b in boundaries
        ),
        "boundary marker offset",
    )
    require(
        all(
            a["after_marker"] <= b["after_marker"]
            and moment(a["monotonic"]) <= moment(b["monotonic"])
            for a, b in pairwise(boundaries)
        ),
        "host boundary causal order",
    )
    armed = _json(documents[wire.ARM_READY_NAME])
    phases = capture_result["phases"]
    require(set(phases) == set(wire.PHASES), "three phase PID receipts required")
    active = phases["active"]
    position = 0
    for index in range(len(rows) + 1):
        while (
            position < len(boundaries) and boundaries[position]["after_marker"] == index
        ):
            boundary = boundaries[position]
            kind = boundary["kind"]
            if kind == "authorize_active":
                require(
                    _json(documents[wire.ACTIVE_GO_NAME])
                    == {"schema_version": 1, "run_id": run_id, "phase": "active"}
                    and phases["denied"].get("retired") is True,
                    "active go before denied retire",
                )
            elif kind in ("authorize_arm", "mark_waiting", "authorize_restored"):
                name = {
                    "authorize_arm": wire.ARM_READY_NAME,
                    "mark_waiting": wire.REVOKE_READY_NAME,
                    "authorize_restored": wire.RESTORED_READY_NAME,
                }[kind]
                verify_readiness(
                    _json(documents[name]),
                    name,
                    facts=facts,
                    receipt_sha256=receipt_sha256,
                    armed=armed,
                )
            elif kind == "confirm_loss":
                authorized = _json(documents[wire.REVOKE_AUTHORIZED_NAME])
                verify_readiness(
                    authorized,
                    wire.REVOKE_AUTHORIZED_NAME,
                    facts=facts,
                    receipt_sha256=receipt_sha256,
                    armed=armed,
                    request_raw=documents[wire.REVOKE_REQUEST_NAME],
                    ready_raw=documents[wire.REVOKE_READY_NAME],
                )
                record = verify_death(
                    death_proof,
                    os_raw,
                    identity=active["process_identity"],
                    run_id=run_id,
                    device=capture_result["device"],
                    authorized_at=capture_result["authorized_monotonic"],
                )
                loss = _json(documents[wire.LOSS_CONFIRMED_NAME])
                require(
                    loss
                    == {
                        "schema_version": 1,
                        "run_id": run_id,
                        "authorized_sha256": digest(
                            documents[wire.REVOKE_AUTHORIZED_NAME]
                        ),
                        "request_nonce": authorized["request_nonce"],
                        "pid": record["pid"],
                        "uid": record["real_uid"],
                        "user_id": record["user_id"],
                        "reason": 8,
                        "exit_proof_sha256": digest(death_proof),
                        "revoked_elapsed_ms": capture_result["revoked_elapsed_ms"],
                    },
                    "loss confirmation original OS proof binding",
                )
                require(
                    type(loss["revoked_elapsed_ms"]) is int
                    and 0 <= loss["revoked_elapsed_ms"] <= 4000
                    and active.get("closed_resources") is None
                    and active.get("dead") is True,
                    "dead Probe cannot supply close/zero",
                )
            getattr(facts, kind)()
            position += 1
        if index < len(rows):
            line = rows[index]
            header = THREADTIME.match(line)
            require(header is not None, "raw source PID required")
            facts.consume(line)
    require(
        position == len(boundaries) and facts.passed, "incomplete permission runtime"
    )
    commands = capture_result.get("commands")
    require(
        type(commands) is list and 0 < len(commands) <= 4096,
        "bounded capture command receipts required",
    )
    launches = []
    arms = []
    auth = moment(capture_result["authorized_monotonic"])
    confirmed = moment(boundaries[3]["monotonic"])
    for row in commands:
        command = row["command"]
        require(
            type(command) is list
            and command[1:3] == ["-s", capture_result["device"]]
            and moment(row["started_monotonic"]) <= moment(row["finished_monotonic"]),
            "capture command identity/time",
        )
        if auth < row["started_monotonic"] < confirmed:
            require(
                not any(
                    token in command[3:]
                    for token in ("force-stop", "kill", "install", "start")
                ),
                "host force/relaunch inside death-proof window",
            )
        if "blehub.rfPermissionPhase" in command:
            launches.append(row)
        if "android.intent.action.VIEW" in command:
            arms.append(row)
    require(
        [row["phase"] for row in launches] == list(wire.PHASES)
        and len(arms) == 1
        and arms[0]["command"][-1] == wire.arm_url(run_id),
        "three phase launches/one canonical ARM",
    )
    for row, phase in zip(launches, wire.PHASES, strict=True):
        command = row["command"]
        require(
            row.get("returncode") == 0
            and all(
                command.count(key) == 1 and command[command.index(key) + 1] == value
                for key, value in (
                    ("blehub.runtimeMode", "rf-permission-transition"),
                    ("blehub.rfPermissionRunId", run_id),
                    ("blehub.rfPermissionPhase", phase),
                )
            ),
            "phase launch extras",
        )
    for phase, native in facts.bootstraps.items():
        entry = phases[phase]
        identity = entry["process_identity"]
        require(
            entry["native_bootstrap"] == native
            and entry["nonce"] == facts.nonces[phase]
            and identity["pid"] == native["pid"]
            and identity["uid"] == native["uid"]
            and identity["user_id"] == native["user_id"]
            and identity["native_started_unix_ms"] == native["started_unix_ms"]
            and type(identity["start_ticks"]) is int
            and identity["start_ticks"] > 0,
            "phase OS/native/nonce binding",
        )
    return {
        "schema_version": 1,
        "platform": "android",
        "scenario": wire.MODE,
        "status": "passed",
        "run_id": run_id,
        "service_uuid": service_uuid,
        "source": str(log_path),
        "runtime_sha256": digest(text.encode()),
        "native_bootstraps": facts.bootstraps,
        "runtime_nonces": facts.nonces,
        "resources": facts.resources,
        "active_closed_resources": None,
        "route": facts.route,
        "counts": {
            "app_runtimes": 3,
            "alive_probes": 3,
            "hub_probe_lifecycles": 4,
            "closed_zero_snapshots": 3,
            "connections": 3,
            "disconnections": 3,
        },
    }


def verify_physical(
    physical: dict[str, Any],
    *,
    request: dict[str, Any],
    receipt: dict[str, Any],
    receipt_sha256: str,
    runtime: dict[str, Any],
    documents: dict[str, bytes],
) -> None:
    """Verify three real GAP pairs and provider causal boundaries, not JS claims."""
    run = runtime["run_id"]
    for value in (request, receipt, physical):
        require(
            type(value.get("schema_version")) is int
            and value["schema_version"] == 1
            and value.get("session_id") == run,
            "physical session",
        )
    for key in ("contract_sha256", "profile_sha256", "resource_identity"):
        require(
            request.get(key) is not None
            and receipt.get(key) == physical.get(key) == request[key],
            "fixture identity/hash",
        )
    require(
        receipt.get("proof_method") == "btp-vendor-live-under-device-lease"
        and physical.get("receipt_sha256") == receipt_sha256
        and request.get("service_uuid") == runtime["service_uuid"],
        "physical receipt attestation",
    )
    for key in ("fixture_pid", "fixture_pgid"):
        require(
            type(physical.get(key)) is int
            and physical[key] > 0
            and physical[key] == receipt.get(key),
            "fixture PID/PGID",
        )
    for key, value in {
        "mode": wire.MODE,
        "run_id": run,
        "status": "passed",
        "phase": "closed",
        "connections": 3,
        "disconnections": 3,
        "primary_error": None,
        "cleanup_errors": [],
        "rejected_events": [],
        "aliveProbes": ["denied", "baseline", "restored"],
        "native_bootstraps": runtime["native_bootstraps"],
        "runtime_nonces": runtime["runtime_nonces"],
        "resources": runtime["resources"],
        "cleanup": {
            "advertising_stopped": True,
            "hold_inactive": True,
            "peer_count": 0,
        },
    }.items():
        require(physical.get(key) == value, f"physical {key}")
    require(
        physical.get("protocol")
        == {name: _json(documents[name]) for name in wire.HANDSHAKE_NAMES},
        "physical handshake bytes/content",
    )
    hold = _json(documents[wire.ARM_READY_NAME])["hold"]
    ticket = ticket_from_document(hold)
    retired = status_from_document(physical["hold_status"])
    require(
        retired.ticket == ticket
        and cleanup_ready(retired)
        and retired.winner is Winner.DISCONNECT
        and retired.outcome is Outcome.DISCONNECTED_UNCOMMITTED
        and physical.get("handles", {}).get("read-write") == ticket.handle,
        "physical retained resource-zero receipt",
    )
    events = physical.get("events")
    require(type(events) is list and len(events) == 6, "exactly three GAP pairs")
    previous = 0.0
    for i, event in enumerate(events):
        kind = "disconnected" if i % 2 else "connected"
        require(
            set(event) == {"kind", "peer", "payload_hex", "monotonic"}
            and event["kind"] == kind,
            "GAP schema/order",
        )
        payload = event["payload_hex"]
        require(
            type(payload) is str
            and re.fullmatch(r"[0-9a-f]{" + str(14 if i % 2 else 26) + "}", payload)
            is not None,
            "GAP original wire",
        )
        raw = bytes.fromhex(payload)
        require(
            event["peer"] == [raw[1:7][::-1].hex(), raw[0]]
            and (not i % 2 or event["peer"] == events[i - 1]["peer"]),
            "GAP peer binding",
        )
        require(previous < moment(event["monotonic"]), "GAP chronology")
        previous = event["monotonic"]
    boundaries = physical.get("boundaries")
    require(
        type(boundaries) is list and len(boundaries) == 12, "physical boundary count"
    )
    gap = [b for b in boundaries if b["boundary"].startswith("gap-")]
    require(
        [{k: b[k] for k in ("kind", "peer", "payload_hex", "monotonic")} for b in gap]
        == events,
        "GAP boundary originals",
    )
    named = {
        b["boundary"]: b for b in boundaries if not b["boundary"].startswith("gap-")
    }
    require(
        list(named)
        == [
            "armed",
            "waiting",
            "revoke-authorized",
            "permission-loss-confirmed",
            "retirement-observed",
            "physical-closed",
        ],
        "physical causal order",
    )
    times = [moment(b["monotonic"]) for b in boundaries]
    require(all(a <= b for a, b in pairwise(times)), "physical boundary chronology")
    require(
        events[1]["monotonic"]
        < events[2]["monotonic"]
        < named["armed"]["monotonic"]
        <= named["waiting"]["monotonic"]
        <= named["revoke-authorized"]["monotonic"]
        < events[3]["monotonic"]
        < events[4]["monotonic"]
        and named["permission-loss-confirmed"]["monotonic"]
        <= named["retirement-observed"]["monotonic"]
        < events[4]["monotonic"]
        and events[5]["monotonic"] <= named["physical-closed"]["monotonic"],
        "physical loss/restoration order",
    )
    require(
        named["revoke-authorized"]["monotonic"] - named["waiting"]["monotonic"] < 3
        and named["permission-loss-confirmed"]["monotonic"]
        - named["waiting"]["monotonic"]
        < 8
        and status_from_document(named["revoke-authorized"]["status"]).phase
        is Phase.HELD,
        "physical fresh WAITING/watchdog budget",
    )
    for name in ("armed", "waiting", "revoke-authorized", "retirement-observed"):
        require(
            named[name].get("ticket") == hold
            and status_from_document(named[name]["status"]).ticket == ticket,
            "physical hold generation",
        )
    require(
        named["permission-loss-confirmed"].get("reason") == 8
        and named["permission-loss-confirmed"].get("pid")
        == runtime["native_bootstraps"]["active"]["pid"],
        "physical loss identity",
    )


def permission_command_mode(control: dict[str, Any], device: str) -> bool:
    """Bind every pm mutation to the one initial shell/root selection and raw proof."""
    mode = control.get("permission_command_mode", "shell")
    require(mode in ("shell", "root"), "unknown permission command mode")
    use_root = mode == "root"
    identities = []
    allowed = {
        permission_mutation_arguments(verb, permission, use_root=use_root)
        for verb in ("grant", "revoke")
        for permission in PERMISSIONS
    }
    for action in control["actions"]:
        for row in action["receipts"]:
            command = row["command"]
            require(
                type(command) is list
                and len(command) >= 5
                and re.split(r"[\\/]", command[0])[-1] in ("adb", "adb.exe")
                and command[1:4] == ["-s", device, "shell"],
                "permission command device/identity",
            )
            args = tuple(command[4:])
            if args == ROOT_ID_ARGS:
                output = row["stdout"]
                require(
                    use_root
                    and action["label"] == "initial"
                    and row is action["receipts"][0]
                    and row["returncode"] == 0
                    and re.match(r"^uid=0(?:\(|\s|$)", output.strip()) is not None
                    and row["stdout_complete"] is True
                    and row["stdout_bytes"] == len(output.encode())
                    and row["stdout_sha256"] == digest(output.encode()),
                    "original initial root identity proof",
                )
                identities.append(row)
            elif args[0] in ("su", "pm"):
                require(args in allowed, "permission mutation transport/script drift")
    require(len(identities) == int(use_root), "missing/duplicate root identity")
    return use_root


def verify_permission_control(
    control: dict[str, Any], run_id: str, device: str
) -> None:
    """Audit final restoration/readback without mistaking it for old process close."""
    require(
        control.get("schema_version") == 1
        and control.get("run_id") == run_id
        and control.get("device") == device
        and control.get("status") == "passed"
        and control.get("package") == PACKAGE
        and control.get("user_id") == 0
        and control.get("api", 0) >= 31
        and control.get("primary_error") is None
        and control.get("cleanup_errors") == [],
        "final permission control",
    )
    require(
        control.get("initial_grants")
        == control.get("final_grants")
        == dict.fromkeys(PERMISSIONS, True),
        "initial grants not restored",
    )
    permission_command_mode(control, device)
    actions = control["actions"]
    require(
        [a["label"] for a in actions]
        == [
            "initial",
            "deny-startup",
            "grant:active",
            "revoke-connected",
            "await-permission-death",
            "grant:restored",
            "restore-initial-grants",
        ],
        "permission action order",
    )
    for action in actions:
        require(
            action["status"] == "confirmed"
            and moment(action["started_monotonic"])
            <= moment(action["finished_monotonic"])
            < moment(action["deadline_monotonic"]),
            "permission action failed/late",
        )
        dumps = [
            r
            for r in action["receipts"]
            if r["command"][4:] == ["dumpsys", "package", PACKAGE]
        ]
        if action["label"] == "await-permission-death":
            continue
        require(bool(dumps), "permission readback missing")
        uid, grants = parse_permissions(dumps[-1]["stdout"])
        expected = dict.fromkeys(PERMISSIONS, action["label"] != "deny-startup")
        require(
            uid == control["uid"]
            and (
                not grants[CONNECT]
                if action["label"] == "revoke-connected"
                else grants == expected
            ),
            "permission final readback drift",
        )


def verify_state_control(control: dict[str, Any], run_id: str, device: str) -> None:
    """Exactly initial/final ON readbacks; never an off/on radio scenario."""
    require(
        control.get("schema_version") == 1
        and control.get("run_id") == run_id
        and control.get("device") == device
        and control.get("status") == "passed"
        and control.get("initial_state") == control.get("final_state") == "ON"
        and control.get("primary_error") is None
        and control.get("cleanup_errors") == [],
        "radio final ON control",
    )
    require(
        [a["label"] for a in control["actions"]] == ["initial", "final"],
        "radio mutations/extra actions forbidden",
    )
    for action in control["actions"]:
        require(
            action["observed_state"] == action["requested_state"] == "ON"
            and bool(action["receipts"]),
            "radio ON readback missing",
        )
        for row in action["receipts"]:
            require(
                row["command"][1:]
                == ["-s", device, "shell", "dumpsys", "bluetooth_manager"]
                and row["returncode"] == 0
                and re.findall(r"(?m)^\s*state:\s*(\S+)\s*$", row["stdout"]) == ["ON"],
                "radio read-only command/ON state",
            )


def index_runtime(text: str, log_path: Path | str) -> dict[str, Any]:
    """Failure-tolerant raw ranges for the bundle; no success/death inference."""
    lines = text.splitlines(keepends=True)
    require(len(text.encode()) <= MAX_BYTES, "index runtime budget")
    events, natives, invalid, errors = [], [], [], []
    starts: dict[str, int] = {}
    current = None
    for number, line in enumerate(lines, 1):
        if "BLEHUB_NITRO_RUNTIME" not in line:
            continue
        if (
            line.count("BLEHUB_NITRO_RUNTIME") != 1
            or line.count(RAW_PREFIX) != 1
            or PREFIX not in line
            or not line.endswith("\n")
        ):
            invalid.append(number)
            continue
        marker = line.split(PREFIX, 1)[1].removesuffix("\n")
        events.append({"line": number, "marker": marker})
        if marker.startswith("native:bootstrap:"):
            try:
                value = _json(marker.split(":", 2)[2].encode())
                current = value.get("phase")
                require(current in wire.PHASES, "invalid native phase label in index")
                starts.setdefault(current, number)
                natives.append({"kind": "bootstrap", "payload": value, "line": number})
            except (RuntimeError, ValueError):
                invalid.append(number)
        if re.search(r"(?:^|:)(failed|rejected)(?::|$)", marker.split("{", 1)[0]):
            errors.append(number)
    flows = []
    for i, phase in enumerate(wire.PHASES):
        start = starts.get(phase)
        following = starts.get(wire.PHASES[i + 1]) if i < 2 else None
        end = following - 1 if following else len(lines)
        segment = "".join(lines[start - 1 : end]).encode() if start else b""
        boot = next(
            (n["payload"] for n in natives if n["payload"].get("phase") == phase), {}
        )
        flows.append(
            {
                "index": i,
                "mode": phase,
                "phase": phase,
                "source": str(log_path),
                "pid": boot.get("pid"),
                "boot_id": boot.get("boot_id"),
                "run_id": boot.get("run_id"),
                "status": "partial" if start else "missing",
                "start_line": start,
                "end_line": end if start else None,
                "sha256": digest(segment) if start else None,
                "size": len(segment),
                "closed_resources": None if phase == "active" else "requires-ledger",
            }
        )
    return {
        "scenario": wire.MODE,
        "prefix": PREFIX,
        "flows": flows,
        "events": events,
        "native_markers": natives,
        "invalid_markers": invalid,
        "error_markers": errors,
    }
