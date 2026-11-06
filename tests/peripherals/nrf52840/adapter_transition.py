"""Single-session adapter OFF -> ON attestation; no radio toggling or firmware swap.

Host atomically publishes complete normalized runtime.log snapshots. Control is
{schema_version: 1, run_id: <fixture-session UUID>}; the request must be under
root/fixture-session. Native bootstrap/signal use the existing Android identity
fields, with only signal adding url=blehub-rn-spike://adapter/<run_id>/on.
Initial DirectFixture provisioning is unchanged; ON I/O never uses local SET_VALUE.
After provider READY, the host publishes immutable go={schema_version: 1, run_id}
within 360s. Only then do the 90s phase / 300s whole RF budgets begin. Before
sending the existing ON URL, the host must validate adapter-transition-ready.json;
that ACK opens exactly one connection window, independently of logcat latency.
A successful provider remains read-only under the same lease until stop-owned.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.react_native.check_runtime_log import check_rf_scan

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
from .recovery_ten import (
    CLEANUP_SECONDS,
    IDENTITY_FIELDS,
    POLL_SECONDS,
    RESOURCE_FIELDS,
    RecoveryTen,
)

MODE = "adapter-transition-attested"
CONTROL_NAME = "adapter-transition-control.json"
PHYSICAL_NAME = "adapter-transition-physical.json"
READY_NAME = "adapter-transition-ready.json"
GO_NAME = "go"
PREPARING_SECONDS = 360.0
PREFIX = "BLEHUB_NITRO_RUNTIME rf-adapter-transition:"
PHASE_SECONDS = 90.0
WHOLE_SECONDS = 300.0
DRAIN_SECONDS = 0.25
ADAPTER_MODES = ("startup-off", "active-loss")
ARM_NAME = "adapter-transition-armed.json"
LOSS_NAME = "adapter-transition-loss-ready.json"
OFF_REQUEST = "adapter-transition-off-request.json"
OFF_READY = "adapter-transition-off-ready.json"
OFF_CONFIRMED = "adapter-transition-off-confirmed.json"
# Test orchestration budgets, not production watchdog settings (which stay 10s).
LOSS_SECONDS = 8.0
OFF_CALL_SECONDS = 4.0
ACTIVE_GATT_SCHEMA = "active-gatt-canonical-v1"
ACTIVE_AUDIT_BYTES = 3500


def adapter_mode(value: Any) -> str:
    """Missing mode means the historical startup-only protocol, never active loss."""
    if value not in ADAPTER_MODES:
        raise ValueError("unsupported adapter mode")
    return value


def signal_url(run_id: str, mode: str, phase: str) -> str:
    adapter_mode(mode)
    suffix = phase if mode == "startup-off" else f"active-loss/{phase}"
    return f"blehub-rn-spike://adapter/{run_id}/{suffix}"


class ActiveReadFacts:
    """Finite adapter-only read-loss protocol, shared by online and offline checks.

    Acceptance markers are not native-entry evidence. Only the provider's live
    typed v2 STATUS supplies HELD; no timestamps from another process are used.
    """

    def __init__(self, run_id: str, service: str, name: str) -> None:
        self.run_id, self.service, self.name = run_id, service, name
        self.events: list[dict[str, Any]] = []
        self.domains: set[tuple[str, str]] = set()
        self.terminals: dict[str, str | None] = {}
        self.discovery: dict[str, str] | None = None
        self.arm: dict[str, Any] | None = None
        self.arm_observed = False
        self.arm_authorized = False
        self.loss_authorized = False
        self.accepted: list[str] = []
        self.advertised = False
        self.settled: str | None = None
        self.retired: dict[str, int] | None = None
        self.done = False

    def event(self, event: dict[str, Any]) -> None:
        from tools.react_native.adapter_transition_ledger import _RF_EVENT_FIELDS

        if self.done or self.retired is not None or self.settled is not None:
            raise RuntimeError("old active route callback after settlement")
        kind, command = event.get("kind"), event.get("commandId")
        audit_fields = (
            {"auditSchema", "gattSummary"} if kind == "gattDiscovered" else set()
        )
        if set(event) - (_RF_EVENT_FIELDS | audit_fields) or len(self.events) >= 128:
            raise RuntimeError("unknown/overflow active event")
        error = event.get("error")
        category = None
        if error is not None:
            details = error.get("platformDetails") if type(error) is dict else None
            if (
                type(error) is not dict
                or set(error) - {"category", "message", "platformDetails"}
                or type(error.get("message")) is not str
                or type(details) is not dict
                or details.get("platform") != "platform_android"
                or set(details) - {"platform", "name", "code", "message"}
                or any(v is not None and type(v) is not str for v in details.values())
            ):
                raise RuntimeError("active error lacks original Android error details")
            category = error.get("category")
        fields: dict[str, Any] = {"kind": kind}
        if command is not None:
            fields["commandId"] = command
        if kind == "commandCompleted":
            expected = {
                "32000": ("adapterSnapshotChanged",),
                "32002": ("scanStarted",),
                "32003": ("scanStopped",),
                "32004": ("Connecting", "Connected"),
                "32006": ("gattDiscovered",),
                "32008": (),
                "32009": (),
            }
            if command not in expected or command in self.terminals:
                raise RuntimeError("unknown/duplicate active terminal")
            if command in ("32008", "32009"):
                if (
                    not self.loss_authorized
                    or command not in self.accepted
                    or category not in ("connection", "gatt")
                ):
                    raise RuntimeError(
                        "active loss terminal before fault, success, or watchdog-first"
                    )
                if command == "32009" and "32008" not in self.terminals:
                    raise RuntimeError("queued terminal preceded active terminal")
                if (
                    command == "32009"
                    and self.terminals["32008"] == "connection"
                    and category != "connection"
                ):
                    raise RuntimeError(
                        "impossible disconnect-first queued classification"
                    )
            elif category is not None or any(
                (command, d) not in self.domains for d in expected[command]
            ):
                raise RuntimeError("active setup terminal failed or preceded domain")
            self.terminals[command] = category
        elif kind == "adapterSnapshotChanged":
            if command != "32000" or self.events:
                raise RuntimeError("unexpected active adapter snapshot")
            fields.update(adapterState="PoweredOn", scanAuthorization="Authorized")
            self.domain("32000", kind)
        elif kind == "scanStarted":
            if command != "32002" or "32000" not in self.terminals:
                raise RuntimeError("early active scan")
            fields["scanSessionId"] = "32001"
            self.domain("32002", kind)
        elif kind == "advertisementObserved":
            if (
                command is not None
                or "32002" not in self.terminals
                or ("32003", "scanStopped") in self.domains
            ):
                raise RuntimeError("late/early active advertisement")
            services = event.get("advertisedServices")
            peer = event.get("peripheralId")
            if (
                type(peer) is not str
                or re.fullmatch(r"[1-9][0-9]*", peer) is None
                or int(peer) > (1 << 64) - 1
            ):
                raise RuntimeError("invalid active peripheral ID")
            if (
                type(services) is not list
                or self.service not in services
                or event.get("localName") != self.name
            ):
                raise RuntimeError("foreign active advertisement")
            fields.update(
                scanSessionId="32001",
                peripheralId=peer,
                advertisedServices=services,
                localName=self.name,
                rssi=event.get("rssi"),
            )
            self.advertised = True
        elif kind == "scanStopped":
            if command != "32003" or not self.advertised:
                raise RuntimeError("invalid active scan stop")
            fields["scanSessionId"] = "32001"
            self.domain("32003", kind)
        elif kind == "connectionStateChanged":
            fields["connectionId"] = "32005"
            if command == "32004":
                state = event.get("connectionState")
                if "32003" not in self.terminals or state not in (
                    "Connecting",
                    "Connected",
                ):
                    raise RuntimeError("invalid setup connection")
                if state == "Connected" and (command, "Connecting") not in self.domains:
                    raise RuntimeError("Connected before Connecting")
                fields["connectionState"] = state
                self.domain(command, state)
            elif command is None:
                if not self.loss_authorized or not all(
                    c in self.terminals for c in ("32008", "32009")
                ):
                    raise RuntimeError("passive loss before fault/operation settlement")
                if category == "connection" and event.get("connectionState") is None:
                    pass
                elif (
                    category is None and event.get("connectionState") == "Disconnected"
                ):
                    fields["connectionState"] = "Disconnected"
                else:
                    raise RuntimeError("invalid passive loss policy")
                self.domain("loss", "passive")
            else:
                raise RuntimeError("foreign active connection")
        elif kind == "gattDiscovered":
            if command != "32006" or "32004" not in self.terminals:
                raise RuntimeError("early/foreign active discovery")
            fields.update(
                connectionId="32005",
                gattSessionId="32007",
                gattServices=event.get("gattServices"),
            )
            if type(fields["gattServices"]) is not list:
                raise RuntimeError("missing real GATT database")
            if "auditSchema" in event or "gattSummary" in event:
                self.validate_compact_gatt(event)
                fields.update(
                    auditSchema=ACTIVE_GATT_SCHEMA, gattSummary=event["gattSummary"]
                )
            self.domain(command, kind)
        else:
            raise RuntimeError(f"unexpected active domain: {kind}")
        if (
            any(event.get(k) != v for k, v in fields.items())
            or any(
                v is not None and k not in fields.keys() | {"error"}
                for k, v in event.items()
            )
            or kind != "commandCompleted"
            and command is not None
            and error is not None
        ):
            raise RuntimeError("active event route/foreign field/error drift")
        self.events.append(event)

    def validate_compact_gatt(self, event: dict[str, Any]) -> None:
        """Canonical topology plus full counts, explicitly not a full native replay."""
        database, summary = event["gattServices"], event.get("gattSummary")
        if (
            event.get("auditSchema") != ACTIVE_GATT_SCHEMA
            or type(summary) is not dict
            or set(summary)
            != {"serviceCount", "characteristicCount", "descriptorCount"}
        ):
            raise RuntimeError("invalid compact GATT audit schema")
        if any(type(v) is not int or not 0 <= v < (1 << 53) for v in summary.values()):
            raise RuntimeError("invalid compact GATT summary counts")
        if (
            len(database) != 1
            or type(database[0]) is not dict
            or set(database[0]) != {"id", "uuid", "primary", "characteristics"}
        ):
            raise RuntimeError("compact GATT must retain exactly the canonical service")
        service = database[0]
        if (
            service["uuid"] != self.service
            or service["primary"] is not True
            or type(service["characteristics"]) is not list
            or len(service["characteristics"]) != 5
        ):
            raise RuntimeError("compact canonical service topology drift")
        ids: set[str] = set()

        def identifier(value: Any) -> None:
            if (
                type(value) is not str
                or re.fullmatch(r"[1-9][0-9]*", value) is None
                or int(value) > (1 << 64) - 1
                or value in ids
            ):
                raise RuntimeError("compact GATT invalid/duplicate native instance ID")
            ids.add(value)

        identifier(service["id"])
        expected = {
            "fff10000": (True, True, True, False, False, 0),
            "fff20000": (True, False, False, True, True, 1),
            "fff50000": (True, False, False, False, True, 1),
            "fff30000": (True, False, False, False, False, 0),
            "fff40000": (False, True, True, False, False, 0),
        }
        seen: set[str] = set()
        properties = (
            "readable",
            "writableWithResponse",
            "writableWithoutResponse",
            "notifiable",
            "indicatable",
        )
        for characteristic in service["characteristics"]:
            if type(characteristic) is not dict or set(characteristic) != {
                "id",
                "uuid",
                "descriptors",
                *properties,
            }:
                raise RuntimeError("compact characteristic schema drift")
            uuid = characteristic["uuid"]
            prefix = uuid[:8] if type(uuid) is str else ""
            if (
                prefix not in expected
                or prefix in seen
                or uuid != prefix + "-0000-4000-8000-000000000000"
            ):
                raise RuntimeError("compact characteristic UUID topology drift")
            seen.add(prefix)
            identifier(characteristic["id"])
            if any(
                characteristic[k] is not v
                for k, v in zip(properties, expected[prefix][:5], strict=True)
            ):
                raise RuntimeError("compact characteristic property drift")
            descriptors = characteristic["descriptors"]
            if type(descriptors) is not list or len(descriptors) != expected[prefix][5]:
                raise RuntimeError("compact descriptor count drift")
            for descriptor in descriptors:
                if (
                    type(descriptor) is not dict
                    or set(descriptor) != {"id", "uuid"}
                    or descriptor["uuid"] != "00002902-0000-1000-8000-00805f9b34fb"
                ):
                    raise RuntimeError("compact CCCD topology drift")
                identifier(descriptor["id"])
        if (
            summary["serviceCount"] < 1
            or summary["characteristicCount"] < 5
            or summary["descriptorCount"] < 2
            or summary["serviceCount"] == 1
            and (summary["characteristicCount"], summary["descriptorCount"]) != (5, 2)
        ):
            raise RuntimeError(
                "compact full-database counts cannot omit retained topology"
            )

    def domain(self, command: str, kind: str) -> None:
        if (command, kind) in self.domains or command in self.terminals:
            raise RuntimeError("duplicate/late active domain")
        self.domains.add((command, kind))

    def observe(
        self, marker: str, bootstrap: dict[str, Any], *, require_readiness: bool
    ) -> None:
        if marker.startswith("active:event:"):
            event = _json(marker.removeprefix("active:event:").encode())
            if (
                "auditSchema" in event
                and len((PREFIX + marker).encode("utf-8")) > ACTIVE_AUDIT_BYTES
            ):
                raise RuntimeError(
                    "compact active event exceeded its audit byte budget"
                )
            self.event(event)
        elif marker.startswith("active:discovery-ready:"):
            value = _json(marker.removeprefix("active:discovery-ready:").encode())
            if self.discovery is not None or not all(
                c in self.terminals
                for c in ("32000", "32002", "32003", "32004", "32006")
            ):
                raise RuntimeError("duplicate/early discovery readiness")
            if (
                set(value) != {"connectionId", "gattSessionId", "characteristicId"}
                or value.get("connectionId") != "32005"
                or value.get("gattSessionId") != "32007"
                or type(value.get("characteristicId")) is not str
                or re.fullmatch(r"[1-9][0-9]*", value["characteristicId"]) is None
                or int(value["characteristicId"]) > (1 << 64) - 1
            ):
                raise RuntimeError("foreign discovery route")
            database = next(
                e["gattServices"] for e in self.events if e["kind"] == "gattDiscovered"
            )
            targets = [
                c
                for s in database
                if s.get("uuid") == self.service
                for c in s.get("characteristics", [])
                if c.get("id") == value.get("characteristicId")
                and c.get("readable") is True
                and c.get("writableWithResponse") is True
            ]
            if len(targets) != 1:
                raise RuntimeError("active target absent/ambiguous in native database")
            self.discovery = value
        elif marker.startswith("native:arm:"):
            value = _json(marker.removeprefix("native:arm:").encode())
            if (
                self.discovery is None
                or self.arm is not None
                or type(value.get("pid")) is not int
                or require_readiness
                and not self.arm_authorized
                or value
                != {
                    **bootstrap,
                    "source": "payload-url",
                    "url": signal_url(self.run_id, "active-loss", "arm"),
                }
            ):
                raise RuntimeError("foreign/early/duplicate active ARM signal")
            self.arm = value
        elif marker == f"active:arm-signal:runId={self.run_id}:source=linking":
            if self.arm is None or self.arm_observed:
                raise RuntimeError("early/duplicate JS ARM ACK")
            self.arm_observed = True
        elif marker.startswith("active:accepted:"):
            command = marker.removeprefix("active:accepted:")
            if (
                not self.arm_observed
                or command != ("32008" if not self.accepted else "32009")
                or len(self.accepted) >= 2
            ):
                raise RuntimeError("foreign/duplicate/out-of-order read acceptance")
            self.accepted.append(command)
        elif marker.startswith("active:settled:"):
            policy = (
                "disconnect-first"
                if self.terminals.get("32008") == "connection"
                else "read-error-first"
            )
            if (
                self.settled is not None
                or ("loss", "passive") not in self.domains
                or marker != f"active:settled:{policy}"
            ):
                raise RuntimeError("missing/incorrect active loss settlement")
            self.settled = policy
        elif marker.startswith("active:retired:"):
            value = _json(marker.removeprefix("active:retired:").encode())
            if (
                not self.settled
                or self.retired is not None
                or set(value) != RESOURCE_FIELDS
                or any(type(v) is not int or v < 0 for v in value.values())
            ):
                raise RuntimeError("invalid pre-close resource snapshot")
            if any(
                value[k] != 0
                for k in (
                    "activeScans",
                    "connections",
                    "gattAttributes",
                    "subscriptions",
                    "pendingIo",
                    "watchdogs",
                    "callbackRoutes",
                    "handoffPending",
                )
            ):
                raise RuntimeError(
                    "loss did not retire active native resources before close"
                )
            if (
                any(
                    value[k] != 1
                    for k in (
                        "ingressRoots",
                        "adapterDelegates",
                        "backendLeases",
                        "hubRoots",
                        "handoffRoots",
                        "listeners",
                        "lifecycleOwners",
                    )
                )
                or value["workerThreads"] != 2
            ):
                raise RuntimeError("pre-close snapshot is not a live production Probe")
            self.retired = value
        elif (
            marker == "active:silence:passed"
            and self.retired is not None
            and not self.done
        ):
            self.done = True
        else:
            raise RuntimeError(f"unknown/duplicate active marker: {marker}")


FLOW_PREFIXES = (
    "rf:start:",
    "rf:listener:registered-before-initialize",
    *(
        f"rf:step:{step}:passed:"
        for step in (
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
        )
    ),
    "rf:listener:removed",
    "rf:close:resolved",
    "rf:step:close:passed:",
    "rf-io:passed:",
)


def validate_control(
    root: Path, plan: FixtureSession | None, *, fresh: bool = True
) -> FixtureSession:
    """Validate immutable declarations before opening the device (not live proof)."""
    plan = require_multifunction(plan)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("adapter transition requires a private control directory")
    if plan.root.resolve() != (root / "fixture-session").resolve():
        raise ValueError("adapter transition requires root/fixture-session request")
    if load_session(plan.request_path) != plan:
        raise ValueError("adapter transition session changed")
    if {p.name for p in root.glob("*-control.json")} != {CONTROL_NAME}:
        raise ValueError("adapter transition requires exactly one control file")
    value = read_document(root / CONTROL_NAME)
    if (
        set(value)
        != (
            {"schema_version", "run_id", "adapter_mode"}
            if "adapter_mode" in value
            else {"schema_version", "run_id"}
        )
        or adapter_mode(value.get("adapter_mode", "startup-off")) not in ADAPTER_MODES
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["run_id"] != plan.session_id
    ):
        raise ValueError("invalid adapter transition control/session")
    for name in (
        (
            PHYSICAL_NAME,
            READY_NAME,
            GO_NAME,
            ARM_NAME,
            LOSS_NAME,
            OFF_REQUEST,
            OFF_READY,
            OFF_CONFIRMED,
            "abort",
        )
        if fresh
        else ()
    ):
        path = root / name
        if path.exists() or path.is_symlink():
            raise ValueError(f"adapter transition refuses stale output: {path}")
    return plan


def validate_session(
    root: Path, plan: FixtureSession | None, process: tuple[int, int]
) -> FixtureSession:
    """Live, same-process proof required before any scenario mutation."""
    plan = validate_control(root, plan, fresh=False)
    verify_receipt(plan, fixture_identity=process)
    return plan


class RuntimeFacts:
    """Exact cumulative protocol: no deduplication, foreign records or failed tails."""

    def __init__(
        self,
        run_id: str,
        service_uuid: str,
        local_name: str,
        *,
        require_readiness: bool = False,
        mode: str = "startup-off",
    ) -> None:
        self.run_id, self.service_uuid, self.local_name = (
            run_id,
            service_uuid,
            local_name,
        )
        self.lines: list[str] = []
        self.stage = 0
        self.nonce: str | None = None
        self.bootstrap: dict[str, Any] | None = None
        self.signal: dict[str, Any] | None = None
        self.flow: list[str] = []
        self.resources: dict[str, dict[str, Any]] = {}
        self.flow_passed = False
        self.started = False
        self.require_readiness = require_readiness
        self.on_authorized = False
        self.mode = adapter_mode(mode)
        self.active = (
            ActiveReadFacts(run_id, service_uuid, local_name)
            if mode == "active-loss"
            else None
        )

    @property
    def on_started(self) -> bool:
        return self.started

    @property
    def passed(self) -> bool:
        return self.stage == 14

    def refresh(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            if self.lines:
                raise RuntimeError("runtime.log disappeared")
            return
        content = read_live_snapshot(path).decode("utf-8")
        if content and not content.endswith("\n"):
            raise RuntimeError("runtime.log is not a complete normalized snapshot")
        lines = content.splitlines()
        if lines[: len(self.lines)] != self.lines:
            raise RuntimeError("runtime.log rewound or changed observed lines")
        for line in lines[len(self.lines) :]:
            self.observe(line)
        self.lines = lines

    def native(self, marker: str, kind: str) -> None:
        value = _json(marker.removeprefix(f"native:{kind}:").encode())
        if (
            set(value)
            != IDENTITY_FIELDS
            | ({"adapter_mode"} if "adapter_mode" in value else set())
            | ({"url"} if kind == "signal" else set())
            or value.get("adapter_mode", "startup-off") != self.mode
            or value["run_id"] != self.run_id
            or type(value["pid"]) is not int
            or value["pid"] <= 0
            or value["bundle_id"] != "com.blehubnitrohost"
            or any(
                type(value[k]) is not str or not value[k] or len(value[k]) > 2048
                for k in ("boot_id", "executable", "source")
            )
        ):
            raise RuntimeError("foreign/malformed native adapter identity")
        if kind == "bootstrap":
            if (
                self.stage != 0
                or self.bootstrap is not None
                or value["source"] != "launch"
            ):
                raise RuntimeError("duplicate/early native bootstrap")
            self.bootstrap = value
        else:
            if (
                self.stage != 7
                or self.require_readiness
                and not self.on_authorized
                or self.signal is not None
                or self.bootstrap is None
                or value["source"] != "payload-url"
                or value["url"] != signal_url(self.run_id, self.mode, "on")
                or any(
                    value[k] != self.bootstrap[k] for k in IDENTITY_FIELDS - {"source"}
                )
            ):
                raise RuntimeError("foreign/duplicate/early native signal")
            self.signal = value
            self.stage = 8

    def off_event(self, marker: str) -> None:
        event = _json(marker.removeprefix("off:event:").encode())
        # All native optional fields may be absent/null, never carry foreign routes.
        expected = (
            {
                "kind": "adapterSnapshotChanged",
                "commandId": "31000",
                "adapterState": "PoweredOff",
                "scanAuthorization": "Authorized",
            },
            {"kind": "commandCompleted", "commandId": "31000"},
            {"kind": "commandCompleted", "commandId": "31002"},
        )
        if self.stage not in (1, 2, 3):
            raise RuntimeError("duplicate/out-of-order OFF event")
        fields = expected[self.stage - 1]
        if any(event.get(k) != v for k, v in fields.items()) or any(
            v is not None and k not in fields.keys() | {"error"}
            for k, v in event.items()
        ):
            raise RuntimeError("foreign OFF event/route/state")
        error = event.get("error")
        if self.stage == 3:
            if (
                not isinstance(error, dict)
                or error.get("category") != "adapter_unavailable"
            ):
                raise RuntimeError("OFF scan must terminal adapter_unavailable")
        elif error is not None:
            raise RuntimeError("OFF initialize failed")
        self.stage += 1

    def observe(self, line: str) -> None:
        if not line.startswith(PREFIX) or line != line.strip():
            raise RuntimeError("foreign/non-normalized runtime line")
        marker = line[len(PREFIX) :]
        if re.search(r"(?:^|:)(?:failed|rejected)(?::|$)", marker.split("{", 1)[0]):
            raise RuntimeError(f"adapter transition runtime failure: {marker}")
        if self.active is not None and marker.startswith(("active:", "native:arm:")):
            if self.stage != 1 or self.bootstrap is None:
                raise RuntimeError("active record outside its lifecycle")
            self.active.observe(
                marker, self.bootstrap, require_readiness=self.require_readiness
            )
            if self.active.done:
                self.stage = 4
            return
        if marker.startswith("native:bootstrap:"):
            self.native(marker, "bootstrap")
            return
        if marker.startswith("native:signal:"):
            self.native(marker, "signal")
            return
        runtime = re.fullmatch(
            r"runtime:runId=([^:]+):nonce=([^:\s]+):phase=(start|close)", marker
        )
        if runtime:
            run, nonce, phase = runtime.groups()
            if run != self.run_id:
                raise RuntimeError("foreign runtime run ID")
            if phase == "start" and self.stage == 0 and self.bootstrap is not None:
                self.nonce, self.stage = nonce, 1
                return
            if phase == "close" and self.stage == 12 and nonce == self.nonce:
                self.stage = 13
                return
            raise RuntimeError("duplicate/out-of-order runtime generation")
        if marker.startswith("off:event:"):
            if self.active is not None:
                raise RuntimeError("startup-only events in active-loss mode")
            self.off_event(marker)
            return
        for phase, stage in (("off", 4), ("on", 10)):
            if marker.startswith(f"{phase}:resources:"):
                if self.stage != stage or phase == "on" and not self.flow_passed:
                    raise RuntimeError("duplicate/out-of-order resources")
                value = _json(marker.removeprefix(f"{phase}:resources:").encode())
                if set(value) != RESOURCE_FIELDS or any(
                    type(v) is not int or v != 0 for v in value.values()
                ):
                    raise RuntimeError(
                        "closed resources require exactly 17 zero counters"
                    )
                self.resources[phase] = value
                self.stage = 5 if phase == "off" else 11
                return
        if marker.startswith("on:flow:"):
            if self.stage != 10 or self.flow_passed:
                raise RuntimeError("early/late/duplicate ON flow")
            body = marker.removeprefix("on:flow:")
            expected = FLOW_PREFIXES[len(self.flow)]
            if not body.startswith(expected) or (
                not expected.endswith(":") and body != expected
            ):
                raise RuntimeError("foreign/duplicate/out-of-order ON flow record")
            self.flow.append(body)
            if body.startswith("rf-io:passed:"):
                check_rf_scan(
                    "\n".join("BLEHUB_NITRO_RUNTIME " + m for m in self.flow),
                    self.service_uuid,
                    Path("runtime.log"),
                    connect=True,
                    gatt=True,
                    io=True,
                    expected_local_name=self.local_name,
                )
                self.flow_passed = True
            return
        transitions = {
            5: ("off:passed", 6),
            6: (f"awaiting-on:runId={self.run_id}", 7),
            8: (f"on-signal:runId={self.run_id}:source=linking", 9),
            9: ("on:started", 10),
            11: ("on:passed", 12),
            13: (f"passed:runId={self.run_id}", 14),
        }
        if self.stage in transitions and marker == transitions[self.stage][0]:
            self.stage = transitions[self.stage][1]
            if marker == "on:started":
                self.started = True
            return
        raise RuntimeError(f"unknown/duplicate/out-of-order adapter marker: {marker}")


class AdapterTransition(RecoveryTen):
    """Reuse RecoveryTen's exact GAP tracker/owned-peer cleanup, not its protocol."""

    def __init__(self, fixture: Any, root: Path) -> None:
        self.fixture, self.root = fixture, root
        self.plan = validate_control(root, fixture._fixture_session)
        self.process = (os.getpid(), os.getpgid(0))
        self.control_bytes = _read_bytes(root / CONTROL_NAME)
        self.adapter_mode = adapter_mode(
            _json(self.control_bytes).get("adapter_mode", "startup-off")
        )
        self.owned_hold = None
        self.first_waiting: float | None = None
        self.loss_ready = False
        self.off_authorized = False
        self.fault_retired = False
        self.cleaning = False
        self.active_documents: dict[str, bytes] = {}
        self.fault = FaultController(fixture._session, self.plan.session_id)
        self.facts = RuntimeFacts(
            self.plan.session_id,
            self.plan.service_uuid,
            fixture._profile.local_name,
            require_readiness=True,
            mode=self.adapter_mode,
        )
        self.peer = None
        self.allow_connection = self.allow_disconnect = False
        self.link_events: list[dict[str, Any]] = []
        self.rejected_events: list[dict[str, Any]] = []
        self.cycle, self.record = -1, None  # Shared tracker boundary metadata.
        self.phase = "host-preparing"
        self.preparing_deadline = time.monotonic() + PREPARING_SECONDS
        self.whole_deadline: float | None = None
        self.go_bytes: bytes | None = None
        self.go_identity: tuple[int, int] | None = None
        self.readiness: dict[str, Any] | None = None
        self.ready_bytes: bytes | None = None
        self.receipt_bytes: bytes | None = None
        self.evidence: dict[str, Any] = {
            "schema_version": 1,
            "mode": MODE,
            "status": "failed",
            "run_id": self.plan.session_id,
            "session_id": self.plan.session_id,
            "fixture_pid": self.process[0],
            "fixture_pgid": self.process[1],
            "contract_sha256": self.plan.contract_sha256,
            "profile_sha256": self.plan.profile_sha256,
            "resource_identity": self.plan.resource_identity,
            "connections": 0,
            "disconnections": 0,
            "events": self.link_events,
            "boundaries": [],
            "hold_status": {},
            "cleanup": {
                "advertising_stopped": False,
                "hold_inactive": False,
                "peer_count": 0,
            },
            "cleanup_errors": [],
            "primary_error": None,
        }

        if "adapter_mode" in _json(self.control_bytes):
            self.evidence["adapter_mode"] = self.adapter_mode

    def active_publish(self, name: str, phase: str, **fields: Any) -> None:
        value = {
            "schema_version": 1,
            "adapter_mode": "active-loss",
            "run_id": self.plan.session_id,
            "phase": phase,
            "receipt_sha256": self.evidence["receipt_sha256"],
            "native_bootstrap": self.facts.bootstrap,
            **fields,
        }
        _publish_receipt(self.root / name, value)
        self.active_documents[name] = _read_bytes(self.root / name)
        self.evidence.setdefault("active_protocol", {})[name] = value

    def cleanup_hold(self) -> None:
        if self.owned_hold is None:
            limits = self.fault.require_idle()
            self.evidence["hold_status"] = {
                "protocol_version": 2,
                "engine_flags": int(limits.engine_flags),
            }
            return
        snapshot = self.fault.status(self.owned_hold)
        if not cleanup_ready(snapshot):
            if not self.cleaning:
                raise RuntimeError("owned fault is not retired/resource-zero/rearmable")
            deadline = time.monotonic() + 7
            limits = self.fault.get_limits()
            if snapshot.phase in (Phase.ARMED, Phase.HELD) and limits.peer_present:
                if (
                    limits.boot_nonce,
                    limits.connection_epoch,
                    limits.bearer_epoch,
                    limits.database_epoch,
                ) != (
                    self.owned_hold.boot_nonce,
                    self.owned_hold.connection_epoch,
                    self.owned_hold.bearer_epoch,
                    self.owned_hold.database_epoch,
                ) or limits.peer != snapshot.peer:
                    raise RuntimeError(
                        "foreign active-loss connection; refusing cleanup"
                    )
                self.fault.abort(self.owned_hold)
            while True:
                snapshot = self.fault.status(self.owned_hold)
                if time.monotonic() >= deadline:
                    raise TimeoutError("owned active-loss retirement deadline")
                if cleanup_ready(snapshot):
                    break
                time.sleep(POLL_SECONDS)
        self.evidence["hold_status"] = status_document(snapshot)

    def active_sample(self, deadline: float) -> None:
        """One serial owner; revalidate real WAITING again for each OFF request."""
        active = self.facts.active
        assert active is not None
        snapshot = (
            self.fault.status(self.owned_hold) if self.owned_hold is not None else None
        )
        self.guard(deadline)
        self.drain()
        self.facts.refresh(self.root / "runtime.log")
        if self.owned_hold is None:
            self.cleanup_hold()
            if active.discovery is not None:
                if self.peer is None or self.fixture._connected_peers != {self.peer}:
                    raise RuntimeError("ARM requires exactly the connected peer")
                rw = next(
                    c
                    for c in self.fixture._profile.characteristics
                    if c.role == "read-write"
                )
                from .gatt_commands import tester_characteristic_native_permissions

                attrs = self.fixture._gatt.get_attributes(attribute_uuid=rw.uuid)
                self.drain()
                target = self.evidence["handles"]["read-write"]
                if (
                    len(attrs) != 1
                    or attrs[0].handle != target
                    or attrs[0].uuid != rw.uuid
                    or attrs[0].permissions
                    != tester_characteristic_native_permissions(rw.permissions)
                ):
                    raise RuntimeError("active-loss native RW target drift")
                self.authority()
                before = self.fixture._gatt.get_attribute_value(target)
                self.guard(deadline)
                self.drain()
                if (
                    before.att_response != 0
                    or before.value != rw.initial_value
                    or self.peer is None
                ):
                    raise RuntimeError("active-loss before value/peer drift")
                peer_wire = bytes([self.peer[1]]) + bytes.fromhex(self.peer[0])[::-1]
                self.owned_hold = self.fault.arm_read(
                    target, before.value, peer_wire
                ).ticket
                snapshot = self.fault.status(self.owned_hold)
                if snapshot.phase is not Phase.ARMED:
                    raise RuntimeError("active-loss ARM did not reach ARMED")
                self.boundary(
                    "active-armed",
                    ticket=ticket_document(self.owned_hold),
                    status=status_document(snapshot),
                )
                active.arm_authorized = True
                self.active_publish(
                    ARM_NAME,
                    "armed",
                    ticket=ticket_document(self.owned_hold),
                    status=status_document(snapshot),
                    peer=list(self.peer),
                    route=active.discovery,
                )
            return
        identity = {"ticket": ticket_document(self.owned_hold)}
        if snapshot.phase is Phase.HELD and self.first_waiting is None:
            self.first_waiting = time.monotonic()
            self.boundary(
                "active-waiting",
                **identity,
                peer=list(self.peer) if self.peer else None,
            )
        if not self.fault_retired:
            if (
                self.first_waiting is not None
                and time.monotonic() >= self.first_waiting + LOSS_SECONDS
            ):
                raise TimeoutError("active-loss exceeded the pre-watchdog 8s boundary")
            if snapshot.phase not in (
                (Phase.ARMED, Phase.HELD, Phase.RETIRING, Phase.RETIRED)
                if self.off_authorized
                else (Phase.ARMED, Phase.HELD)
            ):
                raise RuntimeError(
                    "held read expired/left WAITING before owned release"
                )
        if (
            not self.loss_ready
            and active.accepted == ["32008", "32009"]
            and snapshot.phase is Phase.HELD
        ):
            if self.peer is None or self.fixture._connected_peers != {self.peer}:
                raise RuntimeError("pending read lost its exact peer")
            self.loss_ready = True
            self.active_publish(
                LOSS_NAME,
                "waiting",
                ticket=ticket_document(self.owned_hold),
                status=status_document(snapshot),
                peer=list(self.peer),
                route=active.discovery,
            )
        path = self.root / OFF_REQUEST
        if path.exists() and not self.off_authorized:
            expected = {
                "schema_version": 1,
                "adapter_mode": "active-loss",
                "run_id": self.plan.session_id,
                "loss_ready_sha256": hashlib.sha256(
                    self.active_documents.get(LOSS_NAME, b"")
                ).hexdigest(),
            }
            if not self.loss_ready or read_document(path) != expected:
                raise RuntimeError("early/foreign OFF request")
            self.active_documents[OFF_REQUEST] = _read_bytes(path)
            self.evidence.setdefault("active_protocol", {})[OFF_REQUEST] = expected
            # Do not authorize from the earlier WAITING sample or from dispatch logs.
            self.authority()
            fresh = self.fault.status(self.owned_hold)
            self.drain()
            self.guard(deadline)
            if (
                fresh.phase is not Phase.HELD
                or self.peer is None
                or self.fixture._connected_peers != {self.peer}
                or self.first_waiting is None
                or time.monotonic() >= self.first_waiting + 3
            ):
                raise RuntimeError("OFF requires fresh exact WAITING/peer within 3s")
            self.off_authorized = active.loss_authorized = True
            self.allow_disconnect = True
            self.boundary(
                "active-off-authorized",
                **identity,
                status=status_document(fresh),
                peer=list(self.peer),
            )
            self.active_publish(
                OFF_READY,
                "off-authorized",
                ticket=ticket_document(self.owned_hold),
                status=status_document(fresh),
                peer=list(self.peer),
                request_sha256=hashlib.sha256(
                    self.active_documents[OFF_REQUEST]
                ).hexdigest(),
            )
        confirmed = self.root / OFF_CONFIRMED
        if confirmed.exists() and OFF_CONFIRMED not in self.active_documents:
            expected = {
                "schema_version": 1,
                "adapter_mode": "active-loss",
                "run_id": self.plan.session_id,
                "off_ready_sha256": hashlib.sha256(
                    self.active_documents.get(OFF_READY, b"")
                ).hexdigest(),
            }
            if not self.off_authorized or read_document(confirmed) != expected:
                raise RuntimeError("early/foreign OFF confirmation")
            self.active_documents[OFF_CONFIRMED] = _read_bytes(confirmed)
            self.evidence.setdefault("active_protocol", {})[OFF_CONFIRMED] = expected
            self.boundary("active-off-confirmed", **identity)
        if (
            not self.fault_retired
            and OFF_CONFIRMED in self.active_documents
            and active.settled
        ):
            self.boundary("active-settled", policy=active.settled)
            end = min(deadline, self.first_waiting + LOSS_SECONDS, time.monotonic() + 5)
            while True:
                fresh = self.fault.status(self.owned_hold)
                self.drain()
                self.guard(end)
                if cleanup_ready(fresh) and self.peer is None:
                    if (
                        fresh.winner is not Winner.DISCONNECT
                        or fresh.outcome is not Outcome.DISCONNECTED_UNCOMMITTED
                    ):
                        raise RuntimeError(
                            "active-loss requires disconnected uncommitted receipt"
                        )
                    break
                time.sleep(POLL_SECONDS)
            self.fault_retired = True
            self.evidence["hold_status"] = status_document(fresh)
            self.boundary("active-retired", **identity, status=status_document(fresh))
        if (
            self.facts.stage == 7
            and self.readiness is None
            and self.fault_retired
            and self.peer is None
        ):
            if len(self.link_events) != 2:
                raise RuntimeError("active-loss requires its own first GAP pair")
            self.open_on_window(deadline)

    def authority(self) -> None:
        self.fixture._transport.require()
        if any(
            _read_bytes(self.root / name) != raw
            for name, raw in self.active_documents.items()
        ):
            raise RuntimeError("immutable active-loss handshake changed")
        if (os.getpid(), os.getpgid(0)) != self.process:
            raise RuntimeError("provider PID/PGID changed")
        validate_session(self.root, self.plan, self.process)
        if _read_bytes(self.root / CONTROL_NAME) != self.control_bytes:
            raise RuntimeError("immutable adapter control changed")
        receipt = _read_bytes(self.plan.receipt_path)
        if self.receipt_bytes is not None and receipt != self.receipt_bytes:
            raise RuntimeError("live receipt changed")
        if _json(receipt) != verify_receipt(self.plan, fixture_identity=self.process):
            raise RuntimeError("live receipt changed during authority validation")
        self.receipt_bytes = receipt
        self.evidence["receipt_sha256"] = hashlib.sha256(receipt).hexdigest()
        if self.go_bytes is not None:
            path = self.root / GO_NAME
            if _read_bytes(path) != self.go_bytes:
                raise RuntimeError("immutable adapter go changed")
            info = path.lstat()
            if (info.st_dev, info.st_ino) != self.go_identity:
                raise RuntimeError("immutable adapter go replaced")
        if self.ready_bytes is not None and (
            _read_bytes(self.root / READY_NAME) != self.ready_bytes
        ):
            raise RuntimeError("immutable adapter readiness changed")

    def guard(self, deadline: float) -> None:
        self.fixture._transport.require()
        if self.fixture._stop_requested or (self.root / "abort").exists():
            raise RuntimeError("adapter transition aborted by parent")
        limit = (
            self.preparing_deadline
            if self.whole_deadline is None
            else self.whole_deadline
        )
        if time.monotonic() >= min(deadline, limit):
            raise TimeoutError(
                f"adapter transition absolute budget exceeded: {self.phase}"
            )

    def prepare(self) -> None:
        """Called by DirectFixture after live attestation, before provisioning."""
        self.authority()
        self.cleanup_hold()
        self.drain()
        if self.peer is not None or self.fixture._connected_peers:
            raise RuntimeError("OFF preparation requires no peer")

    def accept_go(self) -> bool:
        if self.go_bytes is not None:
            raise RuntimeError("adapter go was already accepted")
        path = self.root / GO_NAME
        if not path.exists() and not path.is_symlink():
            return False
        raw = _read_bytes(path)
        value = _json(raw)
        if (
            set(value)
            != (
                {"schema_version", "run_id", "adapter_mode"}
                if "adapter_mode" in _json(self.control_bytes)
                else {"schema_version", "run_id"}
            )
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["run_id"] != self.plan.session_id
            or value.get("adapter_mode", "startup-off") != self.adapter_mode
        ):
            raise RuntimeError("invalid adapter go schema/session")
        info = path.lstat()
        if _read_bytes(path) != raw:
            raise RuntimeError("adapter go changed during acceptance")
        self.guard(self.preparing_deadline)
        self.go_bytes = raw
        self.go_identity = (info.st_dev, info.st_ino)
        self.whole_deadline = time.monotonic() + WHOLE_SECONDS
        self.phase = "runtime"
        self.evidence["go_sha256"] = hashlib.sha256(raw).hexdigest()
        if self.facts.active:
            self.allow_connection = True
            self.boundary("active-window-ready")
        return True

    def open_on_window(self, deadline: float) -> None:
        if self.facts.stage != 7 or self.readiness is not None:
            raise RuntimeError("adapter ON window schedule drift")
        self.guard(deadline)
        self.authority()
        self.cleanup_hold()
        self.drain()  # Still closed: even GAP buffered alongside awaiting-on fails.
        self.no_peer()
        self.guard(deadline)
        ready = {
            "schema_version": 1,
            "run_id": self.plan.session_id,
            "status": "ready",
            "receipt_sha256": self.evidence["receipt_sha256"],
            "native_bootstrap": self.facts.bootstrap,
            "monotonic": time.monotonic(),
        }
        self.boundary(
            "on-window-ready",
            monotonic=ready["monotonic"],
            hold_inactive=True,
            peer_count=0,
        )
        if "adapter_mode" in self.evidence:
            ready["adapter_mode"] = self.adapter_mode
        if self.facts.active:
            # Restore only after the first physical peer and owned hold are gone.
            for characteristic in self.fixture._profile.characteristics:
                self.authority()
                self.no_peer()
                self.fixture._gatt.set_value(
                    self.evidence["handles"][characteristic.role],
                    characteristic.initial_value,
                )
            self.fixture._start_advertising()
            self.guard(deadline)
        self.readiness = self.evidence["readiness"] = ready
        self.allow_connection = self.allow_disconnect = True
        self.facts.on_authorized = True
        _publish_receipt(self.root / READY_NAME, ready)
        self.ready_bytes = _read_bytes(self.root / READY_NAME)
        if _json(self.ready_bytes) != ready:
            raise RuntimeError("adapter readiness changed during publication")

    def sample(self, deadline: float) -> None:
        self.guard(deadline)
        self.authority()
        if self.facts.active:
            if self.whole_deadline is None and not self.accept_go():
                self.cleanup_hold()
                self.drain()
                return
            self.active_sample(deadline)
            if self.facts.on_started and "on-started" not in self.evidence:
                self.evidence["on-started"] = time.monotonic()
            self.guard(deadline)
            return
        self.cleanup_hold()
        self.drain()  # Pending frames cannot be retroactively authorized by a log.
        if self.whole_deadline is None:
            if self.accept_go():
                return
            self.facts.refresh(self.root / "runtime.log")
            # Host may publish go then launch between the first check and this
            # snapshot. Recheck go before declaring premature runtime.
            if self.facts.bootstrap is not None or self.facts.stage != 0:
                if self.accept_go():
                    return
                raise RuntimeError("adapter runtime preceded host go")
            self.guard(deadline)
            return
        self.facts.refresh(self.root / "runtime.log")
        if self.facts.stage == 7 and self.readiness is None:
            self.open_on_window(deadline)
        if self.facts.on_started and "on-started" not in self.evidence:
            self.evidence["on-started"] = time.monotonic()
        self.guard(deadline)

    def observe_gap(self, event: Any) -> None:
        try:
            super().observe_gap(event)
        except RuntimeError:
            # Do not adopt an invalid/foreign peer for cleanup, or claim the
            # accepted-event tracker proves that no physical peers remain.
            self.rejected_events.append(
                {
                    "service": event.service,
                    "opcode": event.opcode,
                    "index": event.index,
                    "payload_hex": (
                        event.payload.hex()
                        if isinstance(event.payload, bytes)
                        else repr(event.payload)
                    ),
                    "monotonic": time.monotonic(),
                }
            )
            raise

    def drain(self) -> None:
        deadline = time.monotonic() + CLEANUP_SECONDS
        while (event := self.fixture._session.pop_event()) is not None:
            self.fixture._transport.require()
            if time.monotonic() >= deadline:
                raise TimeoutError("GAP drain budget exceeded")
            self.observe_gap(event)
        if self.fixture._connected_peers != ({self.peer} if self.peer else set()):
            raise RuntimeError("exact GAP tracker drift")

    def cleanup(self) -> None:
        """Revalidate authority and inactive hold BEFORE every owned mutation."""
        self.allow_connection = False
        self.cleaning = True
        deadline = time.monotonic() + CLEANUP_SECONDS
        for name, action in (
            ("stop-advertising", self.fixture._gap.stop_advertising),
            ("owned-peer-disconnect", lambda: self.cleanup_peer(deadline)),
        ):
            try:
                self.authority()
                self.cleanup_hold()
                self.drain()
                if time.monotonic() >= deadline:
                    raise TimeoutError("cleanup absolute budget exceeded")
                action()
                if time.monotonic() >= deadline:
                    raise TimeoutError("cleanup ACK exceeded absolute budget")
                if name == "stop-advertising":
                    self.evidence["cleanup"]["advertising_stopped"] = True
            except (OSError, ValueError, RuntimeError, EOFError, TimeoutError) as error:
                self.evidence["cleanup_errors"].append(f"{name}: {error}")
        try:
            end = min(deadline, time.monotonic() + DRAIN_SECONDS)
            while time.monotonic() < end:
                self.fixture._transport.require()
                event = self.fixture._session.poll_event(
                    min(POLL_SECONDS, end - time.monotonic())
                )
                if event is not None:
                    self.observe_gap(event)
            if time.monotonic() >= deadline:
                raise TimeoutError("final cleanup drain exceeded absolute budget")
            self.cleanup_hold()
            self.drain()
            if time.monotonic() >= deadline:
                raise TimeoutError("final hold/drain response exceeded cleanup budget")
            self.evidence["cleanup"]["hold_inactive"] = True
        except (OSError, ValueError, RuntimeError, EOFError, TimeoutError) as error:
            self.evidence["cleanup_errors"].append(f"final-drain: {error}")
        self.evidence["cleanup"]["peer_count"] = (
            None if self.rejected_events else len(self.fixture._connected_peers)
        )

    def final_facts(self) -> None:
        self.evidence.update(
            connections=sum(e["kind"] == "connected" for e in self.link_events),
            disconnections=sum(e["kind"] == "disconnected" for e in self.link_events),
            resources=self.facts.resources,
            phase=self.phase,
            rejected_events=self.rejected_events,
            native_bootstrap=self.facts.bootstrap,
            native_signal=self.facts.signal,
        )

    def run(self, on_ready: Callable[[], None] = lambda: None) -> None:
        cleaned = False
        try:
            # One DirectFixture, one attestation and one BTP lease. Initial profile
            # provisioning is permitted only after prepare's live/no-peer/hold proof.
            self.evidence["handles"] = self.fixture.start(before_mutations=self.prepare)
            self.preparing_deadline = time.monotonic() + PREPARING_SECONDS
            self.prepare()
            self.guard(self.preparing_deadline)
            on_ready()
            while self.whole_deadline is None:
                self.sample(self.preparing_deadline)
                if self.whole_deadline is None:
                    time.sleep(POLL_SECONDS)
            stage = self.facts.stage
            deadline = time.monotonic() + PHASE_SECONDS
            pairs = 4 if self.facts.active else 2
            while not self.facts.passed or len(self.link_events) != pairs:
                self.sample(deadline)
                if self.facts.stage != stage:
                    stage, deadline = self.facts.stage, time.monotonic() + PHASE_SECONDS
                time.sleep(POLL_SECONDS)
            self.allow_connection = self.allow_disconnect = False
            self.cleanup()
            cleaned = True
            self.facts.refresh(self.root / "runtime.log")
            self.authority()
            self.final_facts()
            if (
                not self.facts.passed
                or self.evidence["connections"] != (2 if self.facts.active else 1)
                or self.evidence["disconnections"] != (2 if self.facts.active else 1)
                or self.evidence["cleanup_errors"]
                or self.readiness is None
                or self.peer is not None
                or self.evidence["cleanup"]
                != {"advertising_stopped": True, "hold_inactive": True, "peer_count": 0}
            ):
                raise RuntimeError("adapter transition physical close invariant failed")
        except BaseException as error:
            self.evidence["primary_error"] = str(error)
            if not cleaned:
                self.cleanup()
            self.final_facts()
            try:
                _publish_receipt(self.root / PHYSICAL_NAME, self.evidence)
            except (OSError, ValueError, RuntimeError) as publication_error:
                error.add_note(
                    f"adapter physical evidence publication failed: {publication_error}"
                )
            raise
        else:
            self.phase = self.evidence["phase"] = "closed"
            self.evidence["status"] = "passed"
            _publish_receipt(self.root / PHYSICAL_NAME, self.evidence)
        finally:
            # Generic DirectFixture teardown must never reset or adopt foreign peers.
            self.fixture._mutation_allowed = False

    def wait_stopped(self) -> None:
        """Keep this exact process/session/lease alive; no post-pass mutations."""
        while not self.fixture._stop_requested:
            self.authority()
            self.facts.refresh(self.root / "runtime.log")
            self.drain()
            event = self.fixture._session.poll_event(POLL_SECONDS)
            if event is not None:
                self.observe_gap(event)


def record_start_failure(
    root: Path, plan: FixtureSession, error: BaseException
) -> None:
    """No device owner was acquired: retain failure, never claim physical cleanup."""
    try:
        validate_control(root, plan)
        _publish_receipt(
            root / PHYSICAL_NAME,
            {
                "schema_version": 1,
                "mode": MODE,
                "status": "failed",
                "run_id": plan.session_id,
                "session_id": plan.session_id,
                "fixture_pid": os.getpid(),
                "fixture_pgid": os.getpgid(0),
                "contract_sha256": plan.contract_sha256,
                "profile_sha256": plan.profile_sha256,
                "resource_identity": plan.resource_identity,
                "connections": 0,
                "disconnections": 0,
                "events": [],
                "cleanup": {
                    "advertising_stopped": False,
                    "hold_inactive": False,
                    "peer_count": None,
                },
                "cleanup_errors": [
                    "fixture not acquired; no mutation/cleanup authority"
                ],
                "primary_error": str(error),
            },
        )
    except (OSError, ValueError, RuntimeError) as publication_error:
        error.add_note(f"adapter start failure publication failed: {publication_error}")


def run(
    fixture: Any, root: Path, *, on_ready: Callable[[], None] = lambda: None
) -> None:
    coordinator = AdapterTransition(fixture, root)
    coordinator.run(on_ready)
    coordinator.wait_stopped()
