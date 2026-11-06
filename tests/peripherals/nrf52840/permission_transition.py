"""Permission-loss coordination on the existing multifunction fixture/BTP owner.

Wire contract (all files immutable, schema_version=1 and run_id on every document):
* permission-control.json: {schema_version, run_id}.
* host permission-active-go.json: {schema_version, run_id, phase: "active"}.
* provider arm/revoke/restored readiness: common {schema_version, run_id, phase,
  receipt_sha256, native_bootstrap, route, hold: {sequence, handle}, peer}.
* host revoke-request: {schema_version, run_id, route, ready_sha256, request_nonce}.
* provider revoke-authorized adds request_sha256, ready_sha256, request_nonce.
* host loss-confirmed: {schema_version, run_id, authorized_sha256, request_nonce,
  pid, uid, user_id, reason: 8, exit_proof_sha256, revoked_elapsed_ms}.
request_nonce is a fresh canonical UUID. revoked_elapsed_ms is the host monotonic
elapsed time from validating authorization to starting CONNECT revoke, in [0,4000].
exit_proof_sha256 hashes the original bytes of the host's persisted controller OS
death-proof artifact; it is not the dump record fingerprint or a synthetic marker.
Hashes bind original file bytes, not reserialized JSON. Host validates the exact OS
exit record and its hash, including original native PID/UID/user, and calls CONNECT
revoke within 4000ms of receiving authorization. Provider cannot attest OS commands.

RuntimeFacts is the shared online/offline owner. consume accepts a raw marker (e.g.
"active:event:{...}"), a PREFIX-prefixed marker, or a raw capture line containing
exactly one PREFIX (e.g. logcat threadtime). It never strips/truncates JSON. Host
must order delayed capture by source time/process, not arrival time; an actual late
callback fails. No device I/O or host/CLI import occurs in the facts methods.

Each phase: native:bootstrap:JSON, runtime:runId=UUID:phase=PHASE:nonce=X:state=start.
Denied emits denied:event snapshot/init-terminal/scan-permission-terminal, then
denied:resources, denied:passed, runtime close, passed:phase=denied:runId=UUID.
Active emits active:flow:baseline:<ordinary rf-io marker>, active:baseline-resources (17
zeros), active:baseline-passed; then active:event setup with IDs 52000/2/3/4/6,
scan=52001, conn=52005, gatt=52007; active:discovery-ready:route JSON.
After arm-ready: native:arm:JSON (bootstrap + source=payload-url + url),
active:arm-signal:runId=UUID:source=linking,
active:reads-accepted:activeCommandId=52008:queuedCommandId=52009:characteristicId=X:
connectionId=52005:gattSessionId=52007:runId=UUID, and
active:pending-resources:JSON. No old-route events/terminals/close/zero are legal.
Restored emits restored:flow:restored:<ordinary rf-io marker>, restored:resources
(17 zeros), restored:passed, runtime close, passed:phase=restored:runId=UUID.
Discovery-ready uses characteristicId=X:connectionId=52005:gattSessionId=52007:
runId=UUID (not JSON). active:baseline:resources is the current JS spelling of
active:baseline-resources; both are accepted once, never both. native:signal is
the canonical ARM signal (native:arm is an explicit alias). No active pass/close.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .adapter_transition import ACTIVE_AUDIT_BYTES, FLOW_PREFIXES, ActiveReadFacts
from .cancellation_control import read_document, require_multifunction
from .fault_control import (
    FaultController,
    Outcome,
    Phase,
    Winner,
    cleanup_ready,
    status_document,
    ticket_document,
)
from .fixture_session import (
    FixtureSession,
    _json,
    _publish_receipt,
    _read_bytes,
    load_session,
    read_live_snapshot,
    verify_receipt,
)
from .gatt_commands import tester_characteristic_native_permissions
from .recovery_ten import POLL_SECONDS, RESOURCE_FIELDS, RecoveryTen

MODE = "permission-transition"
CONTROL_NAME = "permission-control.json"
PHYSICAL_NAME = "permission-transition-physical.json"
RUNTIME_NAME = "permission-transition-runtime.log"
PREFIX = "BLEHUB_NITRO_RUNTIME rf-permission-transition:"
ACTIVE_GO_NAME = "permission-active-go.json"
ARM_READY_NAME = "permission-arm-ready.json"
REVOKE_READY_NAME = "permission-revoke-ready.json"
REVOKE_REQUEST_NAME = "permission-revoke-request.json"
REVOKE_AUTHORIZED_NAME = "permission-revoke-authorized.json"
LOSS_CONFIRMED_NAME = "permission-loss-confirmed.json"
RESTORED_READY_NAME = "permission-restored-ready.json"
PHASES = ("denied", "active", "restored")
IDENTITY_FIELDS = {
    "run_id",
    "phase",
    "pid",
    "uid",
    "user_id",
    "boot_id",
    "bundle_id",
    "executable",
    "source",
    "started_unix_ms",
    "started_elapsed_ms",
}
PHASE_SECONDS = 90.0
WHOLE_SECONDS = 600.0
REVOKE_SECONDS = 4.0
LOSS_SECONDS = 8.0
MAX_MARKERS = 512
MAX_LINE_BYTES = 65536


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _uuid(value: Any) -> bool:
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _positive(value: Any) -> bool:
    return (
        type(value) is str
        and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None
        and int(value) < 1 << 64
    )


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def arm_url(run_id: str) -> str:
    return f"blehub-rn-spike://permission/{run_id}/active/arm"


def _resources(value: dict[str, Any], *, pending: bool = False) -> None:
    _require(
        set(value) == RESOURCE_FIELDS
        and all(type(v) is int and 0 <= v < 1 << 53 for v in value.values()),
        "resources require exactly 17 integer counters",
    )
    if not pending:
        _require(all(v == 0 for v in value.values()), "alive Probe close is not zero")
        return
    expected = dict.fromkeys(RESOURCE_FIELDS, 0)
    expected.update(
        dict.fromkeys(
            (
                "connections",
                "watchdogs",
                "ingressRoots",
                "adapterDelegates",
                "backendLeases",
                "hubRoots",
                "handoffRoots",
                "listeners",
                "lifecycleOwners",
                "peerCacheEntries",
            ),
            1,
        )
    )
    expected.update(pendingIo=2, workerThreads=2)
    _require(
        value["gattAttributes"] > 0 and value["callbackRoutes"] > 0,
        "pending resources lack native GATT routes",
    )
    expected.update(
        gattAttributes=value["gattAttributes"], callbackRoutes=value["callbackRoutes"]
    )
    _require(
        value == expected,
        "pending resources are not two reads/one watchdog/one connection",
    )


class RuntimeFacts:
    """Bounded, fail-sticky facts. Physical authorizations are explicit methods.

    Offline consumers replay authorize_active(), authorize_arm(), mark_waiting(),
    confirm_loss(), authorize_restored() from verified provider/OS documents at the
    corresponding transcript boundaries; markers alone never supply physical proof.
    These methods have no clocks or I/O. refresh is optional cumulative-file input.
    """

    def __init__(
        self, run_id: str, service_uuid: str, expected_local_name: str | None = None
    ) -> None:
        _require(
            _uuid(run_id) and _uuid(service_uuid), "canonical run/service UUID required"
        )
        self.run_id, self.service = run_id, service_uuid
        self.expected_local_name = expected_local_name
        self.bootstraps: dict[str, dict[str, Any]] = {}
        self.nonces: dict[str, str] = {}
        self.phase: str | None = None
        self.started = False
        self.closed: set[str] = set()
        self.passes: set[str] = set()
        self.resources: dict[str, dict[str, int]] = {}
        self.flow: list[str] = []
        self.flow_done = False
        self.baseline_passed = False
        self.denied_events = 0
        self.setup_facts = ActiveReadFacts(
            run_id, service_uuid, expected_local_name or ""
        )
        self.route: dict[str, str] | None = None
        self.peer_id: str | None = None
        self.setup_step = 0
        self.flow_peer: str | None = None
        self.flow_characteristic: str | None = None
        self.arm_authorized = False
        self.arm_signal = False
        self.js_arm = False
        self.accepted: list[str] = []
        self.reads_accepted = False
        self.pending = False
        self.waiting = False
        self.loss_confirmed = False
        self.active_authorized = False
        self.restored_authorized = False
        self.count = 0
        self.failed: str | None = None
        self.lines: list[str] = []

    @property
    def passed(self) -> bool:
        return (
            self.failed is None
            and self.passes == {"denied", "restored"}
            and self.loss_confirmed
        )

    def _check(self, condition: bool, message: str) -> None:
        _require(self.failed is None, f"runtime facts already failed: {self.failed}")
        if not condition:
            self.failed = message
            raise RuntimeError(message)

    def authorize_active(self) -> None:
        self._check(
            self.passes == {"denied"} and not self.active_authorized,
            "early/duplicate active authorization",
        )
        self.active_authorized = True

    def authorize_arm(self) -> None:
        self._check(
            self.route is not None and not self.arm_authorized,
            "early/duplicate ARM authorization",
        )
        self.arm_authorized = True

    def mark_waiting(self) -> None:
        self._check(
            self.pending and not self.waiting and not self.loss_confirmed,
            "WAITING before pending reads or duplicate",
        )
        self.waiting = True

    def confirm_loss(self) -> None:
        self._check(
            self.waiting and not self.loss_confirmed,
            "death before WAITING or duplicate",
        )
        self.loss_confirmed = True

    def authorize_restored(self) -> None:
        self._check(
            self.loss_confirmed and not self.restored_authorized,
            "early/duplicate restored authorization",
        )
        self.restored_authorized = True

    def consume(self, line: str) -> None:
        """Consume one complete marker/capture line; failure permanently poisons facts."""
        _require(self.failed is None, f"runtime facts already failed: {self.failed}")
        try:
            _require(
                type(line) is str
                and 0 < len(line.encode()) <= MAX_LINE_BYTES
                and "\n" not in line
                and "\r" not in line,
                "invalid/oversized runtime line",
            )
            _require(line.count(PREFIX) <= 1, "ambiguous runtime prefix")
            preamble, separator, marker = line.partition(PREFIX)
            if not separator:
                marker, preamble = line, ""
            _require(marker == marker.strip() and marker, "non-normalized marker")
            _require("BLEHUB_NITRO_RUNTIME" not in preamble, "foreign runtime prefix")
            self.count += 1
            _require(self.count <= MAX_MARKERS, "runtime facts overflow")
            self._consume(marker)
            # Threadtime/epoch capture PID, when supplied, is independent evidence.
            pid = re.fullmatch(
                r"(?:\d\d-\d\d\s+\d\d:\d\d:\d\d\.\d+|\d{10}\.\d+)\s+(\d+)\s+\d+\s+[VDIWEF]\s+[^:]+:\s*",
                preamble,
            )
            if pid:
                _require(
                    self.phase is not None
                    and int(pid[1]) == self.bootstraps[self.phase]["pid"],
                    "capture PID differs from native bootstrap",
                )
        except (
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
        ) as error:
            self.failed = str(error)
            raise RuntimeError(f"permission runtime: {error}") from error

    def refresh(self, path: Path) -> None:
        _require(self.failed is None, f"runtime facts already failed: {self.failed}")
        try:
            self._refresh(path)
        except (OSError, ValueError, RuntimeError) as error:
            self.failed = str(error)
            raise

    def _refresh(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            _require(not self.lines, "runtime log disappeared")
            return
        text = read_live_snapshot(path).decode("utf-8")
        _require(not text or text.endswith("\n"), "runtime log has partial line")
        lines = text.splitlines()
        _require(
            len(lines) <= MAX_MARKERS and lines[: len(self.lines)] == self.lines,
            "runtime log rewound/changed/overflowed",
        )
        for line in lines[len(self.lines) :]:
            self.consume(line)
        self.lines = lines

    def _bootstrap(self, value: dict[str, Any]) -> None:
        phase = PHASES[len(self.bootstraps)] if len(self.bootstraps) < 3 else None
        _require(
            set(value) == IDENTITY_FIELDS
            and value.get("phase") == phase
            and value.get("run_id") == self.run_id
            and value.get("source") == "launch"
            and value.get("bundle_id") == "com.blehubnitrohost"
            and _uuid(value.get("boot_id")),
            "native bootstrap schema/phase/identity",
        )
        _require(
            all(
                type(value[k]) is int and 0 < value[k] < 1 << 53
                for k in ("pid", "started_unix_ms", "started_elapsed_ms")
            )
            and type(value["uid"]) is int
            and 10000 <= value["uid"] < 100000
            and type(value["user_id"]) is int
            and value["user_id"] == 0
            and type(value["executable"]) is str
            and 0 < len(value["executable"]) <= 2048,
            "invalid native process fields",
        )
        if phase != "denied":
            _require(
                self.active_authorized
                if phase == "active"
                else self.restored_authorized,
                "App phase started before physical authorization",
            )
            previous = list(self.bootstraps.values())[-1]
            _require(
                all(
                    value[k] == previous[k]
                    for k in ("uid", "user_id", "bundle_id", "executable")
                )
                and all(
                    value["pid"] != b["pid"] and value["boot_id"] != b["boot_id"]
                    for b in self.bootstraps.values()
                )
                and all(
                    value[k] > previous[k]
                    for k in ("started_unix_ms", "started_elapsed_ms")
                ),
                "App phases must be independent ordered native processes",
            )
        self.bootstraps[phase] = value
        self.phase, self.started, self.flow_done = phase, False, False
        self.flow = []
        self.flow_peer = self.flow_characteristic = None

    def _consume(self, marker: str) -> None:
        _require(
            not re.search(r"(?:^|:)(failed|rejected)(?::|$)", marker.split("{", 1)[0]),
            "runtime failure marker",
        )
        if marker.startswith("native:bootstrap:"):
            self._bootstrap(_json(marker.removeprefix("native:bootstrap:").encode()))
            return
        _require(self.phase is not None, "marker before native bootstrap")
        phase = self.phase
        runtime = re.fullmatch(
            r"runtime:runId=([^:]+):phase=(denied|active|restored):nonce=([^:\s]{1,128}):state=(start|close)",
            marker,
        )
        if runtime:
            run, incoming, nonce, state = runtime.groups()
            _require(
                run == self.run_id and incoming == phase, "foreign runtime generation"
            )
            if state == "start":
                _require(
                    not self.started
                    and phase not in self.nonces
                    and nonce not in self.nonces.values(),
                    "duplicate/reused runtime nonce",
                )
                self.nonces[phase], self.started = nonce, True
            else:
                _require(
                    phase != "active"
                    and phase in self.closed
                    and phase not in self.passes
                    and nonce == self.nonces.get(phase)
                    and self.started,
                    "early/duplicate/dead runtime close",
                )
                self.started = False
            return
        if marker == f"passed:phase={phase}:runId={self.run_id}":
            _require(
                phase in self.closed and not self.started and phase not in self.passes,
                "early/duplicate global pass",
            )
            self.passes.add(phase)
            return
        _require(
            self.started and phase not in self.closed, "late event after phase close"
        )
        if marker.startswith("denied:event:"):
            _require(
                len((PREFIX + marker).encode()) <= ACTIVE_AUDIT_BYTES,
                "denied audit exceeds UTF-8 budget",
            )
            _require(
                phase == "denied" and "denied" not in self.resources,
                "late denied callback",
            )
            self._denied(_json(marker.removeprefix("denied:event:").encode()))
            return
        if marker.startswith(("active:flow:", "restored:flow:")):
            label = "baseline" if phase == "active" else "restored"
            prefix = f"{phase}:flow:{label}:"
            _require(
                marker.startswith(prefix)
                and phase in ("active", "restored")
                and not self.flow_done
                and (phase != "active" or not self.baseline_passed),
                "foreign/late flow label",
            )
            self._flow(marker[len(prefix) :])
            return
        resource_names = {
            "denied:resources:": "denied",
            "restored:resources:": "restored",
            "active:baseline-resources:": "baseline",
            "active:baseline:resources:": "baseline",
        }
        for prefix, key in resource_names.items():
            if marker.startswith(prefix):
                _require(
                    key not in self.resources
                    and (
                        (
                            key == "denied"
                            and phase == "denied"
                            and self.denied_events == 3
                        )
                        or (key == "baseline" and phase == "active" and self.flow_done)
                        or (
                            key == "restored" and phase == "restored" and self.flow_done
                        )
                    ),
                    "early/duplicate/foreign resources",
                )
                value = _json(marker[len(prefix) :].encode())
                _resources(value)
                self.resources[key] = value
                return
        if marker == "active:baseline-passed":
            _require(
                phase == "active"
                and "baseline" in self.resources
                and not self.baseline_passed,
                "baseline pass before zero/duplicate",
            )
            self.baseline_passed = True
            return
        if marker == f"{phase}:passed" and phase in ("denied", "restored"):
            _require(phase in self.resources, "phase pass before close-zero")
            self.closed.add(phase)
            return
        _require(
            phase == "active" and self.baseline_passed and not self.loss_confirmed,
            "unknown/late/dead phase marker",
        )
        self._active(marker)

    @staticmethod
    def _event_fields(
        event: dict[str, Any], expected: dict[str, Any], *, error: bool = False
    ) -> None:
        known = {
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
            "advertisedServices",
            "localName",
            "rssi",
            "adapterState",
            "scanAuthorization",
            "error",
            "value",
            "actualMode",
            "auditSchema",
            "gattSummary",
        }
        _require(set(event) <= known, "unknown raw event field")
        _require(
            all(event.get(k) == v for k, v in expected.items())
            and all(
                v is None
                for k, v in event.items()
                if k not in expected and (k != "error" or not error)
            ),
            "event route/foreign field/error drift",
        )

    def _denied(self, event: dict[str, Any]) -> None:
        _require(self.denied_events < 3, "duplicate denied event")
        expected = (
            {
                "kind": "adapterSnapshotChanged",
                "commandId": "51000",
                "scanAuthorization": "Denied",
                "adapterState": event.get("adapterState"),
            },
            {"kind": "commandCompleted", "commandId": "51000"},
            {"kind": "commandCompleted", "commandId": "51002"},
        )[self.denied_events]
        if self.denied_events == 0:
            _require(
                event.get("adapterState")
                in (
                    "Unknown",
                    "Unavailable",
                    "PoweredOff",
                    "PoweredOn",
                ),
                "invalid actual denied snapshot",
            )
        self._event_fields(event, expected, error=self.denied_events == 2)
        if self.denied_events == 2:
            error = event.get("error")
            _require(
                type(error) is dict
                and error.get("category") == "permission"
                and type(error.get("message")) is str
                and set(error) <= {"category", "message", "platformDetails"},
                "denied scan requires original Permission error",
            )
            details = error.get("platformDetails")
            _require(
                details is None
                or type(details) is dict
                and details.get("platform") == "platform_android"
                and set(details) <= {"platform", "name", "code", "message"}
                and all(v is None or type(v) is str for v in details.values()),
                "invalid denied Android error",
            )
        self.denied_events += 1

    def _flow(self, body: str) -> None:
        """Exact ordinary rf-io grammar; no alternate runner or host CLI dependency."""
        _require(len(self.flow) < len(FLOW_PREFIXES), "flow overflow")
        prefix = FLOW_PREFIXES[len(self.flow)]
        _require(body.startswith(prefix), "out-of-order/duplicate rf-io marker")
        if not prefix.endswith(":"):
            _require(body == prefix, "foreign flow suffix")
        else:
            fields: dict[str, str] = {}
            for item in body[len(prefix) :].split(":"):
                key, equals, value = item.partition("=")
                _require(
                    bool(equals and value) and key not in fields,
                    "invalid/duplicate flow field",
                )
                fields[key] = value
            peer = self.flow_peer
            common = {
                "connectionId": "9007",
                "peripheralId": peer,
                "serviceUuid": self.service,
            }
            if prefix == "rf:start:":
                name = fields.get("expectedLocalName")
                _require(
                    name is not None
                    and (
                        self.expected_local_name is None
                        or name == self.expected_local_name
                    ),
                    "foreign flow local name",
                )
                expected = {
                    "serviceUuid": self.service,
                    "expectedLocalName": name,
                    "initCommandId": "9001",
                    "startCommandId": "9002",
                    "stopCommandId": "9003",
                    "scanSessionId": "9004",
                }
            elif prefix == "rf:step:initialize:passed:":
                expected = {
                    "commandId": "9001",
                    "adapterState": "PoweredOn",
                    "scanAuthorization": "Authorized",
                }
            elif prefix == "rf:step:scan-start:passed:":
                expected = {"startCommandId": "9002", "scanSessionId": "9004"}
            elif prefix == "rf:step:advertisement:passed:":
                peer, name = fields.get("peripheralId"), fields.get("localName")
                payload = fields.get("payloadAdvertisedServices", "")
                _require(
                    _positive(peer) and name in (self.expected_local_name, "missing")
                    if self.expected_local_name is not None
                    else _positive(peer) and bool(name),
                    "foreign flow advertisement",
                )
                _require(
                    payload == "none" or all(_uuid(s) for s in payload.split(",")),
                    "invalid advertised service payload",
                )
                expected = {
                    "scanSessionId": "9004",
                    "peripheralId": peer,
                    "localName": name,
                    "filterServiceUuid": self.service,
                    "payloadAdvertisedServices": payload,
                    "payloadContainsFilterUuid": str(
                        self.service in payload.split(",")
                    ).lower(),
                }
                self.flow_peer = peer
            elif prefix == "rf:step:scan-stop:passed:":
                expected = {
                    "stopCommandId": "9003",
                    "scanSessionId": "9004",
                    "requested": "true",
                }
            elif prefix == "rf:step:silence:passed:":
                expected = {"scanSessionId": "9004", "observationMs": "2000"}
            elif prefix in (
                "rf:step:connecting:passed:",
                "rf:step:connected:passed:",
                "rf:step:disconnected:passed:",
            ):
                expected = dict(
                    common, commandId="9006" if "disconnected" in prefix else "9005"
                )
            elif prefix == "rf:step:gatt-discovery:passed:":
                count = fields.get("discoveredServiceCount")
                _require(_positive(count), "invalid discovered service count")
                expected = dict(
                    common,
                    commandId="9008",
                    gattSessionId="9009",
                    requestedGattSessionId="9009",
                    serviceCount="1",
                    discoveredServiceCount=count,
                    characteristicCount="5",
                    descriptorCount="2",
                )
            elif prefix in tuple(
                f"rf:step:{name}:passed:"
                for name in (
                    "read-initial",
                    "write-response",
                    "read-response",
                    "write-without-response",
                    "read-without-response",
                )
            ):
                name = prefix.split(":")[2]
                command, value = {
                    "read-initial": ("9010", "00"),
                    "write-response": ("9011", "10"),
                    "read-response": ("9012", "10"),
                    "write-without-response": ("9013", "ab"),
                    "read-without-response": ("9014", "ab"),
                }[name]
                characteristic = fields.get("characteristicId")
                _require(
                    type(characteristic) is str
                    and 0 < len(characteristic) <= 128
                    and (
                        self.flow_characteristic is None
                        or characteristic == self.flow_characteristic
                    ),
                    "rf-io characteristic route drift",
                )
                self.flow_characteristic = characteristic
                expected = dict(
                    common,
                    commandId=command,
                    gattSessionId="9009",
                    characteristicId=characteristic,
                    value=value,
                )
                if name == "write-without-response":
                    expected["terminal"] = "local-submission"
            elif prefix == "rf:step:connection-silence:passed:":
                expected = dict(common, observationMs="1000")
            elif prefix == "rf:step:close:passed:":
                expected = {"probe-closed": "true", "listener-removed": "true"}
            elif prefix == "rf-io:passed:":
                expected = dict(
                    common,
                    connectCommandId="9005",
                    gattCommandId="9008",
                    readCommandId="9010",
                    writeResponseCommandId="9011",
                    writeWithoutResponseCommandId="9013",
                    disconnectCommandId="9006",
                    scanSessionId="9004",
                )
            else:
                raise RuntimeError("unknown rf-io grammar")
            _require(fields == expected, "rf-io route/value/field drift")
        self.flow.append(body)
        self.flow_done = len(self.flow) == len(FLOW_PREFIXES)

    def _setup_event(self, event: dict[str, Any]) -> None:
        # ActiveReadFacts is only a setup/topology fact store. Its adapter-loss
        # event()/observe() terminal/disconnect policy is deliberately never called.
        facts = self.setup_facts
        _require(
            self.route is None and len(facts.events) < 128, "late/overflow setup event"
        )
        command, kind = event.get("commandId"), event.get("kind")
        if kind == "advertisementObserved":
            _require(
                self.setup_step == 4
                and command is None
                and _positive(event.get("peripheralId")),
                "advertisement outside live scan",
            )
            peer, services = event["peripheralId"], event.get("advertisedServices")
            _require(
                type(services) is list
                and len(services) <= 128
                and all(_uuid(v) for v in services)
                and self.service in services
                and (self.peer_id is None or peer == self.peer_id)
                and (
                    self.expected_local_name is None
                    or event.get("localName") == self.expected_local_name
                )
                and type(event.get("rssi")) is int
                and -32768 <= event["rssi"] <= 32767,
                "foreign active advertisement",
            )
            expected = {
                "kind": kind,
                "scanSessionId": "52001",
                "peripheralId": peer,
                "advertisedServices": services,
                "localName": event.get("localName"),
                "rssi": event["rssi"],
            }
            self.peer_id = peer
        else:
            sequence = (
                ("52000", "adapterSnapshotChanged"),
                ("52000", "commandCompleted"),
                ("52002", "scanStarted"),
                ("52002", "commandCompleted"),
                ("52003", "scanStopped"),
                ("52003", "commandCompleted"),
                ("52004", "Connecting"),
                ("52004", "Connected"),
                ("52004", "commandCompleted"),
                ("52006", "gattDiscovered"),
                ("52006", "commandCompleted"),
            )
            label = (
                event.get("connectionState")
                if kind == "connectionStateChanged"
                else kind
            )
            _require(
                self.setup_step < len(sequence)
                and (command, label) == sequence[self.setup_step],
                "setup domain/terminal order drift",
            )
            expected = {"kind": kind, "commandId": command}
            if kind == "adapterSnapshotChanged":
                expected.update(
                    adapterState="PoweredOn", scanAuthorization="Authorized"
                )
            elif kind in ("scanStarted", "scanStopped"):
                _require(
                    kind != "scanStopped" or self.peer_id is not None,
                    "stop before exact fixture advertisement",
                )
                expected["scanSessionId"] = "52001"
            elif kind == "connectionStateChanged":
                expected.update(connectionId="52005", connectionState=label)
            elif kind == "gattDiscovered":
                database = event.get("gattServices")
                _require(type(database) is list, "missing native GATT database")
                if "auditSchema" in event or "gattSummary" in event:
                    facts.validate_compact_gatt(event)
                    expected.update(
                        auditSchema=event.get("auditSchema"),
                        gattSummary=event.get("gattSummary"),
                    )
                else:
                    # Same topology reader, with truthful counts for the full
                    # retained canonical database. Extra native services may exist.
                    canonical = [
                        s
                        for s in database
                        if type(s) is dict and s.get("uuid") == self.service
                    ]
                    facts.validate_compact_gatt(
                        dict(
                            event,
                            auditSchema="active-gatt-canonical-v1",
                            gattServices=canonical,
                            gattSummary={
                                "serviceCount": len(database),
                                "characteristicCount": sum(
                                    len(s.get("characteristics", [])) for s in database
                                ),
                                "descriptorCount": sum(
                                    len(c.get("descriptors", []))
                                    for s in database
                                    for c in s.get("characteristics", [])
                                ),
                            },
                        )
                    )
                expected.update(
                    connectionId="52005", gattSessionId="52007", gattServices=database
                )
            self.setup_step += 1
            if kind == "commandCompleted":
                facts.terminals[command] = None
            else:
                facts.domain(command, label)
        self._event_fields(event, expected)
        facts.events.append(event)

    def _active(self, marker: str) -> None:
        if marker.startswith("active:event:"):
            _require(
                len((PREFIX + marker).encode()) <= ACTIVE_AUDIT_BYTES,
                "active audit exceeds UTF-8 budget",
            )
            self._setup_event(_json(marker.removeprefix("active:event:").encode()))
        elif marker.startswith("active:discovery-ready:"):
            match = re.fullmatch(
                r"active:discovery-ready:characteristicId=([1-9][0-9]*):connectionId=52005:gattSessionId=52007:runId="
                + re.escape(self.run_id),
                marker,
            )
            _require(
                match is not None
                and _positive(match[1])
                and self.setup_step == 11
                and self.route is None,
                "early/foreign discovery route",
            )
            database = next(
                e["gattServices"]
                for e in self.setup_facts.events
                if e["kind"] == "gattDiscovered"
            )
            targets = [
                c
                for s in database
                if s.get("uuid") == self.service
                for c in s["characteristics"]
                if c["id"] == match[1]
                and c["uuid"] == "fff10000-0000-4000-8000-000000000000"
                and c["readable"] is True
                and c["writableWithResponse"] is True
                and c["writableWithoutResponse"] is True
            ]
            _require(len(targets) == 1, "discovery target is not ordinary canonical RW")
            self.route = {
                "connectionId": "52005",
                "gattSessionId": "52007",
                "characteristicId": match[1],
            }
        elif marker.startswith(("native:signal:", "native:arm:")):
            value = _json(marker.split(":", 2)[2].encode())
            _require(
                self.arm_authorized
                and not self.arm_signal
                and value
                == {
                    **self.bootstraps["active"],
                    "source": "payload-url",
                    "url": arm_url(self.run_id),
                },
                "foreign/early/duplicate native ARM signal",
            )
            self.arm_signal = True
        elif marker == f"active:arm-signal:runId={self.run_id}:source=linking":
            _require(
                self.arm_signal and not self.js_arm,
                "JS ARM before native signal/duplicate",
            )
            self.js_arm = True
        elif marker.startswith("active:reads-accepted:"):
            _require(
                self.route is not None and self.js_arm and not self.reads_accepted,
                "early/duplicate reads acceptance",
            )
            route = f"characteristicId={self.route['characteristicId']}:connectionId=52005:gattSessionId=52007:runId={self.run_id}"
            _require(
                marker
                == "active:reads-accepted:activeCommandId=52008:queuedCommandId=52009:"
                + route,
                "foreign accepted read IDs/route/run",
            )
            self.accepted, self.reads_accepted = ["52008", "52009"], True
        elif marker.startswith("active:pending-resources:"):
            _require(
                self.reads_accepted and not self.pending,
                "early/duplicate pending resources",
            )
            value = _json(marker.removeprefix("active:pending-resources:").encode())
            _resources(value, pending=True)
            self.resources["pending"], self.pending = value, True
        else:
            raise RuntimeError(f"unknown/duplicate active marker: {marker}")


HANDSHAKE_NAMES = (
    ACTIVE_GO_NAME,
    ARM_READY_NAME,
    REVOKE_READY_NAME,
    REVOKE_REQUEST_NAME,
    REVOKE_AUTHORIZED_NAME,
    LOSS_CONFIRMED_NAME,
    RESTORED_READY_NAME,
)


def validate_control(
    root: Path, plan: FixtureSession | None, *, fresh: bool = True
) -> FixtureSession:
    """Validate immutable multifunction declaration before acquiring/opening serial."""
    plan = require_multifunction(plan)
    _require(
        _uuid(plan.session_id) and not root.is_symlink() and root.is_dir(),
        "private control directory/run UUID required",
    )
    _require(
        plan.root.resolve() == (root / "fixture-session").resolve()
        and load_session(plan.request_path) == plan,
        "fixture session path/content changed",
    )
    _require(
        {p.name for p in root.glob("*-control.json")} == {CONTROL_NAME},
        "exactly one permission control required",
    )
    value = read_document(root / CONTROL_NAME)
    _require(
        set(value) == {"schema_version", "run_id"}
        and type(value["schema_version"]) is int
        and value == {"schema_version": 1, "run_id": plan.session_id},
        "invalid permission control",
    )
    if fresh:
        for name in (*HANDSHAKE_NAMES, PHYSICAL_NAME, RUNTIME_NAME, "abort"):
            path = root / name
            _require(
                not path.exists() and not path.is_symlink(),
                f"stale permission output: {name}",
            )
    return plan


class PermissionTransition(RecoveryTen):
    """Reuse exact GAP tracker/owned-peer cleanup and one typed v2 fault owner.

    The success path never calls GAP disconnect. Only failed cleanup may abort an
    owned hold/disconnect an owned peer; cleanup cannot convert failure to success.
    """

    def __init__(self, fixture: Any, root: Path) -> None:
        self.fixture, self.root = fixture, root
        self.plan = validate_control(root, fixture._fixture_session)
        self.process = (os.getpid(), os.getpgid(0))
        self.control_bytes = _read_bytes(root / CONTROL_NAME)
        self.receipt_bytes: bytes | None = None
        self.documents: dict[str, bytes] = {}
        self.fault = FaultController(fixture._session, self.plan.session_id)
        self.owned_hold = None
        self.held_peer = None
        self.first_waiting: float | None = None
        self.authorized_at: float | None = None
        self.loss_confirmed = False
        self.restored = False
        self.baseline_closed = False
        self.facts = RuntimeFacts(
            self.plan.session_id, self.plan.service_uuid, fixture._profile.local_name
        )
        self.peer = None
        self.allow_connection = self.allow_disconnect = False
        self.link_events: list[dict[str, Any]] = []
        self.rejected_events: list[dict[str, Any]] = []
        self.cycle, self.record = -1, None
        self.phase = "denied"
        self.whole_deadline = time.monotonic() + WHOLE_SECONDS
        self.evidence: dict[str, Any] = {
            "schema_version": 1,
            "mode": MODE,
            "run_id": self.plan.session_id,
            "session_id": self.plan.session_id,
            "status": "failed",
            "fixture_pid": self.process[0],
            "fixture_pgid": self.process[1],
            "contract_sha256": self.plan.contract_sha256,
            "profile_sha256": self.plan.profile_sha256,
            "resource_identity": self.plan.resource_identity,
            "events": self.link_events,
            "boundaries": [],
            "protocol": {},
            "cleanup_errors": [],
            "primary_error": None,
            "hold_status": {},
            "cleanup": {
                "advertising_stopped": False,
                "hold_inactive": False,
                "peer_count": None,
            },
        }

    def authority(self) -> None:
        self.fixture._transport.require()
        _require(
            (os.getpid(), os.getpgid(0)) == self.process, "provider identity changed"
        )
        validate_control(self.root, self.plan, fresh=False)
        _require(
            _read_bytes(self.root / CONTROL_NAME) == self.control_bytes,
            "immutable permission control changed",
        )
        receipt = _read_bytes(self.plan.receipt_path)
        _require(
            self.receipt_bytes is None or receipt == self.receipt_bytes,
            "live receipt mutated",
        )
        _require(
            _json(receipt) == verify_receipt(self.plan, fixture_identity=self.process),
            "live receipt mismatch",
        )
        self.receipt_bytes = receipt
        self.evidence["receipt_sha256"] = _hash(receipt)
        for name, raw in self.documents.items():
            _require(
                _read_bytes(self.root / name) == raw,
                f"immutable permission handshake changed: {name}",
            )

    def guard(self, deadline: float) -> None:
        self.authority()
        _require(
            not self.fixture._stop_requested and not (self.root / "abort").exists(),
            "permission transition aborted",
        )
        if time.monotonic() >= min(deadline, self.whole_deadline):
            raise TimeoutError(f"permission transition deadline: {self.phase}")

    def observe_gap(self, event: Any) -> None:
        try:
            super().observe_gap(event)
            _require(
                len(self.link_events) <= 6,
                "permission transition permits exactly three GAP pairs",
            )
        except RuntimeError:
            self.rejected_events.append(
                {
                    "service": event.service,
                    "opcode": event.opcode,
                    "index": event.index,
                    "payload_hex": event.payload.hex(),
                }
            )
            raise

    def drain(self) -> None:
        end = time.monotonic() + 5
        while (event := self.fixture._session.pop_event()) is not None:
            _require(time.monotonic() < end, "GAP drain overflow")
            self.fixture._transport.require()
            self.observe_gap(event)
        _require(
            self.fixture._connected_peers == ({self.peer} if self.peer else set()),
            "exact GAP tracker drift",
        )

    def _status(self) -> Any:
        if self.owned_hold is None:
            limits = self.fault.require_idle()
            self.evidence["hold_status"] = {
                "protocol_version": 2,
                "engine_flags": int(limits.engine_flags),
            }
            return None
        snapshot = self.fault.status(self.owned_hold)
        self.evidence["hold_status"] = status_document(snapshot)
        return snapshot

    def prepare(self) -> None:
        self.authority()
        self._status()
        self.no_peer()

    def _remember(self, name: str, raw: bytes) -> dict[str, Any]:
        value = _json(raw)
        self.documents[name] = raw
        self.evidence["protocol"][name] = value
        return value

    def publish(self, name: str, phase: str, **extra: Any) -> None:
        self.authority()
        value = {
            "schema_version": 1,
            "run_id": self.plan.session_id,
            "phase": phase,
            "receipt_sha256": self.evidence["receipt_sha256"],
            "native_bootstrap": self.facts.bootstraps.get("active"),
            "route": self.facts.route,
            "hold": ticket_document(self.owned_hold) if self.owned_hold else None,
            "fault_status": self.evidence["hold_status"],
            "peer": list(self.held_peer) if self.held_peer else None,
            **extra,
        }
        _publish_receipt(self.root / name, value)
        _require(
            self._remember(name, _read_bytes(self.root / name)) == value,
            "publication changed",
        )

    def _host_document(self, name: str) -> tuple[bytes, dict[str, Any]] | None:
        path = self.root / name
        if name in self.documents or not path.exists() and not path.is_symlink():
            return None
        raw = _read_bytes(path)
        return raw, _json(raw)

    def _exact_peer(self) -> None:
        _require(
            self.held_peer is not None
            and self.peer == self.held_peer
            and self.fixture._connected_peers == {self.held_peer},
            "held read lost exact peer",
        )

    def _restore_baseline_values(self) -> None:
        """Restore the profile pre-state before the permission ARM barrier."""
        self.authority()
        self.no_peer()
        for characteristic in self.fixture._profile.characteristics:
            self.authority()
            self.no_peer()
            self.fixture._gatt.set_value(
                self.evidence["handles"][characteristic.role],
                characteristic.initial_value,
            )
        self.authority()
        self.no_peer()

    def _arm(self, deadline: float) -> None:
        _require(
            len(self.link_events) == 3 and self.peer is not None,
            "ARM requires second physical connection",
        )
        self.held_peer = self.peer
        self._exact_peer()
        rw = next(
            c for c in self.fixture._profile.characteristics if c.role == "read-write"
        )
        _require(
            rw.permissions == 0x03
            and rw.initial_value == b"\x00"
            and rw.uuid == "fff10000-0000-4000-8000-000000000000",
            "target must be ordinary read/write, not authorized/readonly",
        )
        attrs = self.fixture._gatt.get_attributes(attribute_uuid=rw.uuid)
        self.guard(deadline)
        self.drain()
        self._exact_peer()
        target = self.evidence["handles"]["read-write"]
        _require(
            len(attrs) == 1
            and attrs[0].handle == target
            and attrs[0].uuid == rw.uuid
            and attrs[0].permissions == tester_characteristic_native_permissions(0x03),
            "native RW handle/permissions drift",
        )
        before = self.fixture._gatt.get_attribute_value(target)
        self.guard(deadline)
        self.drain()
        self._exact_peer()
        _require(
            before.att_response == 0 and before.value == rw.initial_value,
            "permission before value drift",
        )
        peer_wire = bytes([self.peer[1]]) + bytes.fromhex(self.peer[0])[::-1]
        self.owned_hold = self.fault.arm_read(target, before.value, peer_wire).ticket
        snapshot = self._status()
        self.guard(deadline)
        self.drain()
        self._exact_peer()
        _require(snapshot.phase is Phase.ARMED, "ARM did not reach exact ARMED")
        self.facts.authorize_arm()
        self.boundary(
            "armed",
            ticket=ticket_document(self.owned_hold),
            status=status_document(snapshot),
            peer=list(self.peer),
        )
        self.publish(ARM_READY_NAME, "armed")

    def _challenge(self, deadline: float) -> None:
        request = self._host_document(REVOKE_REQUEST_NAME)
        if request:
            raw, value = request
            expected = {
                "schema_version": 1,
                "run_id": self.plan.session_id,
                "route": self.facts.route,
                "ready_sha256": _hash(self.documents.get(REVOKE_READY_NAME, b"")),
                "request_nonce": value.get("request_nonce"),
            }
            _require(
                self.facts.waiting
                and type(value.get("schema_version")) is int
                and _uuid(value.get("request_nonce"))
                and value == expected,
                "early/foreign/stale revoke challenge",
            )
            self._remember(REVOKE_REQUEST_NAME, raw)
            # A challenge is never authorized using the previous WAITING sample.
            fresh = self._status()
            self.guard(deadline)
            self.drain()
            self._exact_peer()
            self.facts.refresh(self.root / RUNTIME_NAME)
            _require(
                fresh.phase is Phase.HELD
                and self.first_waiting is not None
                and time.monotonic() < self.first_waiting + 3,
                "challenge requires fresh STILLWAITING within 3s",
            )
            self.authorized_at = time.monotonic()
            self.allow_disconnect = (
                True  # OS may disconnect before its dump is collected.
            )
            self.boundary(
                "revoke-authorized",
                status=status_document(fresh),
                ticket=ticket_document(self.owned_hold),
            )
            self.publish(
                REVOKE_AUTHORIZED_NAME,
                "revoke-authorized",
                request_sha256=_hash(raw),
                ready_sha256=expected["ready_sha256"],
                request_nonce=value["request_nonce"],
            )
        confirmed = self._host_document(LOSS_CONFIRMED_NAME)
        if confirmed:
            raw, value = confirmed
            bootstrap = self.facts.bootstraps["active"]
            challenge = self.evidence["protocol"].get(REVOKE_REQUEST_NAME, {})
            fields = {
                "schema_version",
                "run_id",
                "authorized_sha256",
                "request_nonce",
                "pid",
                "uid",
                "user_id",
                "reason",
                "exit_proof_sha256",
                "revoked_elapsed_ms",
            }
            _require(
                self.authorized_at is not None
                and set(value) == fields
                and type(value["schema_version"]) is int
                and value["schema_version"] == 1
                and value["run_id"] == self.plan.session_id
                and value["authorized_sha256"]
                == _hash(self.documents[REVOKE_AUTHORIZED_NAME])
                and value["request_nonce"] == challenge["request_nonce"]
                and all(
                    type(value[k]) is int and value[k] == bootstrap[k]
                    for k in ("pid", "uid", "user_id")
                )
                and type(value["reason"]) is int
                and value["reason"] == 8
                and type(value["exit_proof_sha256"]) is str
                and re.fullmatch(r"[0-9a-f]{64}", value["exit_proof_sha256"])
                is not None
                and type(value["revoked_elapsed_ms"]) is int
                and 0 <= value["revoked_elapsed_ms"] <= int(REVOKE_SECONDS * 1000),
                "invalid permission death confirmation/identity/reason/proof/budget",
            )
            self._remember(LOSS_CONFIRMED_NAME, raw)
            self.facts.confirm_loss()
            self.boundary("permission-loss-confirmed", pid=bootstrap["pid"], reason=8)
            self.guard(deadline)
            self.loss_confirmed = True

    def _accept_go(self) -> None:
        go = self._host_document(ACTIVE_GO_NAME)
        if go and "denied" in self.facts.passes:
            raw, value = go
            _require(
                type(value.get("schema_version")) is int
                and value
                == {
                    "schema_version": 1,
                    "run_id": self.plan.session_id,
                    "phase": "active",
                },
                "foreign active go",
            )
            self.facts.authorize_active()
            self._remember(ACTIVE_GO_NAME, raw)
            _require(
                self.peer is None and not self.fixture._connected_peers,
                "denied phase retained peer",
            )
            self.allow_connection = self.allow_disconnect = True
            self.phase = "active-baseline"

    def _runtime(self) -> None:
        path = self.root / RUNTIME_NAME
        if not path.exists() and not path.is_symlink():
            _require(not self.facts.lines, "runtime log disappeared")
            return
        text = read_live_snapshot(path).decode("utf-8")
        _require(not text or text.endswith("\n"), "partial runtime line")
        lines = text.splitlines()
        _require(
            len(lines) <= MAX_MARKERS
            and lines[: len(self.facts.lines)] == self.facts.lines,
            "runtime log rewound/changed/overflowed",
        )
        for line in lines[len(self.facts.lines) :]:
            # Host can publish go and launch before the next provider poll. Apply
            # the file barrier between cumulative records, never retroactively.
            self._accept_go()
            self.facts.consume(line)
        self.facts.lines = lines
        self._accept_go()
        _require(
            self._host_document(ACTIVE_GO_NAME) is None,
            "active go preceded denied global pass",
        )

    def sample(self, deadline: float) -> None:
        self.guard(deadline)
        self._runtime()
        snapshot = self._status()
        self.drain()
        if self.facts.baseline_passed and not self.baseline_closed:
            _require(
                len(self.link_events) == 2 and self.peer is None,
                "baseline must have exactly one GAP pair",
            )
            self.baseline_closed = True
            self.allow_connection = True
            self.allow_disconnect = False
            self._restore_baseline_values()
            self.fixture._start_advertising()
            self.phase = "active-held"
        if self.facts.route is not None and self.owned_hold is None:
            self._arm(deadline)
            snapshot = self._status()
        if self.owned_hold is not None and not self.loss_confirmed:
            attached = (Phase.ARMED, Phase.HELD)
            allowed = (
                (*attached, Phase.RETIRING, Phase.RETIRED)
                if self.authorized_at is not None
                else attached
            )
            _require(
                snapshot.phase in allowed,
                "fault expired/quarantined before permission loss",
            )
            if snapshot.phase is Phase.HELD:
                if self.first_waiting is None:
                    self.first_waiting = time.monotonic()
                    self.boundary(
                        "waiting",
                        ticket=ticket_document(self.owned_hold),
                        status=status_document(snapshot),
                    )
                if not self.facts.waiting and self.facts.pending:
                    self._exact_peer()
                    self.facts.mark_waiting()
                    self.publish(REVOKE_READY_NAME, "waiting")
            if self.first_waiting is not None:
                _require(
                    time.monotonic() < self.first_waiting + LOSS_SECONDS,
                    "permission loss exceeded pre-watchdog 8s budget",
                )
        self._challenge(deadline)
        if self.loss_confirmed and not self.restored:
            _require(
                self.first_waiting is not None
                and time.monotonic() < self.first_waiting + LOSS_SECONDS,
                "permission retirement exceeded pre-watchdog budget",
            )
            snapshot = self._status()
            self.drain()

            if cleanup_ready(snapshot) and self.peer is None:
                _require(
                    snapshot.winner is Winner.DISCONNECT
                    and snapshot.outcome is Outcome.DISCONNECTED_UNCOMMITTED,
                    "permission loss requires disconnected uncommitted receipt",
                )
                _require(
                    self.first_waiting is not None
                    and time.monotonic() < self.first_waiting + LOSS_SECONDS,
                    "permission retirement exceeded pre-watchdog budget",
                )
                self.boundary(
                    "retirement-observed",
                    ticket=ticket_document(self.owned_hold),
                    status=status_document(snapshot),
                )
                _require(
                    len(self.link_events) == 4,
                    "held loss requires second real GAP disconnect",
                )
                self.no_peer()
                # Restore canonical server values only after dead peer/hold retirement.
                for characteristic in self.fixture._profile.characteristics:
                    self.authority()
                    self.no_peer()
                    self.fixture._gatt.set_value(
                        self.evidence["handles"][characteristic.role],
                        characteristic.initial_value,
                    )
                self.fixture._start_advertising()
                self.guard(deadline)
                self.drain()
                self.no_peer()
                self.facts.authorize_restored()
                self.allow_connection = self.allow_disconnect = True
                self.restored = True
                self.phase = "restored"
                self.publish(
                    RESTORED_READY_NAME,
                    "restored",
                    hold_inactive=True,
                    peer_count=0,
                    canonical_value_hex="00",
                )
        self.guard(deadline)

    def cleanup(self) -> None:
        """Failure-only owned cleanup. Authority failure never permits a mutation."""
        self.allow_connection = False
        deadline = time.monotonic() + 5
        try:
            self.authority()
            snapshot = self._status()
            if self.owned_hold is not None and not cleanup_ready(snapshot):
                limits = self.fault.get_limits()
                if snapshot.phase in (Phase.ARMED, Phase.HELD) and limits.peer_present:
                    _require(
                        (
                            limits.boot_nonce,
                            limits.connection_epoch,
                            limits.bearer_epoch,
                            limits.database_epoch,
                        )
                        == (
                            self.owned_hold.boot_nonce,
                            self.owned_hold.connection_epoch,
                            self.owned_hold.bearer_epoch,
                            self.owned_hold.database_epoch,
                        )
                        and limits.peer == snapshot.peer,
                        "foreign connection during permission cleanup",
                    )
                    self.fault.abort(self.owned_hold)
                while not cleanup_ready(snapshot):
                    snapshot = self._status()
                    _require(
                        time.monotonic() < deadline, "owned fault retirement timeout"
                    )
                    if not cleanup_ready(snapshot):
                        time.sleep(POLL_SECONDS)
            self.evidence["cleanup"]["hold_inactive"] = True
        except (OSError, ValueError, RuntimeError, EOFError, TimeoutError) as error:
            self.evidence["cleanup_errors"].append(f"hold: {error}")
        for name, action in (
            ("stop-advertising", self.fixture._gap.stop_advertising),
            ("owned-peer", lambda: self.cleanup_peer(deadline)),
        ):
            try:
                self.authority()
                _require(
                    self.evidence["cleanup"]["hold_inactive"]
                    and time.monotonic() < deadline,
                    "cleanup lacks inactive hold/budget",
                )
                action()
                if name == "stop-advertising":
                    self.evidence["cleanup"]["advertising_stopped"] = True
            except (OSError, ValueError, RuntimeError, EOFError, TimeoutError) as error:
                self.evidence["cleanup_errors"].append(f"{name}: {error}")
        self.evidence["cleanup"]["peer_count"] = (
            None if self.rejected_events else len(self.fixture._connected_peers)
        )

    def run_cycle(self) -> None:
        """Existing provider run/cycle model; no additional transport or runner."""
        deadline = time.monotonic() + PHASE_SECONDS
        progress: tuple[Any, ...] | None = None
        while not self.facts.passed or len(self.link_events) != 6:
            self.sample(deadline)
            current = (
                self.facts.phase,
                self.baseline_closed,
                self.facts.route is not None,
                self.facts.pending,
                self.loss_confirmed,
                self.restored,
            )
            if current != progress:
                progress, deadline = current, time.monotonic() + PHASE_SECONDS
            time.sleep(POLL_SECONDS)

    def _final_facts(self) -> None:
        self.evidence.update(
            native_bootstraps=self.facts.bootstraps,
            runtime_nonces=self.facts.nonces,
            resources=self.facts.resources,
            aliveProbes=[
                name
                for name in ("denied", "baseline", "restored")
                if name in self.facts.resources
            ],
            connections=sum(e["kind"] == "connected" for e in self.link_events),
            disconnections=sum(e["kind"] == "disconnected" for e in self.link_events),
            rejected_events=self.rejected_events,
            phase=self.phase,
        )

    def run(self, on_ready: Callable[[], None] = lambda: None) -> None:
        try:
            self.evidence["handles"] = self.fixture.start(before_mutations=self.prepare)
            self.prepare()
            on_ready()
            self.run_cycle()
            self.allow_connection = self.allow_disconnect = False
            self.authority()
            self.fixture._gap.stop_advertising()
            self.evidence["cleanup"]["advertising_stopped"] = True
            end = time.monotonic() + 0.25
            while time.monotonic() < end:
                event = self.fixture._session.poll_event(
                    min(POLL_SECONDS, end - time.monotonic())
                )
                if event is not None:
                    self.observe_gap(event)
            snapshot = self._status()
            self.no_peer()
            self._runtime()
            self.authority()
            _require(
                cleanup_ready(snapshot)
                and self.facts.passed
                and self.restored
                and len(self.link_events) == 6
                and not self.rejected_events
                and set(self.facts.resources)
                == {"denied", "baseline", "pending", "restored"},
                "physical close invariant failed",
            )
            self.evidence["cleanup"].update(hold_inactive=True, peer_count=0)
            self.boundary(
                "physical-closed",
                hold_inactive=True,
                peer_count=0,
                advertising_stopped=True,
            )
        except BaseException as error:
            self.evidence["primary_error"] = str(error)
            self.cleanup()
            self._final_facts()
            try:
                _publish_receipt(self.root / PHYSICAL_NAME, self.evidence)
            except (OSError, ValueError, RuntimeError) as publication_error:
                error.add_note(
                    f"permission physical publication failed: {publication_error}"
                )
            raise
        else:
            self.phase = "closed"
            self._final_facts()
            self.evidence["status"] = "passed"
            _publish_receipt(self.root / PHYSICAL_NAME, self.evidence)
        finally:
            # Generic teardown must never adopt a rejected peer or reset the server.
            self.fixture._mutation_allowed = False

    def wait_stopped(self) -> None:
        while not self.fixture._stop_requested:
            self.authority()
            self._runtime()
            self.drain()
            event = self.fixture._session.poll_event(POLL_SECONDS)
            if event is not None:
                self.observe_gap(event)


def run(
    fixture: Any, root: Path, *, on_ready: Callable[[], None] = lambda: None
) -> None:
    coordinator = PermissionTransition(fixture, root)
    coordinator.run(on_ready)
    coordinator.wait_stopped()


def serve(
    port: str, profile_path: Path, root: Path, *, fixture_session: FixtureSession | None
) -> None:
    """Provider mode entry; own only the ordinary DirectFixture lifetime."""
    from .direct_fixture import DirectFixture

    plan = validate_control(root, fixture_session)
    fixture = None
    coordinator = None
    old_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        fixture = DirectFixture(port, profile_path, fixture_session=plan)
        fixture._strict_recovery_cleanup = fixture._preserve_gatt = True
        coordinator = PermissionTransition(fixture, root)
        for sig in old_handlers:
            signal.signal(sig, lambda *_: setattr(fixture, "_stop_requested", True))
        coordinator.run(lambda: print("NRF52840_DIRECT_READY", flush=True))
        coordinator.wait_stopped()
    except BaseException as error:
        if coordinator is None:
            try:
                _publish_receipt(
                    root / PHYSICAL_NAME,
                    {
                        "schema_version": 1,
                        "mode": MODE,
                        "run_id": plan.session_id,
                        "status": "failed",
                        "primary_error": str(error),
                        "cleanup": {
                            "advertising_stopped": False,
                            "hold_inactive": False,
                            "peer_count": None,
                        },
                    },
                )
            except (OSError, ValueError, RuntimeError) as publication_error:
                error.add_note(
                    f"permission start failure publication: {publication_error}"
                )
        raise
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        if fixture is not None:
            fixture._mutation_allowed = False
            fixture.stop()
