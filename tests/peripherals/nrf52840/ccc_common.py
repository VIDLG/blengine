"""Bounded CCC topology, leased peer I/O, retirement and immutable-prefix transport.

No scenario transitions or runtime ID translation live here.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
from dataclasses import asdict, replace
from itertools import pairwise
from pathlib import Path
from typing import Any

from .btp_commands import BtpCommandError
from .cancellation_control import (
    load_control,
    read_document,
    require_multifunction,
    write_document,
)
from .fault_control import (
    FaultController,
    InfrastructureError,
    Operation,
    Outcome,
    Phase,
    RefCounters,
    Status,
    Winner,
    cleanup_ready,
    status_document,
)
from .fault_control import (
    StatusFlags as F,
)
from .fixture_session import read_live_document, verify_receipt
from .gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED
from .gatt_commands import (
    GattAttribute,
    GattCommands,
    tester_characteristic_native_permissions,
    uuid_to_le_bytes,
)
from .profile import ProfilePlan

PREFIX = "BLEHUB_NITRO_RUNTIME "
UPDATES_UUID = "fff20000-0000-4000-8000-000000000000"
READ_UUID = "fff10000-0000-4000-8000-000000000000"
MAX_RUNTIME_BYTES = 8 * 1024 * 1024
ARM_MS, HOLD_MS = 7000, 9000
REJECT_SECONDS, RETIRE_SECONDS, WHOLE_SECONDS = 7.0, 5.0, 180.0
REJECT_FLAGS = (
    F.REQUEST_CAPTURED
    | F.RESPONSE_QUEUED
    | F.TX_COMPLETED
    | F.REARMABLE
    | F.ANSWER_ORDERED
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"ccc mechanics: {message}")


RESOURCE_FIELDS = (
    "activeScans",
    "connections",
    "gattAttributes",
    "subscriptions",
    "pendingIo",
    "watchdogs",
    "callbackRoutes",
    "ingressRoots",
    "adapterDelegates",
    "peerCacheEntries",
    "backendLeases",
    "hubRoots",
    "handoffRoots",
    "handoffPending",
    "listeners",
    "workerThreads",
    "lifecycleOwners",
)


def resolve_target(
    gatt: GattCommands, handles: dict[str, int], service_uuid: str
) -> dict[str, Any]:
    """Resolve native Tester value handles without local declaration callbacks.

    GET_HANDLE_FROM_UUID searches attr->uuid, not service user_data. Its unique
    FFF1/FFF2 value matches anchor the GET_ATTRIBUTES service/descriptor intervals.
    The caller must first bind the service UUID/properties through native GATT
    discovery; neither this mechanism nor the doctor mapping reads that content.
    """
    attrs = gatt.get_attributes()
    uuid_handles = {}
    for role, target in (("read-write", READ_UUID), ("updates", UPDATES_UUID)):
        try:
            uuid_handles[target] = gatt.get_handle_from_uuid(target)
        except (BtpCommandError, TimeoutError) as error:
            raise ValueError(
                f"ccc mechanics: value UUID lookup uuid={target} "
                f"doctor_handle={handles.get(role)!r}: {error}"
            ) from error
    return _resolve_uuid_topology(attrs, uuid_handles, handles, service_uuid)


def _resolve_uuid_topology(
    attrs: tuple[GattAttribute, ...],
    uuid_handles: dict[str, int],
    handles: dict[str, int],
    service_uuid: str,
) -> dict[str, Any]:
    """Replay metadata observations; never synthesize declaration wire bytes.

    Tester alloc_characteristic appends the value immediately after its 2803
    entry. Check ordered entries, not numeric handle adjacency. The full inventory
    is retained so offline replay can reject duplicates outside the target service.
    """
    require(
        0 < len(attrs) <= 512
        and all(
            type(a.handle) is int
            and 0 < a.handle <= 65535
            and type(a.permissions) is int
            and 0 <= a.permissions <= 255
            and isinstance(a.uuid, str)
            for a in attrs
        )
        and all(a.handle < b.handle for a, b in pairwise(attrs)),
        "bounded ordered attribute inventory",
    )
    require(
        isinstance(uuid_handles, dict)
        and set(uuid_handles) == {READ_UUID, UPDATES_UUID},
        "exact value UUID lookups",
    )
    values = {}
    for role, target, permissions in (
        ("read-write", READ_UUID, 0x43),
        ("updates", UPDATES_UUID, 0x41),
    ):
        matches = [a for a in attrs if a.uuid == target]
        handle = uuid_handles[target]
        require(
            type(handle) is int
            and 0 < handle <= 65535
            and len(matches) == 1
            and matches[0].handle == handle == handles.get(role)
            and matches[0].permissions == permissions,
            f"value UUID/doctor/inventory mismatch uuid={target} "
            f"lookup_handle={handle!r} doctor_handle={handles.get(role)!r} "
            f"inventory_handles={[a.handle for a in matches]}",
        )
        values[role] = handle
    roots = [
        a for a in attrs if a.uuid in ("2800", "2801") and a.handle < values["updates"]
    ]
    require(
        bool(roots) and roots[-1].uuid == "2800" and roots[-1].permissions == 1,
        "target primary service boundary",
    )
    start = roots[-1].handle
    end = next(
        (a.handle for a in attrs if a.handle > start and a.uuid in ("2800", "2801")),
        65536,
    )
    require(
        start < values["read-write"] < end,
        "FFF1/FFF2 are in different service intervals",
    )
    scoped = [a for a in attrs if start <= a.handle < end]
    associations = {}
    associated_values = set()
    for index, attr in enumerate(scoped):
        if attr.uuid != "2803":
            continue
        require(
            attr.permissions == 1 and index + 1 < len(scoped),
            "declaration metadata/next entry",
        )
        value = scoped[index + 1]
        require(
            len(uuid_to_le_bytes(value.uuid)) == 16
            and value.handle not in associated_values,
            "Tester declaration must be followed by its unique value entry",
        )
        associations[str(attr.handle)] = value.handle
        associated_values.add(value.handle)
    require(
        values["updates"] in associated_values
        and values["read-write"] in associated_values,
        "target value lacks Tester declaration association",
    )
    value_handle = values["updates"]
    next_declaration = next(
        (a.handle for a in scoped if a.handle > value_handle and a.uuid == "2803"), end
    )
    descriptors = [
        a
        for a in scoped
        if value_handle < a.handle < next_declaration and a.uuid == "2902"
    ]
    require(
        len(descriptors) == 1 and descriptors[0].permissions == 3,
        "unique target CCC descriptor/permissions",
    )
    return {
        "topology_method": TOPOLOGY_METHOD,
        "attribute_inventory": [asdict(a) for a in attrs],
        "uuid_handles": dict(uuid_handles),
        "service_binding": {
            "source": "native-gatt-unique-value-uuid",
            "uuid": service_uuid,
            "handle": start,
        },
        "declaration_associations": associations,
        "topology": [asdict(a) for a in scoped],
        "target_handle": descriptors[0].handle,
        "value_handle": value_handle,
        "read_handle": values["read-write"],
        "target_uuid": UPDATES_UUID,
    }


def _resolve_topology(
    attrs: tuple[GattAttribute, ...],
    declarations: dict[str, bytes],
    handles: dict[str, int],
    service_uuid: str,
) -> dict[str, Any]:
    require(
        0 < len(attrs) <= 512
        and all(
            type(a.handle) is int
            and 0 < a.handle <= 65535
            and type(a.permissions) is int
            and 0 <= a.permissions <= 255
            for a in attrs
        )
        and all(a.handle < b.handle for a, b in pairwise(attrs)),
        "bounded ordered topology",
    )
    require(
        attrs[0].uuid == "2800"
        and declarations.get(str(attrs[0].handle)) == uuid_to_le_bytes(service_uuid)
        and not any(a.uuid in ("2800", "2801") for a in attrs[1:]),
        "exact primary service interval",
    )
    scoped = attrs
    end = 0x10000
    values: dict[str, int] = {}
    for attr in scoped:
        if attr.uuid != "2803":
            continue
        value = declarations.get(str(attr.handle), b"")
        require(len(value) in (5, 19), "characteristic declaration length")
        for role, target, properties, permissions in (
            ("updates", UPDATES_UUID, 0x32, 0x41),
            ("read-write", READ_UUID, 0x0E, 0x43),
        ):
            if value[3:] == uuid_to_le_bytes(target):
                handle = int.from_bytes(value[1:3], "little")
                require(
                    role not in values
                    and value[0] == properties
                    and handle > attr.handle
                    and handle
                    < next(
                        (
                            a.handle
                            for a in scoped
                            if a.handle > attr.handle and a.uuid == "2803"
                        ),
                        end,
                    ),
                    "characteristic declaration association",
                )
                matches = [
                    a
                    for a in scoped
                    if a.handle == handle
                    and a.uuid == target
                    and a.permissions == permissions
                ]
                require(
                    len(matches) == 1 and handles.get(role) == handle,
                    "exact value handle/UUID/permissions",
                )
                values[role] = handle
    require(set(values) == {"updates", "read-write"}, "missing target characteristic")
    value_handle = values["updates"]
    next_declaration = next(
        (a.handle for a in scoped if a.handle > value_handle and a.uuid == "2803"), end
    )
    ccc = [
        a
        for a in scoped
        if value_handle < a.handle < next_declaration and a.uuid == "2902"
    ]
    require(
        len(ccc) == 1 and ccc[0].permissions == 3,
        "unique target CCC descriptor/permissions",
    )
    return {
        "target_handle": ccc[0].handle,
        "value_handle": value_handle,
        "read_handle": values["read-write"],
        "target_uuid": UPDATES_UUID,
        "topology": [asdict(a) for a in scoped],
        "declarations": {
            str(a.handle): declarations[str(a.handle)].hex()
            for a in scoped
            if a.uuid in ("2800", "2801", "2803")
        },
    }


TOPOLOGY_METHOD = "tester-uuid-interval-v1"


class CccRelay:
    """Atomically publish App witnesses before requests; authorize each ACK once."""

    def __init__(self, root: Path, protocol: Any) -> None:
        self.protocol = protocol
        self.directory = self.root = root
        self.control = load_control(root / self.protocol.CONTROL_NAME)
        self.protocol.require(
            (self.control.flow, self.control.mode)
            == (self.protocol.FLOW, self.protocol.RUNTIME_MODE),
            "relay control",
        )
        self.sent: list[str] = []
        self.seen: list[str] = []
        self.prefix = ""

    def observe(self, text: str) -> None:
        self.protocol.require(
            load_control(self.root / self.protocol.CONTROL_NAME) == self.control
            and text.startswith(self.prefix),
            "control/runtime generation changed",
        )
        self.protocol._transcript(text, self.control)
        for index, phase in enumerate(self.protocol.PHASES):
            if PREFIX + self.control.marker("observed", phase) in text:
                self.protocol.require(
                    self.sent[: index + 1] == list(self.protocol.PHASES[: index + 1]),
                    "ACK before forwarding",
                )
        requests = [
            request
            for request in self.protocol.REQUESTS
            if PREFIX + self.control.marker("ready", request) in text
        ]
        self.protocol.require(
            len(requests) <= len(self.sent) + 1, "request before prior forwarding"
        )
        temporary = self.root / "runtime.log.tmp"
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(text)
        temporary.replace(self.root / "runtime.log")
        for request in requests[len(self.seen) :]:
            write_document(self.root / f"request-{request}.json", asdict(self.control))
        self.prefix, self.seen = text, requests

    def pending(self) -> str | None:
        self.protocol.require(
            load_control(self.root / self.protocol.CONTROL_NAME) == self.control,
            "control changed",
        )
        path = self.root / self.protocol.PHYSICAL_NAME
        document = read_live_document(path) if path.exists() else None
        if document is not None:
            self.protocol.require(
                document.get("status") in ("running", "passed")
                and not document.get("primary_error")
                and not document.get("cleanup_errors"),
                "provider failed",
            )
        if len(self.sent) == len(self.protocol.PHASES):
            return None
        index = len(self.sent)
        phase = self.protocol.PHASES[index]
        signal = self.root / f"signal-{phase}.json"
        if not signal.exists():
            return None
        self.protocol.require(
            len(self.seen) > index
            and read_document(signal) == asdict(self.control)
            and document is not None
            and document.get("status") == "running",
            "early/foreign signal",
        )
        self.protocol.validate_boundary(document, self.control, phase)
        row = self.protocol._one(document["boundaries"], phase)
        raw = self.prefix.encode("utf-8")
        self.protocol.require(
            len(raw) >= row["runtime_bytes"]
            and hashlib.sha256(raw[: row["runtime_bytes"]]).hexdigest()
            == row["runtime_sha256"],
            "signal runtime witness drift",
        )
        deadline = document.get("deadline")
        self.protocol.require(
            type(deadline) in (int, float)
            and math.isfinite(deadline)
            and time.monotonic() < deadline,
            "signal whole deadline",
        )
        if phase in ("armed", "held"):
            self.protocol.require(
                time.monotonic()
                < self.protocol._one(document["boundaries"], "armed")[
                    "reject_deadline"
                ],
                "stale ARM/HELD signal",
            )
        return phase

    def forwarded(self, phase: str) -> None:
        self.protocol.require(
            self.pending() == phase and phase not in self.sent,
            "duplicate/out-of-order forwarding",
        )
        self.sent.append(phase)


class CccOwner:
    """One bounded fixture lease; subclasses own scenario facts and transitions."""

    def __init__(
        self, fixture: Any, root: Path, handles: dict[str, int], protocol: Any
    ) -> None:
        self.protocol = protocol
        self.fixture, self.root, self.handles = fixture, root, handles
        self.plan = require_multifunction(fixture._fixture_session)
        self.control = self.protocol.validate_control(root, self.plan)
        self.document = self.protocol._base(root, self.plan)
        self.fault = FaultController(fixture._session, self.plan.session_id)
        self.ticket = self.peer = self.retained = None
        self.peers: set[bytes] = set()
        self.attached = self.detached = 0
        self.prefix = ""
        self.deadline = time.monotonic() + WHOLE_SECONDS
        self.reject_deadline = self.deadline
        self.database_epoch = 0
        self.document["deadline"] = self.deadline

    def publish(self) -> None:
        self.protocol.write_document(
            self.root / self.protocol.PHYSICAL_NAME, self.document
        )

    def record(self, name: str, **fields: Any) -> None:
        self.protocol.require(
            len(self.document["boundaries"]) < 128, "physical evidence overflow"
        )
        self.document["boundaries"].append(
            dict(boundary=name, monotonic=time.monotonic(), **fields)
        )
        self.publish()

    def guard(self, *, cleanup: bool = False) -> None:
        self.fixture._transport.require()
        self.protocol.require(self.fixture._mutation_allowed, "no mutation authority")
        verify_receipt(self.plan, fixture_identity=(os.getpid(), os.getpgid(0)))
        self.protocol.require(
            self.protocol.validate_control(self.root, self.plan) == self.control,
            "control changed",
        )
        if not cleanup:
            self.protocol.require(
                not self.fixture._stop_requested
                and not (self.root / "abort").exists()
                and time.monotonic() < self.deadline,
                "stopped/whole deadline",
            )

    def drain(self) -> None:
        for _ in range(128):
            event = (
                self.fixture._session.pop_event()
                or self.fixture._session.poll_event(0.001)
            )
            if event is None:
                return
            if event.service == 1 and event.opcode in (
                GAP_DEVICE_CONNECTED,
                GAP_DEVICE_DISCONNECTED,
            ):
                self.protocol.require(
                    event.index == 0
                    and len(event.payload)
                    == (13 if event.opcode == GAP_DEVICE_CONNECTED else 7)
                    and event.payload[0] in (0, 1, 2, 3),
                    "malformed GAP event",
                )
                peer = event.payload[:7]
                if event.opcode == GAP_DEVICE_CONNECTED:
                    self.protocol.require(
                        self.peer is None and not self.peers, "duplicate/foreign attach"
                    )
                    self.peer = peer
                    self.peers.add(peer)
                    self.attached += 1
                else:
                    self.protocol.require(
                        peer in self.peers and peer == self.peer,
                        "duplicate/foreign detach",
                    )
                    self.peers.remove(peer)
                    self.detached += 1
                self.fixture._observe_gap_event(event)
                self.record("gap", opcode=event.opcode, peer=peer.hex())
        raise ValueError(f"{self.protocol.SCENARIO}: event drain overflow")

    def pause(self, *, cleanup: bool = False) -> None:
        self.guard(cleanup=cleanup)
        time.sleep(0.02)

    def snapshot(self) -> tuple[str, dict[str, Any]]:
        path = self.root / "runtime.log"
        text = ""
        if path.exists():
            self.protocol.require(not path.is_symlink(), "linked runtime")
            with path.open("rb") as stream:
                raw = stream.read(MAX_RUNTIME_BYTES + 1)
            self.protocol.require(len(raw) <= MAX_RUNTIME_BYTES, "runtime file budget")
            text = raw.decode("utf-8")
        self.protocol.require(text.startswith(self.prefix), "runtime prefix changed")
        facts = self.protocol._transcript(text, self.control)
        self.prefix = text
        return text, facts

    def wait(self, request: str) -> dict[str, Any]:
        while True:
            self.guard()
            self.drain()
            _, facts = self.snapshot()
            path = self.root / f"request-{request}.json"
            if path.exists():
                _, facts = self.snapshot()
                self.protocol.require(
                    read_document(path) == asdict(self.control)
                    and self.protocol.READY_STEPS[self.protocol.REQUESTS.index(request)]
                    in facts["steps"]
                    and self.prefix.count(
                        PREFIX + self.control.marker("ready", request)
                    )
                    == 1,
                    "request lacks exact App witness",
                )
                return facts
            if request in self.protocol.HELD_REQUESTS:
                self.protocol.require(
                    time.monotonic() < self.reject_deadline, "REJECT deadline"
                )
            self.pause()

    def live(self):
        self.guard()
        limits = self.fault.get_limits()
        self.drain()
        self.protocol.require(
            self.peer is not None
            and self.peers == {self.peer}
            and limits.peer_present
            and limits.peer == self.peer
            and limits.boot_nonce == self.boot,
            "live peer/boot changed",
        )
        if self.ticket is not None:
            self.protocol.require(
                limits.connection_epoch == self.ticket.connection_epoch
                and limits.bearer_epoch == self.ticket.bearer_epoch
                and limits.database_epoch == self.database_epoch,
                "live epoch changed",
            )
        self.protocol.require(not limits.engine_flags, "live engine not idle")
        return limits

    def local(self, expected: bytes | None = None) -> dict[str, Any]:
        expected = self.protocol.BEFORE if expected is None else expected
        before = self.live()
        result = self.fixture._gatt.get_attribute_value(
            self.document["target_handle"],
            peer_address=self.peer[1:].hex(),
            peer_address_type=self.peer[0],
        )
        after = self.live()
        self.protocol.require(before == after, "live route changed across local read")
        self.protocol.require(
            result.att_response == 0 and result.value == expected,
            f"independent CCC readback handle=0x{self.document['target_handle']:04x} "
            f"att_response=0x{result.att_response:02x} value={result.value.hex()} "
            f"peer={self.peer.hex()}",
        )
        route = {
            "boot_nonce": before.boot_nonce.hex(),
            "peer": before.peer.hex(),
            "peer_present": before.peer_present,
            "connection_epoch": before.connection_epoch,
            "bearer_epoch": before.bearer_epoch,
            "database_epoch": before.database_epoch,
            "engine_flags": int(before.engine_flags),
        }
        return {
            "att_response": 0,
            "value": result.value.hex(),
            "peer": self.peer.hex(),
            "limits_before": route,
            "limits_after": dict(route),
        }

    def status(self) -> Status:
        self.guard()
        self.protocol.require(self.ticket is not None, "missing ticket")
        status = self.fault.status(self.ticket)
        self.guard()
        self.drain()
        self.protocol.require(
            status.ticket == self.ticket and status.peer == self.peer, "foreign status"
        )
        return status

    def retired(self, deadline: float) -> Status:
        while True:
            status = self.status()
            self.protocol.require(time.monotonic() < deadline, "retirement deadline")
            if status.phase is Phase.RETIRED:
                self.protocol.validate_status(
                    replace(status, refs=RefCounters(0, 0, 0, 0, 0, 0)),
                    Phase.RETIRED,
                    ticket=self.ticket,
                    peer=self.peer,
                )
                self.protocol.require(
                    not any(
                        asdict(status.refs)[k]
                        for k in (
                            "conn_refs",
                            "bearer_pins",
                            "response_buffers",
                            "resolver_work",
                            "tx_pending",
                        )
                    ),
                    "retired fault refs remain",
                )
                if status.refs.evidence_pending == 0:
                    self.protocol.require(
                        self.retained is None or status == self.retained,
                        "retained receipt drift",
                    )
                    return status
            else:
                self.protocol.require(
                    status.phase in (Phase.RESOLUTION_QUEUED, Phase.RETIRING)
                    and status.winner is Winner.REJECT
                    and status.outcome is Outcome.INJECTED_REJECT
                    and status.infrastructure_error is InfrastructureError.NONE
                    and not status.flags
                    & (
                        F.CALLBACK_INVOKED
                        | F.COMMIT_OBSERVED
                        | F.NORMAL_PATH_EVALUATED
                        | F.INFRASTRUCTURE_FAILED
                        | F.TX_FAILED
                    ),
                    "unexpected resolution outcome",
                )
                self.protocol.require(
                    status.phase is not Phase.RESOLUTION_QUEUED
                    or time.monotonic() < deadline - RETIRE_SECONDS,
                    "resolution deadline",
                )
            self.pause()

    def boundary(self, phase: str, status: Status, **fields: Any) -> None:
        self.record(
            phase,
            status=status_document(status),
            runtime_sha256=hashlib.sha256(self.prefix.encode()).hexdigest(),
            runtime_bytes=len(self.prefix.encode("utf-8")),
            **fields,
        )
        self.protocol.validate_boundary(self.document, self.control, phase)
        self.protocol.write_document(
            self.root / f"signal-{phase}.json", asdict(self.control)
        )

    def emit(self, value: bytes) -> dict[str, Any]:
        self.live()
        self.fixture._gatt.set_value(self.document["value_handle"], value)
        self.database_epoch += 1
        self.live()
        return {
            "handle": self.document["value_handle"],
            "value": value.hex(),
            "result": "accepted",
            "delivery": "not-proven",
        }

    def cleanup(self) -> None:
        self.guard(cleanup=True)
        # Inspect before consuming events or mutating: a stale ticket never owns a new peer.
        status = self.fault.status(self.ticket) if self.ticket is not None else None
        limits = self.fault.get_limits()
        if status is not None:
            self.protocol.require(
                status.ticket == self.ticket and status.peer == self.peer,
                "cleanup foreign status",
            )
        if limits.peer_present:
            self.protocol.require(
                self.peer is not None
                and self.peers == {self.peer}
                and limits.peer == self.peer
                and limits.boot_nonce == self.boot,
                "cleanup unowned peer",
            )
            if self.ticket is not None:
                self.protocol.require(
                    (limits.connection_epoch, limits.bearer_epoch)
                    == (self.ticket.connection_epoch, self.ticket.bearer_epoch),
                    "cleanup foreign epochs",
                )
        self.drain()
        if status is not None and status.phase in (Phase.ARMED, Phase.HELD):
            self.protocol.require(
                limits.peer_present
                and limits.database_epoch == self.ticket.database_epoch,
                "cleanup inactive/foreign owner",
            )
            self.fault.abort(self.ticket)
        for peer in tuple(self.peers):
            self.guard(cleanup=True)
            self.fixture._gap.disconnect(peer[1:][::-1].hex(), peer[0])
        deadline = time.monotonic() + RETIRE_SECONDS
        while True:
            self.guard(cleanup=True)
            self.drain()
            status = self.fault.status(self.ticket) if self.ticket is not None else None
            self.protocol.require(time.monotonic() < deadline, "cleanup drain deadline")
            if not self.peers and (status is None or cleanup_ready(status)):
                break
            self.pause(cleanup=True)
        self.protocol.require(
            not self.fault.require_idle().peer_present, "cleanup live peer"
        )
        self.fixture._gap.stop_advertising()
        self.record(
            "cleanup",
            peer_count=0,
            status=status_document(status) if status is not None else None,
        )

    def run(self) -> None:
        try:
            self.publish()
            self.execute()
            self.document["status"] = "passed"
            self.publish()
        except BaseException as error:
            self.document.update(
                status="failed", primary_error=f"{type(error).__name__}: {error}"
            )
            try:
                self.publish()
            except BaseException as publication:  # noqa: BLE001 - preserve the original failure
                error.add_note(f"failed evidence publication: {publication}")
            try:
                self.cleanup()
            except BaseException as cleanup:  # noqa: BLE001 - cleanup never replaces the primary
                self.document["cleanup_errors"].append(
                    f"{type(cleanup).__name__}: {cleanup}"
                )
                error.add_note(f"{self.protocol.SCENARIO} cleanup: {cleanup}")
            try:
                self.publish()
            except BaseException as publication:  # noqa: BLE001 - retain primary and cleanup errors
                error.add_note(f"final evidence publication: {publication}")
            raise


def validate_status(
    status: Status,
    phase: Phase,
    *,
    ticket=None,
    peer: bytes | None = None,
    before: bytes,
    requested: bytes,
) -> None:
    require(
        status.ticket.operation is Operation.CCC_WRITE
        and (ticket is None or status.ticket == ticket)
        and (peer is None or status.peer == peer),
        "status ticket/peer",
    )
    require(
        status.before == status.after == before
        and status.normal_att_result
        == status.callback_result
        == status.offset
        == status.derived_write_flags
        == 0
        and status.infrastructure_error is InfrastructureError.NONE,
        "callback/commit/local receipt drift",
    )
    require(status.phase is phase, "status phase")
    if phase is Phase.ARMED:
        require(
            status.flags == 0
            and status.winner is Winner.NONE
            and status.outcome is Outcome.PENDING
            and status.request_opcode == 0
            and not status.observed
            and status.remaining.arm_ms > 0,
            "ARMED receipt",
        )
    elif phase is Phase.HELD:
        require(
            status.flags == F.REQUEST_CAPTURED
            and status.winner is Winner.NONE
            and status.outcome is Outcome.PENDING
            and status.request_opcode == 0x12
            and status.observed == requested
            and status.remaining.hold_ms > 0,
            "HELD exact CCC request",
        )
    else:
        require(
            phase is Phase.RETIRED
            and status.flags == REJECT_FLAGS
            and status.winner is Winner.REJECT
            and status.outcome is Outcome.INJECTED_REJECT
            and status.request_opcode == 0x12
            and status.observed == requested
            and cleanup_ready(status),
            "REJECT retirement/zero",
        )


def _id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", value))
        and int(value) < 1 << 64
    )


def _same(actual: Any, expected: dict[str, Any], label: str) -> None:
    """Typed subset binding: bool must never impersonate an epoch or counter."""
    require(isinstance(actual, dict), f"{label} object")
    for key, value in expected.items():
        require(
            type(actual.get(key)) is type(value) and actual[key] == value,
            f"{label} {key}",
        )


def validate_native_target(
    facts: dict[str, Any], document: dict[str, Any], profile: ProfilePlan
) -> None:
    """Bind opaque native IDs and captured declarations to the frozen profile.

    validate_boundary already resolves the CCC interval and exact FFF1/FFF2
    declaration associations. Check profile membership of all captured values and
    the native target/descriptor projection here, without inventing ATT IDs.
    """
    discoveries = [
        event for event in facts["events"] if event["kind"] == "gattDiscovered"
    ]
    require(len(discoveries) == 1, "unique native discovery")
    services = discoveries[0].get("gattServices")
    require(
        isinstance(services, list)
        and len(services) == 1
        and isinstance(services[0], dict)
        and services[0].get("uuid") == profile.service_uuid,
        "native profile service",
    )
    chars = services[0].get("characteristics")
    require(
        isinstance(chars, list)
        and len(chars) <= 512
        and all(isinstance(c, dict) for c in chars),
        "native characteristic topology",
    )
    step = facts["steps"]["gatt"]
    by_uuid = {c.uuid: c for c in profile.characteristics}
    require(
        all(
            _id(c.get("id")) and isinstance(c.get("uuid"), str) and c["uuid"] in by_uuid
            for c in chars
        )
        and len({c["id"] for c in chars}) == len(chars)
        and len({c["uuid"] for c in chars}) == len(chars),
        "native profile instance identities",
    )
    metadata = document.get("topology_method") == TOPOLOGY_METHOD
    if metadata:
        require(services[0].get("primary") is True, "native primary service binding")
        for native in chars:
            definition = by_uuid[native["uuid"]]
            for flag, bit in (
                ("readable", 0x02),
                ("writableWithResponse", 0x08),
                ("writableWithoutResponse", 0x04),
                ("notifiable", 0x10),
                ("indicatable", 0x20),
            ):
                require(
                    native.get(flag) is bool(definition.properties & bit),
                    "native/profile properties for metadata topology",
                )
        binding = document["service_binding"]
        _same(
            binding,
            {"source": "native-gatt-unique-value-uuid", "uuid": services[0]["uuid"]},
            "native service binding",
        )
    for target, field, capabilities in (
        (
            UPDATES_UUID,
            "characteristicId",
            ("readable", "notifiable", "indicatable"),
        ),
        (
            READ_UUID,
            "readCharacteristicId",
            ("readable", "writableWithResponse"),
        ),
    ):
        matches = [c for c in chars if c.get("uuid") == target]
        require(
            len(matches) == 1
            and matches[0].get("id") == step[field]
            and all(matches[0].get(key) is True for key in capabilities),
            "native target/profile route",
        )
        definition = by_uuid[target]
        require(
            definition.initial_value == b"\x00"
            and definition.requires_ccc == (target == UPDATES_UUID),
            "profile target baseline/CCC",
        )
        if definition.requires_ccc:
            descriptors = matches[0].get("descriptors")
            require(
                isinstance(descriptors, list)
                and len(descriptors) <= 512
                and all(isinstance(d, dict) for d in descriptors),
                "native descriptor topology",
            )
            ccc = [
                d
                for d in descriptors
                if d.get("uuid") in ("2902", "00002902-0000-1000-8000-00805f9b34fb")
            ]
            require(
                len(ccc) == 1
                and _id(ccc[0].get("id"))
                and ccc[0]["id"]
                not in (step["characteristicId"], step["readCharacteristicId"]),
                "native target CCC descriptor",
            )
    attrs = document["topology"]
    require(all(isinstance(a["uuid"], str) for a in attrs), "physical UUID field types")
    declarations = (
        document["declaration_associations"] if metadata else document["declarations"]
    )
    require(
        set(declarations)
        == {
            str(a["handle"])
            for a in attrs
            if a["uuid"] in (("2803",) if metadata else ("2800", "2801", "2803"))
        },
        "declaration inventory",
    )
    claimed: set[int] = set()
    declared: set[str] = set()
    for attr in attrs:
        if attr["uuid"] != "2803":
            continue
        if metadata:
            value_handle = declarations[str(attr["handle"])]
            entries = [a for a in attrs if a["handle"] == value_handle]
            require(
                len(entries) == 1 and entries[0]["uuid"] in by_uuid,
                "metadata value/profile association",
            )
            definition = by_uuid[entries[0]["uuid"]]
        else:
            encoded = bytes.fromhex(declarations[str(attr["handle"])])
            definitions = [
                c
                for c in profile.characteristics
                if encoded[3:] == uuid_to_le_bytes(c.uuid)
            ]
            require(
                len(encoded) == 19 and len(definitions) == 1, "declaration/profile UUID"
            )
            definition = definitions[0]
            value_handle = int.from_bytes(encoded[1:3], "little")
            require(
                encoded[0] == definition.properties, "declaration/profile properties"
            )
        require(
            definition.uuid not in declared and value_handle not in claimed,
            "duplicate profile declaration/value",
        )
        declared.add(definition.uuid)
        claimed.add(value_handle)
        values = [a for a in attrs if a["handle"] == value_handle]
        require(
            len(values) == 1
            and values[0]["uuid"] == definition.uuid
            and values[0]["permissions"]
            == tester_characteristic_native_permissions(definition.permissions),
            "declaration/profile value permissions",
        )
        end = next(
            (
                a["handle"]
                for a in attrs
                if a["handle"] > attr["handle"] and a["uuid"] == "2803"
            ),
            65536,
        )
        require(attr["handle"] < value_handle < end, "profile declaration interval")
        descriptors = [
            a for a in attrs if value_handle < a["handle"] < end and a["uuid"] == "2902"
        ]
        require(
            len(descriptors) == int(definition.requires_ccc)
            and all(a["permissions"] == 3 for a in descriptors),
            "profile descriptor association",
        )
    require(
        {a["handle"] for a in attrs if a["uuid"] in by_uuid} == claimed,
        "unassociated profile value",
    )


def verify_prefixes(
    text: str, document: dict[str, Any], control, protocol
) -> list[dict[str, Any]]:
    """Replay each byte-exact prefix in its own ready-before-ACK interval."""
    raw = text.encode("utf-8")
    lines = text.splitlines(keepends=True)
    require(
        lines and text.endswith("\n") and "\r" not in text,
        "normalized LF transcript required",
    )
    ends: dict[str, int] = {}
    starts: dict[str, int] = {}
    offset = 0
    for line in lines:
        require(
            line.endswith("\n") and line.startswith(protocol.PREFIX),
            "non-normalized runtime line",
        )
        body = line[len(protocol.PREFIX) : -1]
        require(
            body == body.strip()
            and body.startswith(
                (protocol.RUNTIME_MODE + ":", "rf-control:", "rf-host:")
            ),
            "non-normalized runtime body",
        )
        if body.startswith("rf-control:"):
            require(body not in ends, "duplicate control offset")
            starts[body] = offset
            ends[body] = offset + len(line.encode("utf-8"))
        offset += len(line.encode("utf-8"))
    require(
        list(ends) == protocol.control_records(control), "complete original controls"
    )
    rows = {
        row["boundary"]: row
        for row in document["boundaries"]
        if row["boundary"] != "gap"
    }
    proofs = []
    previous = 0
    for index, phase in enumerate((*protocol.PHASES, "closed")):
        row = rows[phase]
        size = row["runtime_bytes"]
        require(
            previous < size <= len(raw) and raw[size - 1 : size] == b"\n",
            "prefix order/line boundary",
        )
        prefix_raw = raw[:size]
        require(
            hashlib.sha256(prefix_raw).hexdigest() == row["runtime_sha256"],
            "physical runtime prefix hash",
        )
        try:
            prefix = prefix_raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("ccc-enable-reject: prefix cuts UTF-8") from error
        facts = protocol._transcript(prefix, control)
        if phase == "closed":
            protocol.runtime_facts(prefix, run_id=control.session, complete=True)
            require(
                all(
                    line.startswith(protocol.PREFIX + "rf-host:")
                    for line in raw[size:].decode("utf-8").splitlines()
                ),
                "native records outside physical close prefix",
            )
        else:
            request = protocol.REQUESTS[index]
            ready = control.marker("ready", request)
            ack = control.marker("observed", phase)
            require(
                ends[ready] <= size <= starts[ack],
                "prefix outside original readiness/ACK interval",
            )
            require(
                protocol.READY_STEPS[index] in facts["steps"],
                "prefix missing App readiness witness",
            )
        proofs.append(
            {
                "boundary": phase,
                "runtime_bytes": size,
                "runtime_sha256": row["runtime_sha256"],
            }
        )
        previous = size
    return proofs
