"""Read-only iOS startup-off transcript audit, never an OS/physical/RF gate.

Wire authorities: AdapterStartupGate.swift and rf_adapter_transition.tsx.
Native identities and URL acceptances are cooperative records, not attestation.
The journal reader owns envelope validation and retains the original bytes.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from tools.react_native import check_runtime_log
from tools.react_native.host_contract import BUNDLE_ID
from tools.react_native.ios_adapter_startup_journal import PREFIX, AdapterStartupJournal
from tools.react_native.rf_evidence_bundle import (
    RAW_PREFIX,
    RECOVERY_RESOURCE_FIELDS,
    EvidenceError,
    _recovery_json,
    _require,
)

_UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
_NONCE = re.compile(r"[0-9a-z]+-[0-9a-z]+-[0-9a-z]+")
# RfScanEvent optional members may be absent or null, never foreign route data.
_EVENT_FIELDS = frozenset(
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
# Only the report envelopes belong here; check_rf_scan owns their RF/IO fields.
_IO_PHASES = (
    *check_runtime_log.RF_PHASES[:-1],
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
)
_IO_ENVELOPES = (
    "rf:start:",
    "rf:listener:registered-before-initialize",
    *(f"rf:step:{phase}:passed:" for phase in _IO_PHASES),
    "rf:listener:removed",
    "rf:close:resolved",
    "rf:step:close:passed:",
    "rf-io:passed:",
)


def _uuid(value: Any, label: str) -> None:
    _require(type(value) is str and _UUID.fullmatch(value) is not None, label)


def _executable(value: Any) -> None:
    _require(
        type(value) is str
        and re.fullmatch(r"file:///[^\s?#\\]+", value) is not None
        and re.search(r"%(?![0-9a-fA-F]{2})", value) is None,
        "native executable must be a local file URL",
    )
    try:
        decoded = unquote(value, errors="strict")
    except UnicodeError as error:
        raise EvidenceError("native executable URL encoding") from error
    _require(
        not decoded.endswith("/")
        and not any(unicodedata.category(char) in {"Cc", "Cf"} for char in decoded),
        "native executable file URL path/control characters",
    )


def _json(content: str) -> dict[str, Any]:
    try:
        return _recovery_json(content)
    except (ValueError, TypeError, RecursionError) as error:
        raise EvidenceError(f"invalid startup-off marker JSON: {error}") from error


def _native_identity(boot: Any, run_id: str) -> None:
    _require(
        type(boot) is dict
        and set(boot)
        == {
            "run_id",
            "adapter_mode",
            "pid",
            "boot_id",
            "bundle_id",
            "executable",
            "source",
        },
        "native bootstrap exact schema",
    )
    _uuid(boot["boot_id"], "canonical native boot_id required")
    _require(
        boot["run_id"] == run_id
        and boot["adapter_mode"] == "startup-off"
        and type(boot["pid"]) is int
        and 0 < boot["pid"] <= 2_147_483_647
        and boot["bundle_id"] == BUNDLE_ID
        and boot["source"] == "launch",
        "native bootstrap identity/mode/PID/bundle/source",
    )
    _executable(boot["executable"])


def _nonce(value: Any) -> None:
    _require(
        type(value) is str
        and len(value) <= 128
        and _NONCE.fullmatch(value) is not None,
        "runtime canonical three-part base36 nonce",
    )


def _identity(value: Any, run_id: str) -> dict:
    _require(
        type(value) is dict and set(value) == {"native", "run_id", "nonce"},
        "startup-off identity exact schema",
    )
    _require(value["run_id"] == run_id, "startup-off identity run_id")
    _native_identity(value["native"], run_id)
    _nonce(value["nonce"])
    return value


def _receipt_sha256(value: Any) -> None:
    _require(
        type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        "canonical fixture receipt SHA-256 required",
    )


def verify_window_ready(value: dict, run_id: str, receipt_sha256: str) -> dict:
    """Validate a cooperative window attachment and return its copied identity.

    The caller owns strict JSON decoding (``_json`` for attachment text), receipt
    hashing and source capture. This is not independent PID or Settings proof.
    """
    _uuid(run_id, "canonical window run_id required")
    _receipt_sha256(receipt_sha256)
    _require(
        type(value) is dict
        and set(value)
        == {"schema_version", "run_id", "fixture_receipt_sha256", "identity"},
        "startup-off window exact schema",
    )
    _require(
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["run_id"] == run_id
        and value["fixture_receipt_sha256"] == receipt_sha256,
        "startup-off window version/run/receipt",
    )
    return copy.deepcopy(_identity(value["identity"], run_id))


# (action, requires the same runtime identity, Settings Switch readback).
_CONTROL_STEPS = (
    ("authorization-verified", False, None),
    ("settings-initial-on", False, "1"),
    ("settings-off", False, "0"),
    ("off-probe-closed", True, None),
    ("settings-on", False, "1"),
    ("original-session-returned", True, None),
    ("host-window-requested", True, None),
    ("host-window-ready", True, None),
    ("on-requested", True, None),
    ("app-runtime-passed", True, None),
    ("settings-final-on", False, "1"),
    ("original-result-returned", True, None),
)


def verify_control(control: dict, *, runtime: dict, receipt_sha256: str) -> dict:
    """Audit only a complete successful cooperative UI control observation.

    ``runtime`` is the separately audited runtime ledger. Identity fields are UI
    source records bound to that ledger, not independent process provenance.
    Failed attempts (including cleanup-restored-on) never become passing audits.
    Settings/runtime source evidence still needs binding in the final bundle;
    this function performs no I/O, deadline checks, RF or cleanup guarantees.
    """
    _require(type(runtime) is dict, "startup-off runtime must be an object")
    run_id = runtime.get("run_id")
    _uuid(run_id, "canonical runtime run_id required")
    identity = _identity(
        {
            "native": runtime.get("native_bootstrap"),
            "run_id": run_id,
            "nonce": runtime.get("runtime_nonce"),
        },
        run_id,
    )
    _receipt_sha256(receipt_sha256)
    _require(
        type(control) is dict
        and set(control)
        == {
            "schema_version",
            "scope",
            "run_id",
            "fixture_receipt_sha256",
            "status",
            "mutation_attempted",
            "restored",
            "same_session",
            "observations",
            "primary_error",
            "cleanup_error",
        },
        "startup-off control exact schema",
    )
    _require(
        type(control["schema_version"]) is int
        and control["schema_version"] == 1
        and control["scope"] == "ios-adapter-startup-control-window"
        and control["run_id"] == run_id
        and control["fixture_receipt_sha256"] == receipt_sha256,
        "startup-off control version/scope/run/receipt",
    )
    _require(
        control["status"] == "passed"
        and all(
            control[key] is True
            for key in ("mutation_attempted", "restored", "same_session")
        )
        and control["primary_error"] == ""
        and control["cleanup_error"] == "",
        "startup-off control requires complete success without errors",
    )
    observations = control["observations"]
    _require(
        type(observations) is list and len(observations) == len(_CONTROL_STEPS),
        "startup-off control requires exactly twelve observations",
    )
    previous = 0
    for observation, (action, identified, switch) in zip(observations, _CONTROL_STEPS):
        _require(
            type(observation) is dict
            and set(observation)
            == {"action", "elapsed_seconds", "identity", "switch_value"},
            "startup-off control observation exact schema",
        )
        _require(
            observation["action"] == action and observation["switch_value"] == switch,
            f"startup-off control action/switch: {action}",
        )
        elapsed = observation["elapsed_seconds"]
        _require(
            (type(elapsed) is int or type(elapsed) is float and math.isfinite(elapsed))
            and elapsed >= previous,
            f"startup-off control finite nonnegative nondecreasing elapsed: {action}",
        )
        previous = elapsed
        if identified:
            observed = _identity(observation["identity"], run_id)
            _require(observed == identity, f"startup-off control identity: {action}")
        else:
            _require(
                observation["identity"] is None,
                f"startup-off control unexpected identity: {action}",
            )
    return {
        "schema_version": 1,
        "scope": "cooperative-control-observations-only",
        "control_status": "passed",
        "run_id": run_id,
        "fixture_receipt_sha256": receipt_sha256,
        "identity": copy.deepcopy(identity),
        "observations": copy.deepcopy(observations),
        "explicit_limits": {
            "identity": "UI-sourced native PID/boot and JS nonce binding only, not independent PID provenance.",
            "evidence": "Settings UI and runtime source evidence still require final bundle binding; no physical RF or overall bundle verdict.",
            "cleanup": "Cooperative restored observation only, not a cleanup guarantee.",
            "timing": "Nondecreasing elapsed observations only, not deadlines or absolute cross-clock correlation.",
        },
    }


def _off_event(event: dict[str, Any], kind: str, command: str) -> None:
    expected = {"kind": kind, "commandId": command}
    if kind == "adapterSnapshotChanged":
        expected.update(adapterState="PoweredOff", scanAuthorization="Authorized")
    _require(
        set(event) <= _EVENT_FIELDS
        and all(
            type(event.get(key)) is str and event[key] == value
            for key, value in expected.items()
        )
        and all(
            value is None
            for key, value in event.items()
            if key not in expected and key != "error"
        ),
        "off event kind/command/state/unknown or nonnull foreign DTO fields",
    )
    if command == "31000":
        _require(event.get("error") is None, "off initialize must succeed")
        return
    error = event.get("error")
    _require(
        type(error) is dict
        and {"category", "message"} <= set(error)
        and set(error) <= {"category", "message", "platformDetails"}
        and error["category"] == "adapter_unavailable"
        and type(error["message"]) is str,
        "off accepted scan requires adapter_unavailable terminal DTO",
    )
    details = error.get("platformDetails")
    if details is not None:
        _require(
            type(details) is dict
            and details.get("platform") == "platform_apple"
            and set(details) <= {"platform", "code", "name", "message"}
            and all(value is None or type(value) is str for value in details.values()),
            "off error Apple platform details DTO",
        )


def verify_off_journal(journal: AdapterStartupJournal) -> dict[str, Any]:
    """Audit the exact durable OFF prefix before host ready authorizes any RF."""
    return _audit_journal(journal, None)


def verify_runtime(journal: AdapterStartupJournal, service_uuid: str) -> dict[str, Any]:
    """Audit the complete transcript, including the same strict OFF prefix."""
    return _audit_journal(journal, service_uuid)


def _audit_journal(
    journal: AdapterStartupJournal, service_uuid: str | None
) -> dict[str, Any]:
    """Audit a cooperative transcript without I/O or clocks.

    Raises EvidenceError for journal errors or missing, duplicate, foreign,
    unknown, failed or out-of-order records. A runtime pass does not establish
    Settings state, physical control, BLE delivery, RF pass or independent PID
    provenance. Neither raw journal bytes nor record payloads are rewritten.
    """
    _require(journal.error is None, "startup-off journal retained a nonnull error")
    run_id = journal.run_id
    _uuid(run_id, "canonical journal run_id required")
    if service_uuid is not None:
        _uuid(service_uuid, "canonical service UUID required")
    records = journal.records
    _require(
        all(
            record.startswith(PREFIX)
            and record.count(RAW_PREFIX) == 1
            and "\r" not in record
            and "\n" not in record
            for record in records
        ),
        "foreign/ambiguous startup-off record prefix",
    )
    position = 0

    def take(prefix: str, *, exact: bool = False) -> str:
        nonlocal position
        _require(position < len(records), f"missing startup-off marker: {prefix}")
        marker = records[position][len(PREFIX) :]
        _require(
            marker == prefix if exact else marker.startswith(prefix),
            f"unexpected startup-off marker at record {position + 1}: expected {prefix}, got {marker}",
        )
        position += 1
        return marker[len(prefix) :]

    boot = _json(take("native:bootstrap:"))
    _native_identity(boot, run_id)
    start = take(f"runtime:runId={run_id}:nonce=")
    nonce = start.removesuffix(":phase=start")
    _require(start == nonce + ":phase=start", "runtime start phase")
    _nonce(nonce)
    off_events = []
    for kind, command in (
        ("adapterSnapshotChanged", "31000"),
        ("commandCompleted", "31000"),
        ("commandCompleted", "31002"),
    ):
        event = _json(take("off:event:"))
        _off_event(event, kind, command)
        off_events.append(event)

    def resources(phase: str) -> dict[str, Any]:
        snapshot = _json(take(phase + ":resources:"))
        _require(
            set(snapshot) == set(RECOVERY_RESOURCE_FIELDS)
            and all(type(value) is int and value == 0 for value in snapshot.values()),
            f"{phase} closed resources require exactly seventeen integer zeros",
        )
        return snapshot

    off_resources = resources("off")
    take("off:passed", exact=True)
    take(f"awaiting-on:runId={run_id}", exact=True)
    if service_uuid is None:
        _require(
            position == len(records),
            "OFF journal contains early/extra ready or ON records",
        )
        return {
            "schema_version": 1,
            "run_id": run_id,
            "native_bootstrap": boot,
            "runtime_nonce": nonce,
            "journal_sha256": journal.sha256,
            "resources": off_resources,
        }

    def acceptance(kind: str, path: str) -> dict[str, Any]:
        native = _json(take(f"native:{kind}:"))
        _require(
            native
            == {
                **boot,
                "source": "application-open",
                "url": f"blehub-rn-spike://adapter/{run_id}/{path}",
                "nonce": nonce,
            }
            and type(native.get("pid")) is int,
            f"native {kind} exact identity/nonce/url/source",
        )
        return native

    ready = acceptance("ready", "ready")
    take(f"host-window-ready:runId={run_id}:source=linking", exact=True)
    take(f"on-request:runId={run_id}:source=ui", exact=True)
    signal = acceptance("signal", "on")
    take(f"on-signal:runId={run_id}:source=linking", exact=True)
    take("on:started", exact=True)
    flow = []
    for envelope in _IO_ENVELOPES:
        payload = take("on:flow:")
        _require(
            payload.startswith(envelope)
            if envelope.endswith(":")
            else payload == envelope,
            f"unknown/out-of-order on flow report: {payload}",
        )
        _require(
            not any(
                marker in payload for marker in check_runtime_log.FORBIDDEN_MARKERS
            ),
            "failed on rf-io report",
        )
        flow.append(payload)
    launch = re.fullmatch(
        rf"rf:start:serviceUuid={re.escape(service_uuid)}:expectedLocalName=(.+):"
        r"initCommandId=9001:startCommandId=9002:stopCommandId=9003:scanSessionId=9004",
        flow[0],
    )
    _require(launch is not None, "on flow launch service/command identity")
    local_name = launch[1]
    _require(
        local_name == local_name.strip() and len(local_name.encode("utf-8")) <= 128,
        "on flow launch bounded local name",
    )
    # Remove only the enclosing adapter envelope; never normalize/repair payloads.
    projection = "".join(RAW_PREFIX + payload + "\n" for payload in flow)
    try:
        check_runtime_log.check_rf_scan(
            projection,
            service_uuid,
            Path("ios-adapter-startup-journal"),
            connect=True,
            gatt=True,
            io=True,
            expected_local_name=local_name,
        )
    except AssertionError as error:
        raise EvidenceError(f"on rf-io: {error}") from error
    on_resources = resources("on")
    take("on:passed", exact=True)
    take(f"runtime:runId={run_id}:nonce={nonce}:phase=close", exact=True)
    take(f"passed:runId={run_id}", exact=True)
    _require(
        position == len(records),
        "late/duplicate/foreign startup-off records after passed",
    )
    return {
        "schema_version": 1,
        "platform": "ios",
        "scenario": "adapter-transition",
        "adapter_mode": "startup-off",
        "scope": "cooperative-runtime-transcript-only",
        "runtime_status": "passed",
        "run_id": run_id,
        "service_uuid": service_uuid,
        "runtime_nonce": nonce,
        "pid": boot["pid"],
        "boot_id": boot["boot_id"],
        "native_bootstrap": boot,
        "native_ready": ready,
        "native_signal": signal,
        "journal_sha256": journal.sha256,
        "runtime_sha256": hashlib.sha256(
            journal.runtime_text.encode("utf-8")
        ).hexdigest(),
        "on_flow_sha256": hashlib.sha256(projection.encode("utf-8")).hexdigest(),
        "off_events": off_events,
        "closed_resources": {"off": off_resources, "on": on_resources},
        "counts": {
            "records": len(records),
            "app_runtimes": 1,
            "hub_probe_lifecycles": 2,
            "off_events": 3,
            "closed_zero_snapshots": 2,
        },
        "explicit_limits": {
            "identity": "Cooperative native PID/boot and JS nonce records, not independent process provenance or attestation.",
            "control": "Native ready/URL acceptance and UI request do not prove Settings ON, OS control or physical actions.",
            "rf": "Reuses the rf-io transcript checker; source records, including synthetic records, are not live BLE/RF proof or a bundle verdict.",
            "resources": "Two exact 17-zero logical counter snapshots, not OS memory, platform internals or independently observed resource retirement.",
            "timing": "Forward record order only; no independent clock, silence duration, transport/liveness or persistent result-page proof.",
        },
    }
