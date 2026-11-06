"""In-page watchdog adapter: reuse physical execution, never launch or reprovision."""

from __future__ import annotations

import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tools.react_native import watchdog_ledger
from tools.react_native.run_rf_watchdog import Experiment, WatchdogError

from .cancellation_control import (
    Control,
    load_suite_controls,
    read_document,
    require_multifunction,
    write_document,
)
from .fault_control import (
    FaultController,
    Operation,
    Outcome,
    Phase,
    Winner,
    cleanup_ready,
    decode_status,
    encode_status,
    owner_nonce,
    ticket_document,
    ticket_from_document,
)
from .fixture_session import verify_receipt
from .gap_commands import GAP, GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED
from .gatt_commands import tester_characteristic_native_permissions


class ContinuousExperiment(Experiment):
    """Fail closed if cleanup encounters another hold generation."""

    fixture_peers: set[tuple[str, int]] | None = None

    def record(self, boundary: str, **fields: Any) -> None:
        if (
            boundary == "gap"
            and getattr(self, "_cleanup_only", False)
            and fields["opcode"] == GAP_DEVICE_CONNECTED
        ):
            raise WatchdogError("refusing a new GAP connection during watchdog cleanup")
        if boundary == "gap" and self.fixture_peers is not None:
            expected = {(p[1:7][::-1].hex(), p[0]) for p in self.peers}
            if self.fixture_peers != expected:
                raise WatchdogError("watchdog/fixture exact GAP tracker drift")
            raw = bytes.fromhex(fields["peer"])
            peer = (raw[1:7][::-1].hex(), raw[0])
            if fields["opcode"] == GAP_DEVICE_CONNECTED:
                if self.fixture_peers:
                    raise WatchdogError("foreign or duplicate watchdog GAP peer")
                self.fixture_peers.add(peer)
            else:
                if self.fixture_peers != {peer}:
                    raise WatchdogError("foreign watchdog GAP disconnect")
                self.fixture_peers.remove(peer)
        super().record(boundary, **fields)

    def cleanup(self) -> None:
        if not self.mutation_allowed:
            return
        cleanup: dict[str, Any] = {
            "hold_inactive": False,
            "advertising_stopped": False,
            "peer_state": "unknown",
            "radio_power": "unchanged",
        }
        self.report["physical_cleanup"] = cleanup
        self._cleanup_only = True
        errors: list[str] = []
        deadline = time.monotonic() + 7
        try:
            # Ownership/settlement is a hard gate before any GAP mutation.
            try:
                self.guard()
                self.settle_owned_fault(deadline)
                self.require_owned_cleanup_connection()
            except Exception as error:
                errors.append(str(error))
                raise
            cleanup["hold_inactive"] = False
            for operation in (
                self.stop_advertising_for_cleanup,
                self.disconnect_known_peers,
            ):
                try:
                    self.guard()
                    operation()
                except Exception as error:  # noqa: BLE001 - remaining owned safety cleanup
                    errors.append(str(error))
            if errors:
                raise WatchdogError("physical cleanup failed: " + "; ".join(errors))
            try:
                # GAP stop/disconnect may enqueue new evidence after the first
                # zero sample. Final success needs a fresh retained receipt.
                if self.ticket is not None:
                    while True:
                        snapshot = self.fault.status(self.ticket)
                        if time.monotonic() >= deadline:
                            raise WatchdogError("watchdog final resource-zero deadline")
                        if cleanup_ready(snapshot):
                            break
                        self.pause()
                    cleanup["status_wire"] = encode_status(snapshot).hex()
                limits = self.fault.require_idle()
                self.events()
                if (
                    time.monotonic() >= deadline
                    or limits.peer_present
                    or self.peers
                    or (self.fixture_peers is not None and self.fixture_peers)
                    or (
                        self.ticket is not None
                        and limits.boot_nonce != self.ticket.boot_nonce
                    )
                ):
                    raise WatchdogError(
                        "watchdog final cleanup boot/peer/deadline drift"
                    )
                cleanup["hold_inactive"] = True
            except Exception as error:
                errors.append(str(error))
                raise
        finally:
            cleanup["known_peers_remaining"] = [
                peer.hex() for peer in sorted(self.peers)
            ]
            self.report["physical_cleanup_errors"] = errors

    def settle_owned_fault(self, deadline: float) -> None:
        if self.fault is None:
            raise WatchdogError("missing watchdog v2 owner")
        if self.ticket is None:
            # Before ARM there is no authority to adopt a queued/foreign peer.
            limits = self.fault.require_idle()
            if limits.peer_present or time.monotonic() >= deadline:
                raise WatchdogError("refusing cleanup of a foreign watchdog owner")
            self.report["physical_cleanup"]["hold_inactive"] = True
            return
        # Check the exact receipt BEFORE polling GAP or performing any mutation.
        # A stale receipt must not authorize cleanup of a newer connection.
        snapshot = self.fault.status(self.ticket)
        if time.monotonic() >= deadline:
            raise WatchdogError("watchdog cleanup STATUS exceeded deadline")
        if snapshot.phase in (Phase.ARMED, Phase.HELD):
            limits = self.fault.get_limits()
            if time.monotonic() >= deadline:
                raise WatchdogError("watchdog cleanup limits exceeded deadline")
            if limits.peer_present:
                if (
                    limits.boot_nonce != self.ticket.boot_nonce
                    or limits.peer != snapshot.peer
                    or (
                        limits.connection_epoch,
                        limits.bearer_epoch,
                        limits.database_epoch,
                    )
                    != (
                        self.ticket.connection_epoch,
                        self.ticket.bearer_epoch,
                        self.ticket.database_epoch,
                    )
                ):
                    raise WatchdogError("foreign watchdog connection before cleanup")
                self.fault.abort(self.ticket)
                self.record("cleanup-abort-dispatched", **ticket_document(self.ticket))
            # No attached peer means mutations are stale. Only retained STATUS
            # can prove that its asynchronous detach/retirement has settled.
        while True:
            snapshot = self.fault.status(self.ticket)
            if time.monotonic() >= deadline:
                raise WatchdogError("watchdog cleanup retirement deadline")
            if cleanup_ready(snapshot):
                self.report["physical_cleanup"].update(
                    hold_inactive=True, status_wire=encode_status(snapshot).hex()
                )
                return
            self.pause()

    def require_owned_cleanup_connection(self) -> None:
        limits = self.fault.require_idle()
        if limits.peer_present and (
            self.ticket is None
            or (
                limits.boot_nonce,
                limits.connection_epoch,
                limits.bearer_epoch,
                limits.database_epoch,
            )
            != (
                self.ticket.boot_nonce,
                self.ticket.connection_epoch,
                self.ticket.bearer_epoch,
                self.ticket.database_epoch,
            )
            or self.peers != {limits.peer}
        ):
            raise WatchdogError("refusing cleanup of a foreign watchdog connection")

    def disconnect_known_peers(self) -> None:
        deadline = time.monotonic() + 3
        self.require_owned_cleanup_connection()
        super().disconnect_known_peers()
        if time.monotonic() >= deadline:
            raise WatchdogError("watchdog cleanup GAP observation exceeded deadline")


class ContinuousRuntime:
    """Read the existing pinned host capture; no launch/serial/lease ownership."""

    def __init__(
        self, root: Path, control: Control, guard: Any, report: dict[str, Any]
    ) -> None:
        self.root, self.control, self.guard, self.report = root, control, guard, report
        self.directory = root / control.directory
        self.directory.mkdir(exist_ok=True)

    def snapshot(self, *, allow_exit: bool = False) -> str:
        self.guard()
        path = self.root / "runtime.log"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if "rf-watchdog:failed:" in text or "rf-lab-suite:flow-failed:" in text:
            raise WatchdogError("continuous runtime reported failure")
        start = (
            "BLEHUB_NITRO_RUNTIME rf-lab-suite:flow-started:index=9:mode=rf-watchdog\n"
        )
        if "rf-lab-suite:" in text:
            if text.count(start) != 1:
                raise WatchdogError("continuous watchdog lacks its exact flow scope")
            text = text.split(start, 1)[1]
        watchdog_ledger.verify_control(text, self.control)
        return text

    def request(self, phase: str, deadline: float) -> None:
        path = self.directory / f"request-{phase}.json"
        while True:
            self.guard()
            if time.monotonic() >= deadline:
                raise TimeoutError(f"continuous watchdog request deadline: {phase}")
            if path.exists():
                if read_document(path) != asdict(self.control):
                    raise WatchdogError(
                        "continuous watchdog request token/generation drift"
                    )
                if (
                    phase == "recover"
                    and watchdog_ledger.SILENCE
                    not in watchdog_ledger.markers(self.snapshot())
                ):
                    raise WatchdogError("recovery requires native old-route silence")
                return
            time.sleep(0.05)

    def wait_control_request(self, phase: str, deadline: float) -> None:
        self.request(phase, deadline)
        text = self.snapshot()
        if text.count(self.control.marker("ready", phase)) != 1:
            raise WatchdogError(
                "continuous request lacks the exact native control marker"
            )

    def signal(self, phase: str) -> None:
        self.guard()
        write_document(self.directory / "physical-evidence.json", self.report)
        write_document(self.directory / f"signal-{phase}.json", asdict(self.control))


def _require_no_peer(fixture: Any, deadline: float) -> None:
    """Reach a bounded live idle boundary without taking ownership of a new peer."""
    for _ in range(256):
        fixture._transport.require()
        if time.monotonic() >= deadline:
            raise WatchdogError("canonical preparation event deadline exceeded")
        event = fixture._session.pop_event()
        if event is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WatchdogError("canonical preparation event deadline exceeded")
            event = fixture._session.poll_event(min(0.05, remaining))
        if time.monotonic() >= deadline:
            raise WatchdogError("canonical preparation event deadline exceeded")
        if fixture._connected_peers:
            raise WatchdogError("canonical preparation requires no GAP peers")
        if event is None:
            return
        if (
            type(event.service) is not int
            or type(event.opcode) is not int
            or type(event.index) is not int
            or type(event.payload) is not bytes
        ):
            raise WatchdogError("malformed canonical preparation event")
        if event.service == GAP and event.opcode in (
            GAP_DEVICE_CONNECTED,
            GAP_DEVICE_DISCONNECTED,
        ):
            expected_length = 13 if event.opcode == GAP_DEVICE_CONNECTED else 7
            if (
                event.index != 0
                or len(event.payload) != expected_length
                or event.payload[0] not in range(4)
            ):
                raise WatchdogError("malformed canonical preparation GAP peer event")
            # The preceding flow has already closed its exact peer. Neither a new
            # connection nor an unowned disconnect authorizes GATT mutation here.
            raise WatchdogError("unexpected canonical preparation GAP peer event")
    raise WatchdogError("canonical preparation event backlog did not reach idle")


def restore_canonical(
    fixture: Any, root: Path, handles: dict[str, int]
) -> list[dict[str, Any]]:
    """Use existing ATT handles, after the previous hold/peer is physically closed."""
    cancellation, _ = load_suite_controls(root)
    evidence = read_document(root / cancellation.directory / "physical-evidence.json")
    if (
        evidence.get("status") != "passed"
        or evidence.get("control") != asdict(cancellation)
        or fixture._connected_peers
    ):
        raise WatchdogError(
            "watchdog requires the preceding cancellation physical close"
        )
    fault = FaultController(fixture._session, cancellation.session)
    deadline = time.monotonic() + 10
    fixture._transport.require()
    fixture._gap.stop_advertising()
    _require_no_peer(fixture, deadline)
    closed = evidence["boundaries"][-1]
    ticket = ticket_from_document(closed["ticket"])
    previous = fault.status(ticket)
    _require_no_peer(fixture, deadline)
    from .fault_control import read_completed

    if not read_completed(previous) or ticket.handle != handles["read-write"]:
        raise WatchdogError(
            "previous cancellation lacks resource-zero NORMAL_NO_COMMIT"
        )
    limits = fault.require_idle()
    _require_no_peer(fixture, deadline)
    if limits.peer_present or limits.boot_nonce != ticket.boot_nonce:
        raise WatchdogError("previous cancellation peer/boot drift")
    restored = []
    for characteristic in fixture._profile.characteristics:
        handle = handles[characteristic.role]
        _require_no_peer(fixture, deadline)
        fixture._gatt.set_value(handle, characteristic.initial_value)
        _require_no_peer(fixture, deadline)
        # Non-readable characteristics cannot be attested by a local ATT read.
        readback = None
        if characteristic.permissions & 1:
            value = fixture._gatt.get_attribute_value(handle)
            _require_no_peer(fixture, deadline)
            if value.att_response != 0 or value.value != characteristic.initial_value:
                raise WatchdogError("canonical characteristic readback mismatch")
            readback = value.value.hex()
        restored.append(
            {
                "role": characteristic.role,
                "handle": handle,
                "value": characteristic.initial_value.hex(),
                "readback": readback,
            }
        )
    return restored


def run_connected(
    fixture: Any,
    root: Path,
    handles: dict[str, int],
    restored: list[dict[str, Any]],
    *,
    parent_guard: Any = lambda: None,
) -> None:
    """Execute flow 9 with the same live-attested DirectFixture BTP session."""
    plan = require_multifunction(fixture._fixture_session)
    controls = load_suite_controls(root)
    control = controls[1]
    identity = (os.getpid(), os.getpgid(0))
    verify_receipt(plan, fixture_identity=identity)
    if control.session != plan.session_id:
        raise WatchdogError("watchdog session drift")
    rw = next(c for c in fixture._profile.characteristics if c.role == "read-write")
    target = handles[rw.role]
    report: dict[str, Any] = {
        "schema_version": 2,
        "status": "running",
        "control": asdict(control),
        "session_id": plan.session_id,
        "contract_sha256": plan.contract_sha256,
        "profile_sha256": plan.profile_sha256,
        "resource_identity": plan.resource_identity,
        "fixture_pid": identity[0],
        "fixture_pgid": identity[1],
        "target_uuid": rw.uuid,
        "target_handle": target,
        "target_native_permissions": tester_characteristic_native_permissions(
            rw.permissions
        ),
        "release_budget_seconds": control.release_budget,
        "canonical_values": restored,
        "boundaries": [],
    }
    directory = root / control.directory
    directory.mkdir(exist_ok=True)

    cleaning = False

    def guard() -> None:
        fixture._transport.require()
        if cleaning:
            return
        parent_guard()
        if fixture._stop_requested or (directory / "abort").exists():
            raise WatchdogError("continuous watchdog aborted by owner")
        if load_suite_controls(root) != controls:
            raise WatchdogError("continuous control metadata changed")

    runtime = ContinuousRuntime(root, control, guard, report)
    experiment = ContinuousExperiment(fixture._session, fixture._profile, guard, report)
    experiment.strict_peer_events = True
    experiment.mutation_allowed = True
    primary: BaseException | None = None
    try:
        experiment.fixture_peers = fixture._connected_peers
        if fixture._connected_peers:
            raise WatchdogError("watchdog requires preceding exact no-peer boundary")
        runtime.request("arm", time.monotonic() + 300)
        attributes = fixture._gatt.get_attributes(attribute_uuid=rw.uuid)
        if (
            len(attributes) != 1
            or attributes[0].handle != target
            or attributes[0].uuid.casefold() != rw.uuid.casefold()
            or attributes[0].permissions
            != tester_characteristic_native_permissions(rw.permissions)
        ):
            raise WatchdogError("watchdog ATT target/profile mismatch")
        prior = read_document(root / controls[0].directory / "physical-evidence.json")
        if prior.get("schema_version") != 2 or prior.get("status") != "passed":
            raise WatchdogError("watchdog requires v2 cancellation evidence")
        previous_ticket = ticket_from_document(prior["boundaries"][-1]["ticket"])
        previous = experiment.fault.status(previous_ticket)
        from .fault_control import read_completed

        if not read_completed(previous):
            raise WatchdogError("previous cancellation is not rearmable/resource-zero")
        experiment.run_connected(runtime, target)
        if experiment.ticket == previous_ticket:
            raise WatchdogError("watchdog reused the cancellation ticket")
        verify_receipt(plan, fixture_identity=identity)
    except BaseException as error:  # noqa: BLE001 - preserve primary failure through safety cleanup
        primary = error
        report["primary_error"] = str(error)
    finally:
        # Caller abort must not veto cleanup of this exact leased native generation.
        cleaning = True
        try:
            experiment.cleanup()
            cleanup = report["physical_cleanup"]
            if (
                experiment.peers
                or fixture._connected_peers
                or not cleanup["hold_inactive"]
            ):
                raise WatchdogError(
                    "continuous watchdog cleanup lacks inactive hold/no peers"
                )
            limits = experiment.fault.require_idle()
            experiment.events()
            if limits.peer_present or experiment.peers or fixture._connected_peers:
                raise WatchdogError("continuous watchdog cleanup live peer remains")
            if (
                experiment.ticket is not None
                and limits.boot_nonce != experiment.ticket.boot_nonce
            ):
                raise WatchdogError("continuous watchdog cleanup boot drift")
            cleanup["peer_state"] = "no-known-peers-after-continuous-v2-retirement"
        except BaseException as error:  # noqa: BLE001 - preserve primary failure through safety cleanup
            if primary is None:
                primary = error
                report["primary_error"] = str(error)
            else:
                primary.add_note(f"watchdog cleanup failed: {error}")
        report["status"] = "passed" if primary is None else "failed"
        write_document(directory / "physical-evidence.json", report)
        if primary is not None:
            write_document(
                directory / "failure.json",
                {"control": asdict(control), "error": str(primary)},
            )
    if primary is not None:
        raise primary


def check_physical_evidence(
    path: Path, text: str, cancellation_path: Path, service_uuid: str
) -> None:
    """Strict same-attempt physical proof, independent of JS summary markers."""
    import math
    import re

    from .cancellation_control import control_markers
    from .fixture_session import REQUEST_NAME, load_session, load_session_profile
    from .gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED

    root = path.parent.parent
    cancellation_control, control = load_suite_controls(root)
    if (
        path != root / control.directory / "physical-evidence.json"
        or cancellation_path
        != root / cancellation_control.directory / "physical-evidence.json"
    ):
        raise ValueError("watchdog physical attempt/path drift")
    evidence = read_document(path)
    cancellation = read_document(cancellation_path)
    plan = require_multifunction(load_session(root / "fixture-session" / REQUEST_NAME))
    verify_receipt(
        plan, fixture_identity=(evidence["fixture_pid"], evidence["fixture_pgid"])
    )
    expected_fields = {
        "schema_version",
        "status",
        "control",
        "session_id",
        "contract_sha256",
        "profile_sha256",
        "resource_identity",
        "fixture_pid",
        "fixture_pgid",
        "target_uuid",
        "target_handle",
        "target_native_permissions",
        "release_budget_seconds",
        "canonical_values",
        "boundaries",
        "physical_cleanup_errors",
        "physical_cleanup",
        "ledger",
    }
    if (
        set(evidence) != expected_fields
        or type(evidence.get("schema_version")) is not int
        or any(
            type(evidence.get(key)) is not int or evidence[key] <= 0
            for key in (
                "fixture_pid",
                "fixture_pgid",
                "target_handle",
                "target_native_permissions",
            )
        )
        or evidence.get("status") != "passed"
        or evidence.get("schema_version") != 2
        or evidence.get("control") != asdict(control)
        or evidence.get("primary_error")
        or evidence.get("physical_cleanup_errors") != []
    ):
        raise ValueError("failed/incomplete watchdog physical proof")
    for key in (
        "session_id",
        "contract_sha256",
        "profile_sha256",
        "resource_identity",
        "fixture_pid",
        "fixture_pgid",
        "target_handle",
        "target_uuid",
    ):
        if evidence[key] != cancellation[key]:
            raise ValueError("watchdog/cancellation session/process/ATT drift")
    if (
        control.session != plan.session_id
        or plan.service_uuid != service_uuid.casefold()
        or any(
            evidence[key] != getattr(plan, key)
            for key in (
                "session_id",
                "contract_sha256",
                "profile_sha256",
                "resource_identity",
            )
        )
    ):
        raise ValueError("watchdog frozen provider drift")
    profile = load_session_profile(plan.contract, plan.profile_path)
    rw = next(c for c in profile.characteristics if c.role == "read-write")
    if (
        evidence["target_uuid"] != rw.uuid
        or evidence["target_native_permissions"]
        != tester_characteristic_native_permissions(rw.permissions)
        or evidence["release_budget_seconds"] != control.release_budget
    ):
        raise ValueError("watchdog physical target/permissions/budget drift")
    restored = evidence["canonical_values"]
    if not isinstance(restored, list) or len(restored) != len(profile.characteristics):
        raise ValueError("canonical value coverage incomplete")
    handles = set()
    for item, characteristic in zip(restored, profile.characteristics, strict=True):
        if (
            set(item) != {"role", "handle", "value", "readback"}
            or item["role"] != characteristic.role
            or item["value"] != characteristic.initial_value.hex()
            or item["readback"]
            != (
                characteristic.initial_value.hex()
                if characteristic.permissions & 1
                else None
            )
            or type(item["handle"]) is not int
            or not 0 < item["handle"] <= 65535
            or item["handle"] in handles
        ):
            raise ValueError("canonical initial-value/handle proof drift")
        handles.add(item["handle"])
        if (
            characteristic.role == "read-write"
            and item["handle"] != evidence["target_handle"]
        ):
            raise ValueError("canonical RW handle drift")
    boundaries = evidence["boundaries"]
    times = []
    for boundary in boundaries:
        instant = boundary["monotonic"]
        if (
            type(instant) not in (int, float)
            or not math.isfinite(instant)
            or instant < 0
            or (times and instant < times[-1])
        ):
            raise ValueError("watchdog boundary clock drift")
        times.append(instant)
    required = [
        "armed",
        "held",
        "retired-after-watchdog",
        "post-retirement-old-route-silence",
        "advertising",
    ]
    selected = [b for b in boundaries if b["boundary"] in required]
    if [b["boundary"] for b in selected] != required:
        raise ValueError("watchdog physical boundary coverage/order drift")
    armed, held, retired, silence, advertising = selected
    ticket = ticket_from_document(
        {k: armed[k] for k in ("wire_hex", "sequence", "handle")}
    )
    prior = ticket_from_document(cancellation["boundaries"][-1]["ticket"])
    if (
        cancellation.get("schema_version") != 2
        or ticket.operation is not Operation.READ
        or ticket.owner_nonce != owner_nonce(plan.session_id)
        or ticket.handle != evidence["target_handle"]
        or ticket.boot_nonce != prior.boot_nonce
        or ticket.sequence <= prior.sequence
    ):
        raise ValueError("watchdog ARM ticket/owner/boot drift")
    receipts = []
    for boundary in (armed, held, retired):
        current = ticket_from_document(
            {k: boundary[k] for k in ("wire_hex", "sequence", "handle")}
        )
        raw = boundary["status_wire"]
        if type(raw) is not str:
            raise ValueError("watchdog STATUS must be canonical hex")
        status = decode_status(bytes.fromhex(raw))
        if (
            current != ticket
            or status.ticket != ticket
            or encode_status(status).hex() != raw
        ):
            raise ValueError("watchdog STATUS ticket drift")
        receipts.append(status)
    armed_status, held_status, retired_status = receipts
    if (
        armed_status.phase is not Phase.ARMED
        or held_status.phase is not Phase.HELD
        or not cleanup_ready(retired_status)
        or retired_status.winner is not Winner.DISCONNECT
        or retired_status.outcome is not Outcome.DISCONNECTED_UNCOMMITTED
        or not 0 <= held["monotonic"] - armed["monotonic"] < 8
        or not 0 <= retired["monotonic"] - held["monotonic"] < 16
        or armed_status.remaining.arm_ms == 0
        or held_status.remaining.hold_ms == 0
        or retired["monotonic"] - held["monotonic"]
        >= held_status.remaining.hold_ms / 1000
        or armed["characteristic"] != retired["native_route"]
    ):
        raise ValueError("watchdog HELD/retirement/deadline proof invalid")
    if (
        silence["observation_ms"] < 1000
        or silence["monotonic"] - retired["monotonic"] < 1
        or silence["commands"] != ["21010", "21011"]
    ):
        raise ValueError("missing independent post-retirement old-route silence")
    gaps = [b for b in boundaries if b["boundary"] == "gap"]
    if [b["opcode"] for b in gaps] != [
        GAP_DEVICE_CONNECTED,
        GAP_DEVICE_DISCONNECTED,
        GAP_DEVICE_CONNECTED,
        GAP_DEVICE_DISCONNECTED,
    ]:
        raise ValueError("watchdog exact GAP lifecycle incomplete")
    if (
        any(
            not isinstance(b["peer"], str)
            or re.fullmatch(r"0[0-3][0-9a-f]{12}", b["peer"]) is None
            for b in gaps
        )
        or gaps[0]["peer"] != gaps[1]["peer"]
        or gaps[2]["peer"] != gaps[3]["peer"]
        or armed["peer"] != gaps[0]["peer"]
        or retired["peer"] != gaps[0]["peer"]
        or any(status.peer.hex() != gaps[0]["peer"] for status in receipts)
        or not held["monotonic"] <= gaps[1]["monotonic"] <= retired["monotonic"]
        or gaps[2]["monotonic"] < advertising["monotonic"]
        or gaps[0]["monotonic"] < cancellation["boundaries"][-1]["monotonic"]
    ):
        raise ValueError("watchdog GAP peer/order drift")
    cleanup = evidence["physical_cleanup"]
    if cleanup != {
        "hold_inactive": True,
        "advertising_stopped": True,
        "peer_state": "no-known-peers-after-continuous-v2-retirement",
        "radio_power": "unchanged",
        "known_peers_remaining": [],
        "status_wire": retired["status_wire"],
    }:
        raise ValueError("watchdog physical cleanup incomplete")
    records = re.findall(r"BLEHUB_NITRO_RUNTIME (rf-control:[^\r\n]+)", text)
    if records != control_markers(control):
        raise ValueError("watchdog hold control signals incomplete/foreign")
    characteristic = armed["characteristic"]
    for _, event in watchdog_ledger.events(watchdog_ledger.markers(text)):
        if (
            event["kind"] == "advertisementObserved"
            and service_uuid in event.get("advertisedServices", [])
            and "localName" in event
            and event["localName"] != profile.local_name
        ):
            raise ValueError("watchdog frozen provider local name drift")
    if evidence["ledger"] != watchdog_ledger.verify_ledger(
        text, characteristic=characteristic, service_uuid=service_uuid, integrated=True
    ):
        raise ValueError("watchdog physical/native ledger drift")
    ordered = [
        "rf-watchdog:discovery-ready:",
        control.marker("ready", "arm"),
        control.marker("observed", "armed"),
        "rf-watchdog:active-dispatched:",
        control.marker("ready", "read-dispatched"),
        control.marker("observed", "held"),
        "rf-watchdog:read-dispatched:",
        watchdog_ledger.AWAITING,
        control.marker("ready", "release"),
        control.marker("observed", "released"),
        watchdog_ledger.SILENCE,
        control.marker("ready", "recover"),
        control.marker("observed", "recovery"),
        "rf-watchdog:recovery-read:passed:",
        watchdog_ledger.CLOSE,
        watchdog_ledger.PASSED,
    ]
    positions = [text.index(marker) for marker in ordered]
    if positions != sorted(positions):
        raise ValueError("watchdog discovery/WAITING/recovery protocol drift")
