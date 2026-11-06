"""In-process startup-off physical owner; no OS control, ARM or runtime parser.

The parent prepares FixtureSession and strictly audits the OFF/full journals before
calling open_on/complete. BTP disconnect events have no reason code: only the
parent's successful full ledger proves the logical disconnect, never a timeout.
Receipts and replay are cooperative evidence, not independent RF attestation.
"""

from __future__ import annotations

import copy
import hashlib
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .cancellation_control import require_multifunction, write_document
from .direct_fixture import DirectFixture
from .fault_control import FaultController
from .fixture_attestation import parse_attestation
from .fixture_session import (
    PROOF_METHOD,
    FixtureSession,
    _json,
    _publish_receipt,
    _read_bytes,
    load_session,
    verify_receipt,
)
from .recovery_ten import CLEANUP_SECONDS, POLL_SECONDS, RESOURCE_FIELDS, RecoveryTen

MODE = "ios-adapter-startup-attested"
READY_NAME = "ios-adapter-startup-ready.json"
PHYSICAL_NAME = "ios-adapter-startup-physical.json"
PREPARING_SECONDS = 240.0
WHOLE_SECONDS = 300.0
DRAIN_SECONDS = 0.25
BUNDLE_ID = "org.reactjs.native.example.BleHubNitroHost"
OFF_FIELDS = {
    "schema_version",
    "run_id",
    "native_bootstrap",
    "runtime_nonce",
    "journal_sha256",
    "resources",
}
READY_FIELDS = {
    "schema_version",
    "run_id",
    "status",
    "receipt_sha256",
    "native_bootstrap",
    "runtime_nonce",
    "off_journal_sha256",
    "monotonic",
}


def _require(valid: bool, message: str) -> None:
    if not valid:
        raise ValueError(message)


def _uuid(value: Any) -> None:
    _require(
        type(value) is str
        and re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", value)
        is not None,
        "canonical run/boot UUID required",
    )
    _require(str(uuid.UUID(value)) == value, "canonical UUID required")


def _sha(value: Any) -> None:
    _require(
        type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        "canonical SHA-256 required",
    )


def _clock(value: Any) -> None:
    _require(
        type(value) in (int, float) and math.isfinite(value) and value >= 0,
        "finite nonnegative monotonic required",
    )


def _zeros(value: Any) -> None:
    _require(
        type(value) is dict
        and set(value) == RESOURCE_FIELDS
        and all(type(v) is int and v == 0 for v in value.values()),
        "closed resources require exactly seventeen integer zeros",
    )


def _native(value: Any, run: str) -> None:
    _require(
        type(value) is dict
        and set(value)
        == {
            "run_id",
            "adapter_mode",
            "pid",
            "boot_id",
            "bundle_id",
            "executable",
            "source",
        },
        "native bootstrap exact schema required",
    )
    _uuid(value["boot_id"])
    _require(
        value["run_id"] == run
        and value["adapter_mode"] == "startup-off"
        and type(value["pid"]) is int
        and 0 < value["pid"] <= 2147483647
        and value["bundle_id"] == BUNDLE_ID
        and value["source"] == "launch"
        and type(value["executable"]) is str
        and re.fullmatch(r"file:///[^\s?#\\]+", value["executable"]) is not None,
        "foreign native bootstrap identity",
    )


def _identity(value: dict, run: str) -> None:
    _require(value.get("run_id") == run, "foreign runtime run")
    _native(value.get("native_bootstrap"), run)
    nonce = value.get("runtime_nonce")
    _require(
        type(nonce) is str
        and 0 < len(nonce) <= 128
        and re.fullmatch(r"[0-9a-z]+-[0-9a-z]+-[0-9a-z]+", nonce) is not None,
        "canonical runtime nonce required",
    )


def _off(value: Any, run: str) -> None:
    _require(
        type(value) is dict and set(value) == OFF_FIELDS, "OFF exact schema required"
    )
    _require(
        type(value["schema_version"]) is int and value["schema_version"] == 1,
        "OFF schema_version must be integer 1",
    )
    _identity(value, run)
    _sha(value["journal_sha256"])
    _zeros(value["resources"])


def _ready(value: Any, run: str, digest: str, off: dict) -> None:
    _require(
        type(value) is dict and set(value) == READY_FIELDS,
        "readiness exact schema required",
    )
    _require(
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["status"] == "ready"
        and value["receipt_sha256"] == digest
        and value["off_journal_sha256"] == off["journal_sha256"],
        "readiness version/status/receipt/OFF binding",
    )
    _identity(value, run)
    _require(
        all(value[k] == off[k] for k in ("native_bootstrap", "runtime_nonce")),
        "readiness native/runtime identity drift",
    )
    _clock(value["monotonic"])


def _runtime(value: Any, run: str, off: dict) -> None:
    # Extra full-ledger facts belong to the parent; do not implement its parser here.
    _require(type(value) is dict, "full runtime ledger required")
    _identity(value, run)
    _require(
        type(value.get("schema_version")) is int
        and value["schema_version"] == 1
        and value.get("platform") == "ios"
        and value.get("scenario") == "adapter-transition"
        and value.get("adapter_mode") == "startup-off"
        and value.get("runtime_status") == "passed"
        and all(value[k] == off[k] for k in ("native_bootstrap", "runtime_nonce")),
        "successful full runtime ledger/identity required",
    )
    _sha(value.get("journal_sha256"))
    closed = value.get("closed_resources")
    _require(
        type(closed) is dict and set(closed) == {"off", "on"},
        "full runtime closed resources required",
    )
    for snapshot in closed.values():
        _zeros(snapshot)
    _require(closed["off"] == off["resources"], "OFF resources drift")


class _Links(RecoveryTen):
    """Reuse only RecoveryTen's exact tracker and owned-peer cleanup mechanics."""

    def __init__(self, fixture: DirectFixture, evidence: dict) -> None:
        self.fixture = fixture
        self.evidence = evidence
        self.peer = None
        self.allow_connection = self.allow_disconnect = False
        self.link_events = evidence["events"]
        self.cycle, self.record = -1, None
        self.uncertain = False

    def observe_gap(self, event: Any) -> None:
        try:
            super().observe_gap(event)
        except RuntimeError:
            self.uncertain = True
            self.evidence["rejected_events"].append(
                {
                    "service": event.service,
                    "opcode": event.opcode,
                    "index": event.index,
                    "payload_hex": event.payload.hex()
                    if type(event.payload) is bytes
                    else repr(event.payload),
                    "monotonic": time.monotonic(),
                }
            )
            raise

    def drain(self) -> None:
        try:
            super().drain()
        except BaseException:
            # Once a malformed/unbounded stream is consumed, absence is not proved.
            self.uncertain = True
            raise
        if self.uncertain:
            raise RuntimeError(
                "GAP ownership is uncertain; refusing peer adoption/mutation"
            )


class IosAdapterStartup:
    """One parent-process DirectFixture/lease/BTP owner, startup-off only.

    start/guard/open_on/complete failures are terminal. Always call finish in the
    parent's finally block; start also closes on failure, including partial init.
    No parent callback is invoked by guard, and no provider PID file is written.
    """

    def __init__(self, plan: FixtureSession, root: Path) -> None:
        self.plan = require_multifunction(plan)
        _uuid(plan.session_id)
        _require(load_session(plan.request_path) == plan, "prepared session changed")
        _require(
            root.is_dir() and not root.is_symlink(), "private existing root required"
        )
        for name in (READY_NAME, PHYSICAL_NAME, "abort"):
            _require(
                not (root / name).exists() and not (root / name).is_symlink(),
                f"stale startup output/cancellation: {name}",
            )
        _require(
            not plan.receipt_path.exists() and not plan.receipt_path.is_symlink(),
            "startup requires a fresh installed-image receipt",
        )
        self.root = root
        self.process = (os.getpid(), os.getpgid(0))
        self.fixture: DirectFixture | None = None
        self.links: _Links | None = None
        self.fault: FaultController | None = None
        self.receipt_bytes: bytes | None = None
        self.ready_bytes: bytes | None = None
        self.physical_bytes: bytes | None = None
        self.readiness: dict | None = None
        self.off: dict | None = None
        self.started = self.completed = self.finished = False
        now = time.monotonic()
        self.preparing_deadline = now + PREPARING_SECONDS
        self.whole_deadline = now + WHOLE_SECONDS
        self.cleanup_deadline: float | None = None
        self.evidence: dict[str, Any] = {
            "schema_version": 1,
            "mode": MODE,
            "status": "failed",
            "run_id": plan.session_id,
            "session_id": plan.session_id,
            "fixture_pid": self.process[0],
            "fixture_pgid": self.process[1],
            "contract_sha256": plan.contract_sha256,
            "profile_sha256": plan.profile_sha256,
            "resource_identity": plan.resource_identity,
            "port": plan.port,
            "service_uuid": plan.service_uuid,
            "receipt_sha256": None,
            "handles": {},
            "events": [],
            "boundaries": [],
            "rejected_events": [],
            "readiness": None,
            "resources": None,
            "hold_status": {},
            "cleanup": {
                "advertising_stopped": False,
                "hold_inactive": False,
                "peer_count": None,
            },
            "cleanup_errors": [],
            "primary_error": None,
            "connections": 0,
            "disconnections": 0,
        }

    def _remember(self, error: BaseException) -> None:
        if self.evidence["primary_error"] is None:
            self.evidence["primary_error"] = f"{type(error).__name__}: {error}"
        self.evidence["status"] = "failed"

    def _deadline(self) -> None:
        limit = self.cleanup_deadline
        if limit is None:
            limit = (
                self.whole_deadline if self.off is not None else self.preparing_deadline
            )
        if (
            time.monotonic() >= min(limit, self.whole_deadline)
            and self.cleanup_deadline is None
        ):
            raise TimeoutError("iOS adapter startup absolute budget exceeded")
        if self.cleanup_deadline is not None and time.monotonic() >= limit:
            raise TimeoutError("iOS adapter startup cleanup budget exceeded")

    def _authority(self) -> None:
        assert self.fixture is not None
        self.fixture._transport.require()
        _require(
            (os.getpid(), os.getpgid(0)) == self.process, "fixture PID/PGID changed"
        )
        raw = _read_bytes(self.plan.receipt_path)
        _require(
            self.receipt_bytes is None or raw == self.receipt_bytes,
            "immutable receipt changed",
        )
        receipt = verify_receipt(self.plan, fixture_identity=self.process)
        _require(
            _json(raw) == receipt and _read_bytes(self.plan.receipt_path) == raw,
            "receipt changed during validation",
        )
        self.receipt_bytes = raw
        self.evidence["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
        if self.ready_bytes is not None:
            _require(
                _read_bytes(self.root / READY_NAME) == self.ready_bytes,
                "immutable readiness changed",
            )
        self._deadline()

    def _sample(self) -> None:
        assert self.links is not None and self.fault is not None
        self._authority()
        self.links.drain()
        limits = self.fault.require_idle()
        self._authority()
        self.links.drain()
        peer = self.links.peer
        wire = bytes([peer[1]]) + bytes.fromhex(peer[0])[::-1] if peer else bytes(7)
        _require(
            limits.peer_present == (peer is not None) and limits.peer == wire,
            "live peer/exact GAP tracker mismatch",
        )
        self.evidence["hold_status"] = {
            "protocol_version": 2,
            "engine_flags": int(limits.engine_flags),
            "boot_nonce": limits.boot_nonce.hex(),
        }

    def _boundary(self, name: str, **fields: Any) -> None:
        self.evidence["boundaries"].append(
            {"boundary": name, "monotonic": time.monotonic(), **fields}
        )

    def start(self) -> None:
        _require(not self.started and not self.finished, "startup owner is single-use")
        self.started = True
        try:
            self._deadline()
            self.fixture = DirectFixture(
                self.plan.port, self.plan.profile_path, fixture_session=self.plan
            )
            self.fixture._strict_recovery_cleanup = True
            self.links = _Links(self.fixture, self.evidence)
            self.fault = FaultController(self.fixture._session, self.plan.session_id)
            try:
                handles = self.fixture.start(before_mutations=self._sample)
            finally:
                # DirectFixture's initial-hook failure already disables mutation before
                # stop(); doctor failures/success must also never use generic teardown.
                self.fixture._mutation_allowed = False
            self.evidence["handles"] = dict(handles)
            self._sample()
            self._boundary("prepared", hold_inactive=True, peer_count=0)
        except BaseException as error:
            self.finish(error)
            raise

    def guard(self) -> None:
        try:
            _require(
                self.started
                and not self.finished
                and not self.completed
                and self.evidence["primary_error"] is None,
                "startup owner is not running",
            )
            assert self.fixture is not None and self.links is not None
            _require(
                not self.fixture._stop_requested and not (self.root / "abort").exists(),
                "startup aborted by parent",
            )
            self._sample()  # Request flushes queued transport frames before authorization.
            event = self.fixture._session.poll_event(POLL_SECONDS)
            if event is not None:
                self.links.observe_gap(event)
            self.links.drain()
            self._authority()
        except BaseException as error:
            self._remember(error)
            raise

    def open_on(self, off: dict) -> dict:
        try:
            _require(self.off is None, "ON already authorized")
            _off(off, self.plan.session_id)
            self.guard()  # Still closed: queued early peers cannot be retroactively owned.
            self._sample()
            assert self.links is not None
            _require(self.links.peer is None, "ON requires no peer")
            snapshot = copy.deepcopy(off)
            ready = {
                "schema_version": 1,
                "run_id": self.plan.session_id,
                "status": "ready",
                "receipt_sha256": self.evidence["receipt_sha256"],
                "native_bootstrap": snapshot["native_bootstrap"],
                "runtime_nonce": snapshot["runtime_nonce"],
                "off_journal_sha256": snapshot["journal_sha256"],
                "monotonic": time.monotonic(),
            }
            _publish_receipt(self.root / READY_NAME, ready)
            self.ready_bytes = _read_bytes(self.root / READY_NAME)
            _require(_json(self.ready_bytes) == ready, "readiness publication changed")
            self.off = snapshot
            self.readiness = copy.deepcopy(ready)
            self.evidence.update(off=snapshot, readiness=copy.deepcopy(ready))
            self._boundary(
                "on-window-ready",
                monotonic=ready["monotonic"],
                hold_inactive=True,
                peer_count=0,
            )
            self.links.allow_connection = self.links.allow_disconnect = True
            return copy.deepcopy(ready)
        except BaseException as error:
            self._remember(error)
            raise

    def _cleanup(self) -> None:
        assert self.fixture is not None and self.links is not None
        self.cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
        try:
            # First drain while the original admission window is intact, then close
            # it. Never adopt a connection after a rejected/malformed event.
            for name in ("stop-advertising", "owned-peer-disconnect", "final-drain"):
                try:
                    self._sample()
                    self.links.allow_connection = False
                    if name == "stop-advertising":
                        self.fixture._gap.stop_advertising()
                        self._authority()
                        self.evidence["cleanup"]["advertising_stopped"] = True
                    elif name == "owned-peer-disconnect":
                        self.links.cleanup_peer(self.cleanup_deadline)
                    else:
                        end = min(
                            self.cleanup_deadline, time.monotonic() + DRAIN_SECONDS
                        )
                        while time.monotonic() < end:
                            self._authority()
                            event = self.fixture._session.poll_event(
                                min(POLL_SECONDS, end - time.monotonic())
                            )
                            if event is not None:
                                self.links.observe_gap(event)
                        self._sample()
                        _require(
                            self.links.peer is None, "cleanup still has owned peer"
                        )
                        self.evidence["cleanup"]["hold_inactive"] = True
                        self._boundary(
                            "physical-closed", hold_inactive=True, peer_count=0
                        )
                except BaseException as error:  # noqa: BLE001 - attempt all cleanup stages, preserving primary
                    self.evidence["cleanup_errors"].append(
                        f"{name}: {type(error).__name__}: {error}"
                    )
            self.evidence["cleanup"]["peer_count"] = (
                None
                if self.links.uncertain or self.evidence["cleanup_errors"]
                else len(self.fixture._connected_peers)
            )
        finally:
            self.cleanup_deadline = None

    def _counts(self) -> None:
        self.evidence["connections"] = sum(
            e["kind"] == "connected" for e in self.evidence["events"]
        )
        self.evidence["disconnections"] = sum(
            e["kind"] == "disconnected" for e in self.evidence["events"]
        )

    def _save(self) -> None:
        self._counts()
        if self.physical_bytes is None:
            _publish_receipt(self.root / PHYSICAL_NAME, self.evidence)
        else:
            _require(
                _read_bytes(self.root / PHYSICAL_NAME) == self.physical_bytes,
                "owned physical evidence changed",
            )
            write_document(self.root / PHYSICAL_NAME, self.evidence)
        self.physical_bytes = _read_bytes(self.root / PHYSICAL_NAME)

    def complete(self, runtime: dict) -> dict:
        try:
            _require(
                self.off is not None and self.readiness is not None,
                "ON was not authorized",
            )
            _runtime(runtime, self.plan.session_id, self.off)
            _require(
                runtime.get("service_uuid") == self.plan.service_uuid,
                "runtime service drift",
            )
            self.guard()
            assert self.links is not None
            _require(
                self.links.peer is None
                and [e["kind"] for e in self.evidence["events"]]
                == ["connected", "disconnected"],
                "runtime close requires exact GAP pair, not timeout/cleanup",
            )
            self.evidence.update(
                resources=copy.deepcopy(runtime["closed_resources"]),
                native_bootstrap=copy.deepcopy(runtime["native_bootstrap"]),
                runtime_nonce=runtime["runtime_nonce"],
                runtime_journal_sha256=runtime["journal_sha256"],
            )
            self._cleanup()
            _require(not self.evidence["cleanup_errors"], "physical cleanup failed")
            self.evidence["status"] = "passed"
            self._counts()
            verify_physical(
                self.evidence,
                request=_json(_read_bytes(self.plan.request_path)),
                receipt=_json(self.receipt_bytes),
                receipt_sha256=self.evidence["receipt_sha256"],
                runtime=runtime,
                readiness=self.readiness,
            )
            self._save()
            self.completed = True
            return copy.deepcopy(self.evidence)
        except BaseException as error:
            self._remember(error)
            raise

    def finish(self, primary: BaseException | None) -> list[str]:
        """Persist final verdict and close only our resources; never mask primary."""
        if self.finished:
            return list(self.evidence["cleanup_errors"])
        if primary is not None:
            self._remember(primary)
        if not self.completed and self.evidence["primary_error"] is None:
            self._remember(RuntimeError("startup owner finished before complete"))
        try:
            if self.fixture is not None and not self.fixture._stopping:
                if not self.completed:
                    self._cleanup()
                else:
                    # Retain authority through release; do not mutate after pass.
                    self._sample()
                    _require(self.links.peer is None, "peer appeared after complete")
        except BaseException as error:  # noqa: BLE001 - release resources even on interruption
            self.evidence["cleanup_errors"].append(
                f"finish: {type(error).__name__}: {error}"
            )
        finally:
            if self.fixture is not None:
                self.fixture._mutation_allowed = False
                try:
                    self.fixture.stop()
                except BaseException as error:  # noqa: BLE001 - report close independently of primary
                    self.evidence["cleanup_errors"].append(
                        f"resource-close: {type(error).__name__}: {error}"
                    )
            self.finished = True
        if self.evidence["cleanup_errors"]:
            self.evidence["status"] = "failed"
        try:
            self._save()
        except BaseException as error:  # noqa: BLE001 - finish returns cleanup errors without masking primary
            self.evidence["status"] = "failed"
            self.evidence["cleanup_errors"].append(
                f"physical-publication: {type(error).__name__}: {error}"
            )
        return list(self.evidence["cleanup_errors"])


def verify_physical(
    physical: dict,
    *,
    request: dict,
    receipt: dict,
    receipt_sha256: str,
    runtime: dict,
    readiness: dict,
) -> dict:
    """Pure successful replay binding; caller authenticates inputs/receipt byte hash.

    No disk, current PID, clock or tools-ledger import. This validates cooperative
    supplied facts, not freshness, Settings control or independent RF provenance.
    """
    _require(
        type(request) is dict and type(receipt) is dict and type(physical) is dict,
        "physical/request/receipt objects required",
    )
    run = request.get("session_id")
    _uuid(run)
    _sha(receipt_sha256)
    for value in (request, receipt, physical):
        _require(
            type(value.get("schema_version")) is int and value["schema_version"] == 1,
            "physical/request/receipt schema_version",
        )
    _require(
        receipt.get("proof_method") == PROOF_METHOD
        and type(receipt.get("installed_identity")) is dict,
        "installed-image receipt required",
    )
    installed = parse_attestation(receipt["installed_identity"])
    _require(
        installed.service_uuid == request.get("service_uuid"),
        "installed service mismatch",
    )
    _sha(installed.image_sha256)
    for key in (
        "session_id",
        "contract_sha256",
        "profile_sha256",
        "resource_identity",
        "port",
    ):
        _require(
            key in request
            and receipt.get(key) == request[key]
            and physical.get(key) == request[key],
            f"physical receipt/request mismatch: {key}",
        )
    for key in ("contract_sha256", "profile_sha256"):
        _sha(request[key])
    for key in ("fixture_pid", "fixture_pgid"):
        _require(
            type(receipt.get(key)) is int
            and receipt[key] > 0
            and type(physical.get(key)) is int
            and physical[key] == receipt[key],
            f"physical process identity mismatch: {key}",
        )
    _require(
        physical.get("mode") == MODE
        and physical.get("status") == "passed"
        and physical.get("run_id") == run
        and physical.get("receipt_sha256") == receipt_sha256
        and physical.get("service_uuid") == request.get("service_uuid"),
        "physical mode/status/run/receipt/service mismatch",
    )
    off = physical.get("off")
    _off(off, run)
    _ready(readiness, run, receipt_sha256, off)
    _runtime(runtime, run, off)
    _identity(physical, run)
    resources = physical.get("resources")
    _require(
        type(resources) is dict and set(resources) == {"off", "on"},
        "physical closed resources schema",
    )
    for snapshot in resources.values():
        _zeros(snapshot)
    _ready(physical.get("readiness"), run, receipt_sha256, off)
    _require(
        runtime.get("service_uuid") == request.get("service_uuid")
        and physical.get("readiness") == readiness
        and physical.get("resources") == runtime["closed_resources"]
        and physical.get("runtime_journal_sha256") == runtime["journal_sha256"]
        and physical.get("native_bootstrap") == runtime["native_bootstrap"]
        and physical.get("runtime_nonce") == runtime["runtime_nonce"],
        "physical full runtime/readiness binding mismatch",
    )
    _require(
        physical.get("primary_error") is None
        and physical.get("cleanup_errors") == []
        and physical.get("rejected_events") == []
        and physical.get("cleanup")
        == {
            "advertising_stopped": True,
            "hold_inactive": True,
            "peer_count": 0,
        },
        "physical errors/cleanup invariant",
    )
    cleanup = physical["cleanup"]
    _require(
        type(cleanup) is dict
        and cleanup["advertising_stopped"] is True
        and cleanup["hold_inactive"] is True
        and type(cleanup["peer_count"]) is int,
        "physical cleanup exact types",
    )
    hold = physical.get("hold_status", {})
    _require(
        type(hold) is dict
        and type(hold.get("protocol_version")) is int
        and hold["protocol_version"] == 2
        and type(hold.get("engine_flags")) is int
        and hold["engine_flags"] == 0
        and type(hold.get("boot_nonce")) is str
        and re.fullmatch(r"[0-9a-f]{32}", hold["boot_nonce"]) is not None,
        "inactive live fault proof required",
    )
    handles = physical.get("handles")
    _require(
        type(handles) is dict
        and bool(handles)
        and all(type(v) is int and 0 < v <= 65535 for v in handles.values())
        and len(set(handles.values())) == len(handles),
        "invalid physical handles",
    )
    events = physical.get("events")
    _require(
        type(events) is list
        and len(events) == 2
        and [e.get("kind") for e in events if type(e) is dict]
        == ["connected", "disconnected"]
        and type(physical.get("connections")) is int
        and physical["connections"] == 1
        and type(physical.get("disconnections")) is int
        and physical["disconnections"] == 1,
        "exactly one GAP connection/disconnection required",
    )
    last = readiness["monotonic"]
    peer = None
    for event, length in zip(events, (13, 7), strict=True):
        _require(
            set(event) == {"kind", "peer", "payload_hex", "monotonic"},
            "GAP fact schema",
        )
        _clock(event["monotonic"])
        _require(event["monotonic"] >= last, "GAP preceded readiness or rewound")
        last = event["monotonic"]
        payload = event["payload_hex"]
        _require(
            type(payload) is str
            and re.fullmatch(r"[0-9a-f]+", payload) is not None
            and len(payload) == length * 2,
            "malformed GAP payload",
        )
        raw = bytes.fromhex(payload)
        decoded = [raw[1:7][::-1].hex(), raw[0]]
        _require(
            raw[0] in (0, 1, 2, 3)
            and event["peer"] == decoded
            and (peer is None or peer == decoded),
            "foreign GAP peer",
        )
        peer = decoded
    boundaries = physical.get("boundaries")
    _require(
        type(boundaries) is list and all(type(b) is dict for b in boundaries),
        "physical boundaries required",
    )
    names = [b.get("boundary") for b in boundaries]
    _require(
        names
        == [
            "prepared",
            "on-window-ready",
            "gap-connected",
            "gap-disconnected",
            "physical-closed",
        ],
        "physical boundary order/cleanup disconnect cannot prove success",
    )
    previous = 0
    for boundary in boundaries:
        _clock(boundary.get("monotonic"))
        _require(boundary["monotonic"] >= previous, "physical boundary clock rewound")
        previous = boundary["monotonic"]
    _require(
        boundaries[1]["monotonic"] == readiness["monotonic"]
        and all(
            boundaries[i].get("hold_inactive") is True
            and type(boundaries[i].get("peer_count")) is int
            and boundaries[i]["peer_count"] == 0
            for i in (0, 1, 4)
        ),
        "physical no-peer/idle boundaries",
    )
    for boundary, event in zip(boundaries[2:4], events, strict=True):
        _require(
            all(boundary.get(k) == v for k, v in event.items()),
            "GAP boundary/event drift",
        )
    return copy.deepcopy(physical)
