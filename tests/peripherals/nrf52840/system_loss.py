"""Provider-only write/CCC system-loss coordination on an existing DirectFixture.

No CLI admission, radio authorization/control, serial owner, firmware changes or
recovery verdict. The host must separately authorize OFF/ON and prove same-owner
recovery. Native witnesses are cooperative observations, not RF attestation.

Integration: after DirectFixture.start(), prepare_control() with frozen native
identity/discovery and payload intent (baseline IDs only if already observed),
construct SystemLossCoordinator, arm() with the discovery/baseline prefix, then
observe() the cumulative actual dispatch/pending witness. observe() binds actual
head/sibling command IDs and the CCC subscription ID once in BINDING_NAME. OFF
requests must hash that binding and the complete cumulative witness. Calling
authorize_off() returns physical OFF readiness,
NOT permission to toggle a radio. poll_disconnect() saves the retained receipt
before returning disconnected-retired. Call finish_failure(primary) on host abort.
Do not concurrently consume the fixture's BTP session or GAP events.

A backend-dispatch-return record must come from an actual successful production
backend dispatch, not BLeng begin(). A separate native-io-lane record must observe
the head pending and accepted sibling queued. This module supplies no such native
instrumentation; absent that observation seam the host must leave this gate open.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cancellation_control import read_document, require_multifunction, write_document
from .ccc_common import READ_UUID, UPDATES_UUID, resolve_target
from .fault_control import (
    ArmCommon,
    ArmRequest,
    EngineFlags,
    FaultController,
    InfrastructureError,
    Operation,
    Outcome,
    Phase,
    Status,
    StatusFlags,
    Winner,
    cleanup_ready,
    owner_nonce,
    status_document,
)
from .fixture_session import FixtureSession, _read_bytes, verify_receipt
from .gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED

if TYPE_CHECKING:
    from .direct_fixture import DirectFixture

WRITE_CCC_ENABLE = "response-write-queued-ccc-enable"
CCC_DISABLE_WRITE = "ccc-disable-queued-response-write"
CONTROL_NAME = "system-loss-control.json"
PHYSICAL_NAME = "system-loss-physical.json"
RECEIPT_NAME = "system-loss-disconnect-receipt.json"
BINDING_NAME = "system-loss-binding.json"
# Leave margin before the existing 10s native watchdog; never extend on polling.
OFF_SECONDS = 8.0
RETIRE_SECONDS = 5.0
CCC_UUID = "00002902-0000-1000-8000-00805f9b34fb"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"system-loss: {message}")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def control_digest(control: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(control)).hexdigest()


def witness_digest(witness: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(witness)).hexdigest()


def binding_digest(binding: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(binding)).hexdigest()


def _id(value: Any) -> None:
    _require(
        type(value) is str
        and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None
        and int(value) <= (1 << 64) - 1,
        "noncanonical nonzero ID",
    )


def _uuid(value: Any) -> None:
    _require(type(value) is str and str(uuid.UUID(value)) == value, "noncanonical UUID")


def _hex(value: Any) -> bytes:
    _require(
        type(value) is str
        and re.fullmatch(r"(?:[0-9a-f]{2}){1,20}", value) is not None,
        "noncanonical bounded bytes",
    )
    return bytes.fromhex(value)


def _validate(control: dict[str, Any], plan: FixtureSession) -> None:
    _require(
        set(control)
        == {
            "schema_version",
            "session_id",
            "token",
            "case",
            "identity",
            "route",
            "baseline_subscription_id",
            "baseline_command_id",
            "write_before",
            "write_requested",
        },
        "control schema",
    )
    _require(
        type(control["schema_version"]) is int
        and control["schema_version"] == 1
        and control["session_id"] == plan.session_id,
        "control session/version",
    )
    _uuid(control["token"])
    _require(control["case"] in (WRITE_CCC_ENABLE, CCC_DISABLE_WRITE), "case")
    identity = control["identity"]
    _require(
        type(identity) is dict
        and set(identity)
        == {"pid", "boot_id", "owner_id", "backend_id", "initialize_command_id"},
        "identity schema",
    )
    _require(
        type(identity["pid"]) is int and 0 < identity["pid"] <= 2147483647, "native PID"
    )
    for name in ("boot_id", "owner_id", "backend_id"):
        _uuid(identity[name])
    _id(identity["initialize_command_id"])
    if control["case"] == CCC_DISABLE_WRITE:
        _id(control["baseline_command_id"])
        _id(control["baseline_subscription_id"])
        _require(
            control["baseline_command_id"] != identity["initialize_command_id"],
            "distinct baseline/init commands required",
        )
    else:
        _require(
            control["baseline_command_id"] is None
            and control["baseline_subscription_id"] is None,
            "unexpected baseline IDs",
        )
    route = control["route"]
    _require(
        type(route) is dict
        and set(route)
        == {
            "service_uuid",
            "connection_id",
            "gatt_session_id",
            "read_characteristic_id",
            "updates_characteristic_id",
            "ccc_descriptor_id",
            "read_uuid",
            "updates_uuid",
            "ccc_uuid",
            "read_properties",
            "updates_properties",
        },
        "route schema",
    )
    for name in (
        "connection_id",
        "gatt_session_id",
        "read_characteristic_id",
        "updates_characteristic_id",
        "ccc_descriptor_id",
    ):
        _id(route[name])
    _require(
        len(
            {
                route[name]
                for name in (
                    "read_characteristic_id",
                    "updates_characteristic_id",
                    "ccc_descriptor_id",
                )
            }
        )
        == 3,
        "distinct discovered attribute IDs",
    )
    _require(
        (
            route["service_uuid"],
            route["read_uuid"],
            route["updates_uuid"],
            route["ccc_uuid"],
        )
        == (plan.service_uuid, READ_UUID, UPDATES_UUID, CCC_UUID)
        and route["read_properties"] == ["read", "write-response", "write-command"]
        and route["updates_properties"] == ["read", "notify", "indicate"],
        "discovered target role/properties",
    )
    before, requested = _hex(control["write_before"]), _hex(control["write_requested"])
    _require(
        len(requested) <= len(before) and requested != before, "write before/requested"
    )


def prepare_control(
    root: Path,
    plan: FixtureSession,
    *,
    case: str,
    identity: dict[str, Any],
    route: dict[str, Any],
    baseline_command_id: str | None = None,
    baseline_subscription_id: str | None = None,
    write_before: bytes = b"\x00",
    write_requested: bytes = b"\x10",
) -> dict[str, Any]:
    """Freeze intent/known discovery only, never future dispatch IDs.

    The disable case requires already-observed baseline command/subscription IDs;
    arm() checks their native witness and the actual peer-specific enabled CCC.
    """
    require_multifunction(plan)
    _require(
        root.is_dir() and not root.is_symlink(),
        "private coordination directory required",
    )
    _require(
        not any(
            (root / name).exists() or (root / name).is_symlink()
            for name in (CONTROL_NAME, PHYSICAL_NAME, RECEIPT_NAME, BINDING_NAME)
        ),
        "stale system-loss output",
    )
    control = copy.deepcopy(
        {
            "schema_version": 1,
            "session_id": plan.session_id,
            "token": str(uuid.uuid4()),
            "case": case,
            "identity": identity,
            "route": route,
            "baseline_command_id": baseline_command_id,
            "baseline_subscription_id": baseline_subscription_id,
            "write_before": write_before.hex(),
            "write_requested": write_requested.hex(),
        }
    )
    _validate(control, plan)
    write_document(root / CONTROL_NAME, control)
    return control


class SystemLossCoordinator:
    """One attempt on the caller's leased fixture; terminal, non-retrying API.

    arm/observe/authorize_off/poll_disconnect return copied physical evidence.
    Failures preserve the original exception and attempt bounded owned cleanup.
    No state returned by this class is a same-Hub recovery or host-gate pass.
    """

    def __init__(
        self, fixture: DirectFixture, root: Path, handles: dict[str, int]
    ) -> None:
        self.fixture, self.root = fixture, root
        self.plan = require_multifunction(fixture._fixture_session)
        _require(
            root.is_dir() and not root.is_symlink(), "private coordination directory"
        )
        _require(
            not any(
                (root / name).exists() or (root / name).is_symlink()
                for name in (PHYSICAL_NAME, RECEIPT_NAME, BINDING_NAME)
            ),
            "stale physical output",
        )
        self.control = read_document(root / CONTROL_NAME)
        _validate(self.control, self.plan)
        self.frozen = {
            path: _read_bytes(path)
            for path in (
                root / CONTROL_NAME,
                self.plan.request_path,
                self.plan.receipt_path,
            )
        }
        self.process = (os.getpid(), os.getpgid(0))
        self.handles = (
            handles  # Recheck the live doctor mapping at arm, not a fabricated handle.
        )
        self.fault = FaultController(fixture._session, self.plan.session_id)
        self.ticket = self.request = self.peer = None
        self.peers: set[bytes] = set()
        self.detached = False
        self.prefix: list[dict[str, Any]] = []
        self.binding: dict[str, Any] | None = None
        self._completed = False

        self.document: dict[str, Any] = {
            "schema_version": 1,
            "scope": "system-loss-provider-only",
            "control_sha256": control_digest(self.control),
            "session_id": self.plan.session_id,
            "state": "prepared",
            "recovery": "not-evaluated",
            "radio_authorized": False,
            "native_witness_scope": "host-supplied; native observation seam required; not independent attestation",
            "boundaries": [],
            "primary_error": None,
            "cleanup_errors": [],
            "binding_sha256": None,
        }
        self._guard()
        self._publish()

    def _guard(self, *, cleanup: bool = False) -> None:
        self.fixture._transport.require()
        _require(
            (os.getpid(), os.getpgid(0)) == self.process
            and self.fixture._mutation_allowed,
            "provider ownership",
        )
        _require(
            all(_read_bytes(path) == raw for path, raw in self.frozen.items()),
            "immutable control/request/receipt changed",
        )
        verify_receipt(self.plan, fixture_identity=self.process)
        if not cleanup:
            _require(
                not self.fixture._stop_requested and not (self.root / "abort").exists(),
                "owner aborted",
            )
            _require(
                self.document["state"] not in ("failed", "disconnected-retired"),
                "terminal state",
            )

    def _publish(self) -> None:
        write_document(self.root / PHYSICAL_NAME, self.document)

    def _record(self, boundary: str, **facts: Any) -> dict[str, Any]:
        _require(len(self.document["boundaries"]) < 64, "evidence bound")
        self.document["boundaries"].append(
            dict(boundary=boundary, monotonic=time.monotonic(), **facts)
        )
        self._publish()
        return copy.deepcopy(self.document)

    @contextmanager
    def _step(self) -> Iterator[None]:
        entered_completed = self._completed
        try:
            self._guard()
            yield
        except BaseException as error:
            if not entered_completed:
                self.finish_failure(error)
            raise

    def _drain(self, *, cleanup: bool = False) -> None:
        for _ in range(128):
            event = (
                self.fixture._session.pop_event()
                or self.fixture._session.poll_event(0.001)
            )
            if event is None:
                return
            if event.service != 1 or event.opcode not in (
                GAP_DEVICE_CONNECTED,
                GAP_DEVICE_DISCONNECTED,
            ):
                continue
            _require(
                event.index == 0
                and type(event.payload) is bytes
                and len(event.payload)
                == (13 if event.opcode == GAP_DEVICE_CONNECTED else 7)
                and event.payload[0] in range(4),
                "malformed GAP event",
            )
            peer = event.payload[:7]
            if event.opcode == GAP_DEVICE_CONNECTED:
                _require(
                    self.peer is None and not self.peers and not self.detached,
                    "foreign/duplicate GAP attach",
                )
                self.peer = peer
                self.peers.add(peer)
            else:
                _require(
                    peer == self.peer and self.peers == {peer},
                    "foreign/duplicate GAP detach",
                )
                self.peers.remove(peer)
                self.detached = True
            self.fixture._observe_gap_event(event)
            self._record("gap", opcode=event.opcode, peer=peer.hex())
            _require(
                not self.detached or cleanup or self.document["state"] == "off-ready",
                "early disconnect",
            )
        raise ValueError("system-loss: GAP backlog bound")

    def _live(self, limits: Any) -> None:
        _require(
            self.peer is not None
            and self.peers == {self.peer}
            and not self.detached
            and self.fixture._connected_peers
            == {(self.peer[1:][::-1].hex(), self.peer[0])}
            and limits.peer_present
            and limits.peer == self.peer,
            "live peer changed",
        )
        if self.ticket is not None:
            _require(
                (
                    limits.boot_nonce,
                    limits.connection_epoch,
                    limits.bearer_epoch,
                    limits.database_epoch,
                )
                == (
                    self.ticket.boot_nonce,
                    self.ticket.connection_epoch,
                    self.ticket.bearer_epoch,
                    self.ticket.database_epoch,
                ),
                "live epochs changed",
            )

    def _witness(
        self, witness: dict[str, Any], *, pending: bool
    ) -> dict[str, Any] | None:
        c, route = self.control, self.control["route"]
        _require(
            type(witness) is dict
            and set(witness)
            == {"schema_version", "control_sha256", "identity", "events"}
            and type(witness["schema_version"]) is int
            and witness["schema_version"] == 1
            and witness["control_sha256"] == control_digest(c)
            and _canonical(witness["identity"]) == _canonical(c["identity"]),
            "native identity/session witness",
        )
        events = witness["events"]
        _require(type(events) is list and len(events) <= 6, "witness bound")
        expected = [
            {"kind": "discovered", "source": "native-gatt-discovery", "route": route}
        ]
        candidate = None
        if c["case"] == CCC_DISABLE_WRITE:
            expected.extend(
                [
                    {
                        "kind": "baseline-enabled",
                        "source": "native-domain-event",
                        "command_id": c["baseline_command_id"],
                        "subscription_id": c["baseline_subscription_id"],
                    },
                    {
                        "kind": "baseline-value",
                        "source": "native-domain-event",
                        "subscription_id": c["baseline_subscription_id"],
                        "value_hex": "ef",
                    },
                ]
            )
        if pending:
            _require(
                len(events) == len(expected) + 3
                and all(type(row) is dict for row in events),
                "dispatch/pending witness length/schema",
            )
            head, sibling = events[len(expected) : len(expected) + 2]
            write_first = c["case"] == WRITE_CCC_ENABLE
            head_id, sibling_id = head.get("command_id"), sibling.get("command_id")
            subscription_id = (sibling if write_first else head).get("subscription_id")
            for value in (head_id, sibling_id, subscription_id):
                _id(value)
            commands = [c["identity"]["initialize_command_id"], head_id, sibling_id]
            if not write_first:
                commands.append(c["baseline_command_id"])
                _require(
                    subscription_id == c["baseline_subscription_id"],
                    "baseline subscription drift",
                )
            _require(
                len(set(commands)) == len(commands),
                "distinct actual dispatch IDs required",
            )
            candidate = {
                "schema_version": 1,
                "control_sha256": control_digest(c),
                "head_command_id": head_id,
                "sibling_command_id": sibling_id,
                "subscription_id": subscription_id,
                "witness_sha256": witness_digest(witness),
            }
            for role, operation, characteristic in (
                (
                    "head",
                    "response-write" if write_first else "ccc-disable",
                    route["read_characteristic_id"]
                    if write_first
                    else route["updates_characteristic_id"],
                ),
                (
                    "sibling",
                    "ccc-enable" if write_first else "response-write",
                    route["updates_characteristic_id"]
                    if write_first
                    else route["read_characteristic_id"],
                ),
            ):
                expected.append(
                    {
                        "kind": "dispatch-accepted",
                        "source": "backend-dispatch-return",
                        "role": role,
                        "command_id": candidate[role + "_command_id"],
                        "operation": operation,
                        "characteristic_id": characteristic,
                        "accepted": True,
                        "connection_id": route["connection_id"],
                        "gatt_session_id": route["gatt_session_id"],
                        "subscription_id": subscription_id
                        if operation.startswith("ccc-")
                        else None,
                        "descriptor_id": route["ccc_descriptor_id"]
                        if operation.startswith("ccc-")
                        else None,
                        "before_hex": "0100"
                        if operation == "ccc-disable"
                        else "0000"
                        if operation == "ccc-enable"
                        else c["write_before"],
                        "requested_hex": "0000"
                        if operation == "ccc-disable"
                        else "0100"
                        if operation == "ccc-enable"
                        else c["write_requested"],
                    }
                )
            expected.append(
                {
                    "kind": "queue-pending",
                    "source": "native-io-lane",
                    "head_command_id": head_id,
                    "sibling_command_id": sibling_id,
                    "subscription_id": subscription_id,
                    "head_state": "pending",
                    "sibling_state": "queued",
                }
            )
        _require(
            _canonical(events[: len(self.prefix)]) == _canonical(self.prefix)
            and len(events) >= len(self.prefix),
            "cumulative immutable witness changed",
        )
        _require(
            _canonical(events) == _canonical(expected),
            "discovery/baseline/dispatch/pending witness mismatch",
        )
        if self.binding is not None:
            _require(
                _canonical(candidate) == _canonical(self.binding),
                "immutable dispatch binding changed",
            )
        self.prefix = copy.deepcopy(events)
        return candidate

    def _bind(self, candidate: dict[str, Any], witness: dict[str, Any]) -> None:
        """Persist once, only after exact backend/lane facts and a live HELD sample."""
        if self.binding is not None:
            _require(
                _canonical(candidate) == _canonical(self.binding),
                "immutable dispatch binding changed",
            )
            return
        path = self.root / BINDING_NAME
        _require(not path.exists() and not path.is_symlink(), "stale dispatch binding")
        write_document(path, candidate)
        self.frozen[path] = _read_bytes(path)
        self.binding = copy.deepcopy(candidate)
        self.document["binding"] = copy.deepcopy(candidate)
        self.document["binding_sha256"] = binding_digest(candidate)
        self._record(
            "dispatch-bound",
            binding=copy.deepcopy(candidate),
            witness=copy.deepcopy(witness),
        )

    def arm(self, witness: dict[str, Any]) -> dict[str, Any]:
        """Bind exact target/current before-state, then ARM once; no prelaunch ARM."""
        with self._step():
            _require(self.document["state"] == "prepared", "arm state")
            self._witness(witness, pending=False)
            self._drain()
            limits = self.fault.require_idle()
            self._live(limits)
            topology = resolve_target(
                self.fixture._gatt, self.handles, self.plan.service_uuid
            )
            self.document["target"] = topology
            write_first = self.control["case"] == WRITE_CCC_ENABLE
            target = (
                topology["read_handle"] if write_first else topology["target_handle"]
            )
            before = _hex(self.control["write_before"]) if write_first else b"\x01\x00"
            requested = (
                _hex(self.control["write_requested"]) if write_first else b"\x00\x00"
            )
            # Also prove the sibling's initial CCC / write value; never local SET_VALUE.
            for handle, expected in (
                (topology["read_handle"], _hex(self.control["write_before"])),
                (
                    topology["target_handle"],
                    b"\x00\x00" if write_first else b"\x01\x00",
                ),
            ):
                value = self.fixture._gatt.get_attribute_value(
                    handle,
                    peer_address=self.peer[1:].hex(),
                    peer_address_type=self.peer[0],
                )
                _require(
                    value.att_response == 0 and value.value == expected,
                    "target before-state",
                )
            fresh = self.fault.require_idle()
            self._drain()
            self._guard()
            self._live(fresh)
            _require(fresh == limits, "pre-arm epoch/limits changed")
            common = ArmCommon(
                fresh.boot_nonce,
                owner_nonce(self.plan.session_id),
                fresh.connection_epoch,
                fresh.bearer_epoch,
                fresh.database_epoch,
                self.peer,
                target,
                10000,
                20000,
            )
            self.request = ArmRequest(
                Operation.RESPONSE_WRITE if write_first else Operation.CCC_WRITE,
                common,
                before,
                requested,
            )
            self.document["off_deadline"] = time.monotonic() + OFF_SECONDS
            self.ticket = self.fault.arm(self.request).ticket
            snapshot = self._status()
            _require(
                not snapshot.observed
                and snapshot.phase is Phase.ARMED
                and snapshot.winner is Winner.NONE
                and snapshot.outcome is Outcome.PENDING
                and snapshot.flags == 0
                and snapshot.remaining.arm_ms > 0,
                "ARMED receipt",
            )
            self.document["state"] = "armed"
            return self._record(
                "armed",
                request_wire=self.request.encode().hex(),
                status=status_document(snapshot),
                witness=copy.deepcopy(witness),
            )

    def _status(self) -> Status:
        self._guard(cleanup=True)
        snapshot = self.fault.status(self.ticket)
        self._guard(cleanup=True)
        _require(
            snapshot.peer == self.peer
            and snapshot.before == self.request.before
            and snapshot.after == self.request.before
            and snapshot.normal_att_result == snapshot.callback_result == 0
            and snapshot.infrastructure_error is InfrastructureError.NONE
            and not snapshot.flags
            & (
                StatusFlags.CALLBACK_INVOKED
                | StatusFlags.COMMIT_OBSERVED
                | StatusFlags.NORMAL_PATH_EVALUATED
                | StatusFlags.TX_FAILED
                | StatusFlags.INFRASTRUCTURE_FAILED
            ),
            "receipt target/peer/before/effects",
        )
        return snapshot

    def _held(self) -> Status:
        _require(time.monotonic() < self.document["off_deadline"], "OFF deadline")
        snapshot = self._status()
        limits = self.fault.get_limits()
        self._drain()
        self._guard()
        self._live(limits)
        _require(
            limits.engine_flags == EngineFlags.BUSY, "HELD engine idle/quarantined"
        )
        _require(
            snapshot.phase is Phase.HELD
            and snapshot.winner is Winner.NONE
            and snapshot.outcome is Outcome.PENDING
            and snapshot.flags == StatusFlags.REQUEST_CAPTURED
            and snapshot.observed == self.request.requested
            and snapshot.remaining.hold_ms > 0
            and time.monotonic() < self.document["off_deadline"],
            "fresh HELD receipt/deadline",
        )
        return snapshot

    def observe(self, witness: dict[str, Any]) -> dict[str, Any]:
        """Accept the full cumulative backend/lane witness, not a begin() receipt."""
        with self._step():
            _require(
                self.document["state"] in ("armed", "held-pending"), "observe state"
            )
            candidate = self._witness(witness, pending=True)
            status = self._held()
            self._bind(candidate, witness)
            self.document["state"] = "held-pending"
            if not any(
                row["boundary"] == "held-pending" for row in self.document["boundaries"]
            ):
                return self._record(
                    "held-pending",
                    status=status_document(status),
                    witness=copy.deepcopy(witness),
                )
            return copy.deepcopy(self.document)

    def authorize_off(
        self, request: dict[str, Any], witness: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate provider OFF readiness; NEVER authorize or perform a radio action.

        observe() must first persist the actual dispatch binding. The host's own
        radio authorization and native recovery audit remain separate gates.
        """
        with self._step():
            _require(self.document["state"] in ("armed", "held-pending"), "OFF state")
            self._witness(witness, pending=True)
            _require(
                self.binding is not None,
                "OFF requires bound pending evidence from observe",
            )
            expected = {
                "schema_version": 1,
                "control_sha256": control_digest(self.control),
                "binding_sha256": self.document["binding_sha256"],
                "witness_sha256": witness_digest(witness),
            }
            _require(
                _canonical(request) == _canonical(expected), "stale/foreign OFF request"
            )
            status = self._held()  # Never authorize from observe()'s earlier HELD.
            self.document["state"] = "off-ready"
            self.document["retire_deadline"] = min(
                self.document["off_deadline"], time.monotonic() + RETIRE_SECONDS
            )
            return self._record(
                "off-ready",
                status=status_document(status),
                request=copy.deepcopy(request),
                witness=copy.deepcopy(witness),
            )

    def poll_disconnect(self, witness: dict[str, Any]) -> dict[str, Any]:
        """One bounded sample; only actual GAP detach plus clean receipt advances."""
        with self._step():
            _require(self.document["state"] == "off-ready", "disconnect state")
            self._witness(witness, pending=True)
            _require(
                time.monotonic() < self.document["retire_deadline"],
                "disconnect retirement deadline",
            )
            snapshot = self._status()
            self._drain()
            _require(
                snapshot.winner in (Winner.NONE, Winner.DISCONNECT),
                "loss winner is not disconnect",
            )
            _require(
                snapshot.observed == self.request.requested
                and snapshot.flags & StatusFlags.REQUEST_CAPTURED,
                "disconnect captured request",
            )
            if not self.detached or not cleanup_ready(snapshot):
                return copy.deepcopy(self.document)
            limits = self.fault.require_idle()
            self._drain()
            self._guard()
            _require(
                not self.peers
                and not self.fixture._connected_peers
                and not limits.peer_present
                and limits.boot_nonce == self.ticket.boot_nonce
                and snapshot.winner is Winner.DISCONNECT
                and snapshot.outcome is Outcome.DISCONNECTED_UNCOMMITTED
                and snapshot.flags
                == StatusFlags.REQUEST_CAPTURED | StatusFlags.REARMABLE
                and time.monotonic() < self.document["retire_deadline"],
                "disconnect receipt/live idle/deadline",
            )
            retained = {
                "schema_version": 1,
                "control_sha256": control_digest(self.control),
                # The coordinator writes this only after checking the exact
                # disconnect winner/outcome; the host may project it without
                # decoding the opaque status wire.
                "outcome": "disconnected-uncommitted",
                "status": status_document(snapshot),
                "binding_sha256": self.document["binding_sha256"],
                "binding": copy.deepcopy(self.binding),
                "witness": copy.deepcopy(witness),
            }
            _require(
                not (self.root / RECEIPT_NAME).exists(),
                "retained receipt already exists",
            )
            write_document(self.root / RECEIPT_NAME, retained)
            self.document["state"] = "disconnected-retired"
            result = self._record(
                "disconnected-retired",
                status=status_document(snapshot),
                receipt_sha256=hashlib.sha256(
                    _read_bytes(self.root / RECEIPT_NAME)
                ).hexdigest(),
            )
            self._completed = True
            return result

    def finish_failure(self, primary: BaseException) -> dict[str, Any]:
        """Keep primary and clean only this live owner; completed evidence is final.

        A later caller failure belongs to the host audit, not a rewrite of this
        already-persisted physical result. Completion commits after _record(), so
        failure publishing that final record still follows the normal error path.
        """
        if self._completed or self.document["state"] == "failed":
            return copy.deepcopy(self.document)
        self.document["primary_error"] = str(primary)
        self.document["state"] = "failed"
        try:
            self._guard(cleanup=True)
            if self.ticket is not None:
                deadline = time.monotonic() + 7
                status = self._status()
                limits = self.fault.get_limits()
                self._drain(cleanup=True)
                _require(
                    not limits.engine_flags & EngineFlags.QUARANTINED,
                    "quarantined cleanup owner",
                )
                if status.phase in (Phase.ARMED, Phase.HELD) and limits.peer_present:
                    self._live(limits)
                    self.fault.abort(self.ticket)
                while True:
                    status = self._status()
                    self._drain(cleanup=True)
                    _require(time.monotonic() < deadline, "cleanup retirement deadline")
                    if cleanup_ready(status):
                        self.document["cleanup_status"] = status_document(status)
                        break
                    time.sleep(0.02)
        # Cleanup interruption must retain the caller's primary failure.
        except BaseException as error:  # noqa: BLE001
            self.document["cleanup_errors"].append(str(error))
            primary.add_note(f"system-loss cleanup: {error}")
        try:
            self._publish()
        # Evidence interruption must not replace the primary failure.
        except BaseException as error:  # noqa: BLE001
            self.document["cleanup_errors"].append(f"publication: {error}")
            primary.add_note(f"system-loss failure publication: {error}")
        return copy.deepcopy(self.document)
