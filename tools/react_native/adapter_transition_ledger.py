"""Pure Android adapter-transition transcript audit; never RF reauthorization.

The wire authority is rf_adapter_transition.tsx and MainActivity.adapterRecord.
Native records are cooperative identity evidence, not OS/signature attestation.
Host command receipts and provider physical records are checked structurally;
none of these offline checks replays liveness, attestation or command deadlines.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from itertools import pairwise
from pathlib import Path
from typing import Any

from tools.react_native import check_runtime_log
from tools.react_native.host_contract import ANDROID_PACKAGE
from tools.react_native.rf_evidence_bundle import (
    MAX_ENTRIES,
    MAX_LINE_BYTES,
    MAX_LOG_BYTES,
    RAW_PREFIX,
    RECOVERY_RESOURCE_FIELDS,
    EvidenceError,
    _canonical,
    _recovery_json,
    _require,
    _safe_name,
)

PREFIX = RAW_PREFIX + "rf-adapter-transition:"
# Exact RfScanEvent keys from BleHubProbe.nitro.ts. Nitro serializes known
# optional DTO members as either absent or null, not as foreign route evidence.
_RF_EVENT_FIELDS = frozenset(
    {
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
    }
)
_RUNTIME = re.compile(
    r"runtime:runId=([^:\s]+):nonce=([0-9a-z]+-[0-9a-z]+-[0-9a-z]+):phase=(start|close)"
)
# Only report envelopes are checked here. Existing check_rf_scan owns rf-io fields.
_IO_PHASES = (
    "initialize",
    "scan-start",
    "advertisement",
    "scan-stop",
    "silence",
    "connecting",
    "connected",
    "gatt-discovery",
    "read-initial",
    "write-response",
    "read-response",
    "write-without-response",
    "read-without-response",
    "disconnected",
    "connection-silence",
    "close",
)
_IO_ENVELOPES = (
    "rf:start:",
    "rf:listener:registered-before-initialize",
    *(f"rf:step:{phase}:passed:" for phase in _IO_PHASES[:-1]),
    "rf:listener:removed",
    "rf:close:resolved",
    "rf:step:close:passed:",
    "rf-io:passed:",
)


def _uuid(value: Any) -> None:
    _require(type(value) is str, "UUID must be text")
    try:
        _require(str(uuid.UUID(value)) == value, "noncanonical UUID")
    except ValueError as error:
        raise EvidenceError("invalid UUID") from error


def _scan(text: str) -> tuple[list[bytes], list[dict[str, Any]], list[int]]:
    raw = text.encode("utf-8")
    _require(len(raw) <= MAX_LOG_BYTES, "adapter runtime log exceeds limit")
    lines = raw.splitlines(keepends=True)
    records, invalid = [], []
    for number, line in enumerate(lines, 1):
        _require(len(line) <= MAX_LINE_BYTES, "adapter runtime line exceeds limit")
        content = line.decode("utf-8").removesuffix("\n").removesuffix("\r")
        if RAW_PREFIX not in content:
            continue
        if (
            content.count(RAW_PREFIX) != 1
            or PREFIX not in content
            or not line.endswith(b"\n")
        ):
            invalid.append(number)
            continue
        records.append({"line": number, "marker": content.split(PREFIX, 1)[1]})
        _require(len(records) <= MAX_ENTRIES, "too many adapter runtime markers")
    return lines, records, invalid


def index_runtime(text: str, log_path: str | Path) -> dict[str, Any]:
    """Index inclusive raw off/on ranges, retaining failed/partial transcripts.

    log_path is an attempt-relative source label, never opened. Off starts at JS
    runtime start (there is no off:start marker) and ends just before on:started;
    on extends through EOF so late callbacks/diagnostics remain hash-bound.
    """
    source = _safe_name(str(log_path))
    lines, records, invalid = _scan(text)
    result: dict[str, Any] = {
        "scenario": "adapter-transition",
        "prefix": PREFIX,
        "flows": [],
        "events": records,
        "runtime_markers": [],
        "native_markers": [],
        "resources": [],
        "invalid_markers": invalid,
        "error_markers": [],
    }
    starts: dict[str, int] = {}
    for row in records:
        body, number = row["marker"], row["line"]
        runtime = _RUNTIME.fullmatch(body)
        if runtime:
            result["runtime_markers"].append(
                {
                    "run_id": runtime[1],
                    "nonce": runtime[2],
                    "phase": runtime[3],
                    "line": number,
                }
            )
            if runtime[3] == "start":
                starts.setdefault("off", number)
        if body == "on:started":
            starts.setdefault("on", number)
        native = re.fullmatch(r"native:(bootstrap|signal|arm):(.*)", body)
        resources = re.fullmatch(r"(off|on):resources:(.*)", body)
        if native or resources:
            try:
                payload = _recovery_json(native[2] if native else resources[2])
            except (ValueError, TypeError):
                invalid.append(number)
            else:
                if native:
                    if "adapter_mode" in payload:
                        result["adapter_mode"] = payload["adapter_mode"]
                    result["native_markers"].append(
                        {"kind": native[1], "payload": payload, "line": number}
                    )
                else:
                    result["resources"].append(
                        {"phase": resources[1], "snapshot": payload, "line": number}
                    )
        # Native/event JSON is opaque here: an expected adapter_unavailable
        # message may itself contain ':failed:' without being a failed marker.
        header = body.split("{", 1)[0]
        if (
            header.startswith("failed:")
            or ":failed:" in header
            or ":close:rejected:" in header
        ):
            result["error_markers"].append(number)
    for index, phase in enumerate(("off", "on")):
        start = starts.get(phase)
        end = (
            (starts["on"] - 1 if phase == "off" and "on" in starts else len(lines))
            if start
            else None
        )
        segment = b"".join(lines[start - 1 : end]) if start else b""
        terminals = [
            row["line"] for row in records if row["marker"] == phase + ":passed"
        ]
        bootstrap = next(
            (
                row["payload"]
                for row in result["native_markers"]
                if row["kind"] == "bootstrap"
            ),
            {},
        )
        runtime = next(iter(result["runtime_markers"]), {})
        result["flows"].append(
            {
                "index": index,
                "mode": phase,
                "source": source,
                "run_id": runtime.get("run_id"),
                "runtime_nonce": runtime.get("nonce"),
                "pid": bootstrap.get("pid"),
                "boot_id": bootstrap.get("boot_id"),
                "status": "passed"
                if start and len(terminals) == 1
                else "partial"
                if start
                else "missing",
                "start_line": start,
                "end_line": end,
                "terminal_line": terminals[0] if len(terminals) == 1 else None,
                "sha256": hashlib.sha256(segment).hexdigest() if start else None,
                "size": len(segment),
            }
        )
    return result


def _same(actual: Any, expected: Any, label: str) -> None:
    _require(_canonical(actual) == _canonical(expected), f"adapter {label} drift")


def _time(value: Any, label: str, *, positive: bool = False) -> int | float:
    _require(type(value) in (int, float), f"{label} must be numeric, not bool")
    try:
        valid = math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        valid = False
    _require(
        valid, f"{label} must be finite and {'positive' if positive else 'nonnegative'}"
    )
    return value


def verify_state_control(control: dict[str, Any], run_id: str, device: str) -> None:
    """Pure schema/order audit of the host's adapter-state-control.json.

    device is the explicit Android serial also recorded in the evidence bundle.
    initial/final allow only readbacks; off/on require exactly one svc command
    followed by one or more readbacks. Only the final sample must equal the target.
    Receipt times must fit ordered action envelopes. No timeout field is required
    or inferred: elapsed times do not prove that subprocess deadlines were set.
    """
    _uuid(run_id)
    _require(
        type(device) is str
        and bool(device)
        and not device.startswith("-")
        and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in device),
        "explicit Android serial required",
    )
    _require(
        type(control) is dict
        and set(control)
        == {
            "schema_version",
            "platform",
            "device",
            "run_id",
            "status",
            "initial_state",
            "final_state",
            "actions",
            "primary_error",
            "cleanup_errors",
        },
        "adapter state control schema",
    )
    for key, expected in {
        "schema_version": 1,
        "platform": "android",
        "device": device,
        "run_id": run_id,
        "status": "passed",
        "initial_state": "ON",
        "final_state": "ON",
        "primary_error": None,
        "cleanup_errors": [],
    }.items():
        _same(control[key], expected, f"state control {key}")
    actions = control["actions"]
    _require(
        type(actions) is list and len(actions) == 4, "four adapter actions required"
    )
    previous = 0
    count = 0
    for action, label, state in zip(
        actions,
        ("initial", "off", "on", "final"),
        ("ON", "OFF", "ON", "ON"),
        strict=True,
    ):
        _require(
            type(action) is dict
            and set(action)
            == {
                "label",
                "requested_state",
                "observed_state",
                "started_monotonic",
                "confirmed_monotonic",
                "receipts",
            },
            "adapter action schema",
        )
        _same(action["label"], label, "action label/order")
        _same(action["requested_state"], state, "action requested state")
        _same(action["observed_state"], state, "action observed state")
        start = _time(action["started_monotonic"], "action start")
        confirmed = _time(action["confirmed_monotonic"], "action confirmation")
        _require(previous <= start <= confirmed, "adapter action time order")
        receipts = action["receipts"]
        mutates = label in ("off", "on")
        _require(
            type(receipts) is list and len(receipts) >= (2 if mutates else 1),
            "adapter action receipts",
        )
        count += len(receipts)
        _require(count <= MAX_ENTRIES, "too many adapter command receipts")
        last = start
        for index, receipt in enumerate(receipts):
            _require(
                type(receipt) is dict
                and set(receipt)
                == {
                    "command",
                    "returncode",
                    "stdout",
                    "started_monotonic",
                    "finished_monotonic",
                },
                "adapter command receipt schema",
            )
            _same(receipt["returncode"], 0, "command returncode")
            stdout = receipt["stdout"]
            _require(type(stdout) is str, "command stdout must be text")
            began = _time(receipt["started_monotonic"], "command start")
            ended = _time(receipt["finished_monotonic"], "command finish")
            _require(
                last <= began <= ended <= confirmed,
                "adapter command/action time envelope",
            )
            command = receipt["command"]
            _require(
                type(command) is list
                and bool(command)
                and all(
                    type(arg) is str
                    and arg
                    and not any(ord(c) < 32 or ord(c) == 127 for c in arg)
                    for arg in command
                ),
                "adapter command argv",
            )
            _require(
                re.split(r"[\\/]", command[0])[-1] in ("adb", "adb.exe"),
                "adapter command must invoke adb",
            )
            svc = mutates and index == 0
            tail = (
                ["svc", "bluetooth", "disable" if label == "off" else "enable"]
                if svc
                else ["dumpsys", "bluetooth_manager"]
            )
            _same(
                command[1:],
                ["-s", device, "shell", *tail],
                "adapter command/device/order",
            )
            if not svc:
                states = [
                    match[1]
                    for line in stdout.splitlines()
                    if (
                        match := re.fullmatch(
                            r"\s*state:\s*(ON|OFF|TURNING_ON|TURNING_OFF|BLE_ON|BLE_TURNING_ON|BLE_TURNING_OFF)\s*",
                            line,
                        )
                    )
                ]
                _require(
                    len(states) == 1, "dumpsys must contain one unambiguous state line"
                )
                if index == len(receipts) - 1:
                    _same(states[0], state, "final dumpsys state")
            last = ended
        previous = confirmed


def verify_readiness(
    readiness: dict[str, Any], *, receipt_sha256: str, runtime: dict[str, Any]
) -> None:
    """Bind the provider's one-window ready sidecar to this receipt and JS boot.

    This audits a persisted claim, not a live no-peer/hold check or proof that
    the host actually waited before enabling the radio/sending its payload URL.
    """
    _uuid(runtime["run_id"])
    _require(
        type(readiness) is dict
        and set(readiness)
        == {
            "schema_version",
            "run_id",
            "status",
            "receipt_sha256",
            "native_bootstrap",
            "monotonic",
        }
        | ({"adapter_mode"} if "adapter_mode" in runtime else set()),
        "adapter readiness schema",
    )
    _require(
        type(receipt_sha256) is str
        and re.fullmatch(r"[0-9a-f]{64}", receipt_sha256) is not None,
        "adapter readiness receipt hash",
    )
    for key, expected in {
        "schema_version": 1,
        "run_id": runtime["run_id"],
        "status": "ready",
        "receipt_sha256": receipt_sha256,
        "native_bootstrap": runtime["native_bootstrap"],
    }.items():
        _same(readiness[key], expected, f"readiness {key}")
    if "adapter_mode" in runtime:
        _same(readiness.get("adapter_mode"), runtime["adapter_mode"], "readiness mode")
    _time(readiness["monotonic"], "adapter readiness time")


def verify_go(go: dict[str, Any], run_id: str, mode: str = "startup-off") -> None:
    """Check the immutable launch-budget declaration, not its publication time."""
    _uuid(run_id)
    _same(
        go,
        {
            "schema_version": 1,
            "run_id": run_id,
            **({"adapter_mode": mode} if "adapter_mode" in go else {}),
        },
        "adapter go schema/session",
    )
    _require(
        mode in ("startup-off", "active-loss")
        and (mode != "active-loss" or "adapter_mode" in go),
        "active go requires explicit mode",
    )


def verify_physical(
    physical: dict[str, Any],
    *,
    request: dict[str, Any],
    receipt: dict[str, Any],
    receipt_sha256: str,
    runtime: dict[str, Any],
) -> None:
    """Pure checks against nrf52840/adapter_transition.py's successful record.

    receipt_sha256 is the hash of unchanged fixture-receipt.json bytes (not a
    canonical JSON hash). Cross-process monotonic clocks are never compared.
    Fixture receipt freshness/live-under-lease attestation remains an online gate.
    """
    _require(
        all(type(value) is dict for value in (physical, request, receipt, runtime)),
        "adapter physical proof objects",
    )
    run_id = runtime["run_id"]
    _uuid(run_id)
    for value in (request, receipt, physical):
        _same(value.get("schema_version"), 1, "physical/session schema")
        _same(value.get("session_id"), run_id, "physical/session identity")
    active = runtime.get("adapter_mode") == "active-loss"
    if active:
        _same(physical.get("adapter_mode"), "active-loss", "physical adapter mode")
        verify_active_physical(physical, runtime, receipt_sha256)
    else:
        _same(
            physical.get("adapter_mode", "startup-off"),
            "startup-off",
            "startup physical mode",
        )
    for key, expected in {
        "mode": "adapter-transition-attested",
        "run_id": run_id,
        "status": "passed",
        "connections": 2 if active else 1,
        "disconnections": 2 if active else 1,
        "rejected_events": [],
        "primary_error": None,
        "cleanup_errors": [],
        "cleanup": {
            "advertising_stopped": True,
            "hold_inactive": True,
            "peer_count": 0,
        },
    }.items():
        _require(key in physical, f"missing physical {key}")
        _same(physical[key], expected, f"physical {key}")
    _same(
        receipt.get("proof_method"),
        "btp-vendor-live-under-device-lease",
        "fixture receipt method",
    )
    _require(
        type(receipt_sha256) is str
        and re.fullmatch(r"[0-9a-f]{64}", receipt_sha256) is not None,
        "fixture receipt hash",
    )
    _same(
        physical.get("receipt_sha256"), receipt_sha256, "physical fixture receipt hash"
    )
    for key in ("contract_sha256", "profile_sha256"):
        expected = request.get(key)
        _require(
            type(expected) is str
            and re.fullmatch(r"[0-9a-f]{64}", expected) is not None,
            f"fixture {key}",
        )
        for value in (receipt, physical):
            _same(value.get(key), expected, f"physical/session {key}")
    for key in ("resource_identity", "port"):
        expected = request.get(key)
        _require(type(expected) is str and bool(expected.strip()), f"fixture {key}")
        _same(receipt.get(key), expected, f"fixture receipt {key}")
        if key == "resource_identity":
            _same(physical.get(key), expected, f"physical {key}")
    _same(
        request.get("service_uuid"), runtime["service_uuid"], "fixture runtime service"
    )
    for key in ("fixture_pid", "fixture_pgid"):
        value = physical.get(key)
        _require(type(value) is int and value > 0, f"positive physical {key}")
        _same(receipt.get(key), value, f"fixture receipt {key}")
    for key in ("native_bootstrap", "native_signal"):
        _same(physical.get(key), runtime[key], f"physical {key}")
    _same(
        physical.get("resources"),
        runtime["closed_resources"],
        "physical closed resources",
    )
    hold = physical.get("hold_status")
    if active:
        from tests.peripherals.nrf52840.fault_control import (
            cleanup_ready,
            status_from_document,
        )

        _require(
            cleanup_ready(status_from_document(hold)),
            "physical v2 resource-zero receipt",
        )
    else:
        _same(
            hold, {"protocol_version": 2, "engine_flags": 0}, "physical v2 idle engine"
        )
    links = physical.get("events")
    _require(
        type(links) is list and len(links) == (4 if active else 2),
        "exact physical GAP pairs",
    )
    readiness = physical.get("readiness")
    verify_readiness(readiness, receipt_sha256=receipt_sha256, runtime=runtime)
    previous = (
        physical["boundaries"][0]["monotonic"] if active else readiness["monotonic"]
    )
    on_started = _time(physical.get("on-started"), "physical on-started", positive=True)
    _require(previous <= on_started, "physical on-started preceded readiness")
    # The provider drains GAP before refreshing logcat. A valid connection can
    # therefore precede its observation of the JS on:started marker. Readiness,
    # not delayed log observation, is the connection-window authority.
    for event, kind in zip(
        links, ("connected", "disconnected") * (2 if active else 1), strict=True
    ):
        _require(
            type(event) is dict
            and set(event) == {"kind", "peer", "payload_hex", "monotonic"},
            "physical GAP event schema",
        )
        _same(event["kind"], kind, "physical GAP order")
        peer = event["peer"]
        _require(
            type(peer) is list
            and len(peer) == 2
            and type(peer[0]) is str
            and re.fullmatch(r"[0-9a-f]{12}", peer[0]) is not None
            and type(peer[1]) is int
            and peer[1] in (0, 1, 2, 3),
            "physical GAP peer",
        )
        payload = event["payload_hex"]
        size = 26 if kind == "connected" else 14
        _require(
            type(payload) is str
            and re.fullmatch(r"[0-9a-f]{" + str(size) + r"}", payload) is not None,
            "physical GAP payload",
        )
        wire = bytes.fromhex(payload)
        _same([wire[1:7][::-1].hex(), wire[0]], peer, "physical GAP wire peer")
        moment = _time(event["monotonic"], "physical GAP time", positive=True)
        _require(previous < moment, "physical GAP/readiness time order")
        previous = moment
    _same(links[0]["peer"], links[1]["peer"], "physical GAP same peer")
    if active:
        _same(links[2]["peer"], links[3]["peer"], "recovery GAP same peer")
        _require(
            links[1]["monotonic"] < readiness["monotonic"] < links[2]["monotonic"],
            "physical loss/recovery window order",
        )
        return
    _same(
        physical.get("boundaries"),
        [
            {
                "boundary": "on-window-ready",
                "cycle": -1,
                "monotonic": readiness["monotonic"],
                "hold_inactive": True,
                "peer_count": 0,
            },
            *[
                {"boundary": "gap-" + event["kind"], "cycle": -1, **event}
                for event in links
            ],
        ],
        "physical GAP raw boundaries",
    )


def verify_active_physical(
    physical: dict[str, Any], runtime: dict[str, Any], receipt_sha256: str
) -> None:
    """Audit the owned WAITING/OFF/release chain; never compare cross-process clocks."""
    from tests.peripherals.nrf52840.adapter_transition import (
        ARM_NAME,
        LOSS_NAME,
        OFF_CONFIRMED,
        OFF_READY,
        OFF_REQUEST,
    )

    protocol = physical.get("active_protocol")
    _require(
        type(protocol) is dict
        and set(protocol)
        == {ARM_NAME, LOSS_NAME, OFF_REQUEST, OFF_READY, OFF_CONFIRMED},
        "complete active physical handshake",
    )
    armed, waiting, request, authorized, confirmed = (
        protocol[n]
        for n in (ARM_NAME, LOSS_NAME, OFF_REQUEST, OFF_READY, OFF_CONFIRMED)
    )
    for value, phase, extras in (
        (armed, "armed", {"ticket", "status", "peer", "route"}),
        (waiting, "waiting", {"ticket", "status", "peer", "route"}),
        (authorized, "off-authorized", {"ticket", "status", "peer", "request_sha256"}),
    ):
        _require(
            type(value) is dict
            and set(value)
            == {
                "schema_version",
                "adapter_mode",
                "run_id",
                "phase",
                "receipt_sha256",
                "native_bootstrap",
            }
            | extras,
            "active physical ACK schema",
        )
        for key, expected in {
            "schema_version": 1,
            "adapter_mode": "active-loss",
            "run_id": runtime["run_id"],
            "phase": phase,
            "receipt_sha256": receipt_sha256,
            "native_bootstrap": runtime["native_bootstrap"],
        }.items():
            _same(value.get(key), expected, f"active ACK {key}")
        _same(value.get("ticket"), armed.get("ticket"), "active v2 ticket generation")
        _same(value.get("peer"), armed.get("peer"), "active exact peer")
    for value, hash_key in (
        (request, "loss_ready_sha256"),
        (confirmed, "off_ready_sha256"),
    ):
        _require(
            type(value) is dict
            and set(value) == {"schema_version", "adapter_mode", "run_id", hash_key},
            "active host declaration schema",
        )
        for key, expected in {
            "schema_version": 1,
            "adapter_mode": "active-loss",
            "run_id": runtime["run_id"],
        }.items():
            _same(value.get(key), expected, f"active host declaration {key}")
        _require(
            type(value[hash_key]) is str
            and re.fullmatch(r"[0-9a-f]{64}", value[hash_key]) is not None,
            "active ACK bytes hash",
        )
    _require(
        type(authorized["request_sha256"]) is str
        and re.fullmatch(r"[0-9a-f]{64}", authorized["request_sha256"]) is not None,
        "active request bytes hash",
    )
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

    identity = armed["ticket"]
    ticket = ticket_from_document(identity)
    _require(
        ticket.owner_nonce == owner_nonce(runtime["run_id"])
        and ticket.operation is Operation.READ,
        "active v2 owner/operation",
    )
    _same(
        physical.get("handles", {}).get("read-write"),
        ticket.handle,
        "active provisioned target",
    )
    for value, phase in (
        (armed, Phase.ARMED),
        (waiting, Phase.HELD),
        (authorized, Phase.HELD),
    ):
        status = status_from_document(value["status"])
        _require(
            status.ticket == ticket and status.phase is phase,
            "active exact v2 phase/ticket",
        )
        _same(
            [status.peer[1:7][::-1].hex(), status.peer[0]],
            armed["peer"],
            "active v2 peer",
        )
    for value in (armed, waiting):
        _same(value.get("route"), runtime["active_loss"]["route"], "active route")
    retired = status_from_document(physical["hold_status"])
    _require(
        retired.ticket == ticket
        and cleanup_ready(retired)
        and retired.winner is Winner.DISCONNECT
        and retired.outcome is Outcome.DISCONNECTED_UNCOMMITTED,
        "successful retained disconnect/resource-zero receipt",
    )
    boundaries = physical.get("boundaries")
    _require(
        type(boundaries) is list and len(boundaries) == 12, "active boundary count"
    )

    required = [
        "active-window-ready",
        "gap-connected",
        "active-armed",
        "active-waiting",
        "active-off-authorized",
        "active-off-confirmed",
        "active-settled",
        "active-retired",
        "on-window-ready",
        "gap-connected",
        "gap-disconnected",
    ]
    # The first GAP disconnect can be delivered during OFF or after RELEASE. It
    # must follow authorization, and must precede the fresh recovery window.
    gaps = [b for b in boundaries if b.get("boundary", "").startswith("gap-")]
    _require(len(gaps) == 4, "two active/recovery GAP pairs")
    first_loss = gaps[1]
    _require(first_loss.get("boundary") == "gap-disconnected", "first loss GAP kind")
    trimmed = list(boundaries)
    trimmed.remove(first_loss)
    _same([b.get("boundary") for b in trimmed], required, "active causal boundaries")
    times = [
        _time(b.get("monotonic"), "active boundary time", positive=True)
        for b in boundaries
    ]
    _require(all(a <= b for a, b in pairwise(times)), "active boundary order")
    named = {b["boundary"]: b for b in trimmed}
    _require(
        named["active-off-authorized"]["monotonic"]
        < first_loss["monotonic"]
        < named["on-window-ready"]["monotonic"],
        "loss outside authorized window",
    )
    waiting_at = named["active-waiting"]["monotonic"]
    _require(
        named["active-off-authorized"]["monotonic"] - waiting_at < 3
        and named["active-off-confirmed"]["monotonic"] - waiting_at < 8
        and named["active-settled"]["monotonic"] - waiting_at < 8,
        "active pre-watchdog budget",
    )
    for name in (
        "active-armed",
        "active-waiting",
        "active-off-authorized",
        "active-off-confirmed",
        "active-retired",
    ):
        _same(named[name].get("ticket"), identity, f"{name} ticket")
    authorized_status = status_from_document(named["active-off-authorized"]["status"])
    _require(
        authorized_status.ticket == ticket and authorized_status.phase is Phase.HELD,
        "fault fresh HELD",
    )
    _same(
        named["active-retired"]["status"],
        physical["hold_status"],
        "retained retirement proof",
    )
    for name in ("active-waiting", "active-off-authorized"):
        _same(named[name].get("peer"), armed["peer"], "held physical peer")
    _same(
        named["active-settled"].get("policy"),
        runtime["active_loss"]["policy"],
        "native retirement policy",
    )
    _same(
        [
            {
                "kind": b["kind"],
                "peer": b["peer"],
                "payload_hex": b["payload_hex"],
                "monotonic": b["monotonic"],
            }
            for b in gaps
        ],
        physical.get("events"),
        "active raw GAP boundaries",
    )
    _same(gaps[0]["peer"], armed["peer"], "held GAP peer")


def verify_runtime(
    text: str, run_id: str, service_uuid: str, log_path: Path
) -> dict[str, Any]:
    """Validate one complete transcript without I/O, device access or clock reads.

    text is unchanged UTF-8 raw capture or its line-text projection; run_id and
    service_uuid must be canonical lowercase UUIDs. log_path is diagnostic only.
    Raises EvidenceError on foreign/duplicate/unknown/out-of-order records.
    Returns a JSON-serializable schema-v1 runtime ledger for the host to persist.
    """
    _uuid(run_id)
    _uuid(service_uuid)
    _, records, invalid = _scan(text)
    _require(not invalid, f"foreign/ambiguous/truncated adapter markers: {invalid}")
    position = 0

    def take(prefix: str, *, exact: bool = False) -> str:
        nonlocal position
        _require(position < len(records), f"missing adapter marker: {prefix}")
        marker = records[position]["marker"]
        _require(
            marker == prefix if exact else marker.startswith(prefix),
            f"unexpected adapter marker at {records[position]['line']}: expected {prefix}, got {marker}",
        )
        position += 1
        return marker[len(prefix) :]

    boot = _recovery_json(take("native:bootstrap:"))
    _require(
        set(boot)
        == {"run_id", "pid", "boot_id", "bundle_id", "executable", "source"}
        | (
            {"adapter_mode"}
            if boot.get("adapter_mode") in ("startup-off", "active-loss")
            else set()
        ),
        "native bootstrap keys",
    )
    _uuid(boot["boot_id"])
    _require(
        boot["run_id"] == run_id
        and type(boot["pid"]) is int
        and 0 < boot["pid"] <= 2_147_483_647
        and boot["bundle_id"] == ANDROID_PACKAGE
        and boot["source"] == "launch",
        "native bootstrap identity",
    )
    executable = boot["executable"]
    _require(
        type(executable) is str
        and re.fullmatch(r"file:/+[^\s?#]+\.apk", executable) is not None,
        "native installed APK executable URI",
    )
    runtime = _RUNTIME.fullmatch("runtime:" + take("runtime:"))
    _require(
        runtime is not None and runtime[1] == run_id and runtime[3] == "start",
        "runtime start identity/nonce",
    )
    nonce = runtime[2]
    active_result: dict[str, Any] | None = None
    if boot.get("adapter_mode") == "active-loss":
        from tests.peripherals.nrf52840.adapter_transition import ActiveReadFacts

        on_start = next(
            (
                r["marker"]
                for r in records
                if r["marker"].startswith("on:flow:rf:start:")
            ),
            "",
        )
        name = re.search(r":expectedLocalName=([^:\r\n]+):initCommandId=", on_start)
        _require(name is not None, "missing frozen active/recovery local name")
        facts = ActiveReadFacts(run_id, service_uuid, name[1])
        # Offline validation of the resulting transcript does not reauthorize OFF;
        # the separately checked live handshake owns that causal boundary.
        facts.loss_authorized = True
        try:
            while position < len(records) and (
                records[position]["marker"].startswith("active:")
                or records[position]["marker"].startswith("native:arm:")
            ):
                facts.observe(
                    records[position]["marker"], boot, require_readiness=False
                )
                position += 1
        except (ValueError, RuntimeError, KeyError, TypeError, StopIteration) as error:
            raise EvidenceError(f"active-loss transcript: {error}") from error
        _require(
            facts.done, "active loss missing settlement/pre-close resources/silence"
        )
        active_result = {
            "events": facts.events,
            "route": facts.discovery,
            "native_arm": facts.arm,
            "policy": facts.settled,
            "retired_resources": facts.retired,
            "accepted": facts.accepted,
        }
    off_events = []
    for kind, command in (
        ()
        if active_result is not None
        else (
            ("adapterSnapshotChanged", "31000"),
            ("commandCompleted", "31000"),
            ("commandCompleted", "31002"),
        )
    ):
        event = _recovery_json(take("off:event:"))
        expected = {"kind": kind, "commandId": command}
        if kind == "adapterSnapshotChanged":
            expected.update(adapterState="PoweredOff", scanAuthorization="Authorized")
        _require(
            set(event) <= _RF_EVENT_FIELDS
            and all(
                type(event.get(key)) is str and event[key] == value
                for key, value in expected.items()
            )
            and all(
                value is None
                for key, value in event.items()
                if key not in expected and key != "error"
            ),
            "off event kind/command/state/unknown or nonnull foreign fields",
        )
        if command == "31000":
            _require(event.get("error") is None, "off initialize must succeed")
        else:
            error = event.get("error")
            _require(
                type(error) is dict
                and {"category", "message"} <= set(error)
                and set(error) <= {"category", "message", "platformDetails"}
                and error.get("category") == "adapter_unavailable"
                and type(error.get("message")) is str,
                "off accepted scan terminal category/error projection",
            )
            if error.get("platformDetails") is not None:
                details = error["platformDetails"]
                _require(
                    type(details) is dict
                    and details.get("platform") == "platform_android"
                    and set(details) <= {"platform", "code", "name", "message"}
                    and all(
                        value is None or type(value) is str
                        for key, value in details.items()
                        if key != "platform"
                    ),
                    "off error platform details",
                )
        off_events.append(event)

    def resources(phase: str) -> dict[str, Any]:
        snapshot = _recovery_json(take(phase + ":resources:"))
        _require(
            set(snapshot) == set(RECOVERY_RESOURCE_FIELDS)
            and all(type(v) is int and v == 0 for v in snapshot.values()),
            f"{phase} closed resources require exactly seventeen integer zeros",
        )
        return snapshot

    off_resources = resources("off")
    take("off:passed", exact=True)
    take(f"awaiting-on:runId={run_id}", exact=True)
    signal = _recovery_json(take("native:signal:"))
    _require(
        signal
        == {
            **boot,
            "source": "payload-url",
            "url": f"blehub-rn-spike://adapter/{run_id}/"
            + ("active-loss/on" if active_result is not None else "on"),
        }
        and type(signal.get("pid")) is int,
        "native signal identity/url/source drift",
    )
    take(f"on-signal:runId={run_id}:source=linking", exact=True)
    take("on:started", exact=True)
    flow = []
    while position < len(records) and records[position]["marker"].startswith(
        "on:flow:"
    ):
        flow.append(take("on:flow:"))
    _require(
        len(flow) == len(_IO_ENVELOPES),
        "on flow report count (missing/duplicate/unknown)",
    )
    for marker, envelope in zip(flow, _IO_ENVELOPES, strict=True):
        _require(
            marker == envelope
            if not envelope.endswith(":")
            else marker.startswith(envelope),
            f"unknown/out-of-order on flow report: {marker}",
        )
    _require(
        re.fullmatch(
            rf"rf:start:serviceUuid={re.escape(service_uuid)}:expectedLocalName=[^:\r\n]+:"
            r"initCommandId=9001:startCommandId=9002:stopCommandId=9003:scanSessionId=9004",
            flow[0],
        )
        is not None,
        "on flow launch service/command identity",
    )
    projection = "".join(RAW_PREFIX + marker + "\n" for marker in flow)
    try:
        check_runtime_log.check_rf_scan(
            projection, service_uuid, log_path, connect=True, gatt=True, io=True
        )
    except AssertionError as error:
        raise EvidenceError(f"on rf-io: {error}") from error
    on_resources = resources("on")
    take("on:passed", exact=True)
    take(f"runtime:runId={run_id}:nonce={nonce}:phase=close", exact=True)
    take(f"passed:runId={run_id}", exact=True)
    _require(
        position == len(records), "late/duplicate/foreign adapter records after passed"
    )
    return {
        "schema_version": 1,
        "platform": "android",
        "scenario": "adapter-transition",
        **({"adapter_mode": boot["adapter_mode"]} if "adapter_mode" in boot else {}),
        **({"active_loss": active_result} if active_result is not None else {}),
        "status": "passed",
        "run_id": run_id,
        "service_uuid": service_uuid,
        "runtime_nonce": nonce,
        "pid": boot["pid"],
        "boot_id": boot["boot_id"],
        "native_bootstrap": boot,
        "native_signal": signal,
        "runtime_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "off_events": off_events,
        "closed_resources": {"off": off_resources, "on": on_resources},
        "on_flow_sha256": hashlib.sha256(projection.encode("utf-8")).hexdigest(),
        "counts": {
            "app_runtimes": 1,
            "hub_probe_lifecycles": 2,
            "off_events": len(off_events),
            "closed_zero_snapshots": 2,
        },
    }
