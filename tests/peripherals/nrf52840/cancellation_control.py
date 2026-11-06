"""Attempt-bound test control protocol. No transport or second BTP owner."""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .fixture_contract import MULTIFUNCTION_CAPABILITY_VARIANTS
from .fixture_session import FixtureSession, _json, _read_bytes, verify_receipt

CONTROL_NAME = "cancellation-control.json"
PHASES = ("armed", "held", "released")
REQUESTS = ("arm", "read-dispatched", "release")
RELEASE_BUDGET = 7.0  # Conservative ARM anchor precedes the 10s native watchdog.
POLL_SECONDS = 0.05
WATCHDOG_CONTROL_NAME = "watchdog-control.json"
WATCHDOG_PHASES = (*PHASES, "recovery")
WATCHDOG_REQUESTS = (*REQUESTS, "recover")
# Aggregate host boundary: <8s ARM capture + <16s watchdog/disconnect/drain.
# This is NOT a firmware hold budget (20s) or a change to the native watchdog (10s).
WATCHDOG_RELEASE_BUDGET = 24.0


@dataclass(frozen=True)
class Control:
    session: str
    flow: int
    mode: str
    token: str

    def __post_init__(self) -> None:
        for value in (self.session, self.token):
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError("RF control requires canonical UUIDs")
        if type(self.flow) is not int or (self.flow, self.mode) not in (
            (8, "rf-cancellation"),
            (9, "rf-watchdog"),
            (10, "rf-write-reject"),
            (11, "rf-ccc-enable-reject"),
            (12, "rf-ccc-disable-reject"),
        ):
            raise ValueError("RF control requires an exact supported flow/mode pair")

    @property
    def phases(self) -> tuple[str, ...]:
        if self.flow == 12:
            return (
                "baseline-value-sent",
                "armed",
                "held",
                "rejected",
                "continued-value-sent",
                "retry-allowed",
                "silence-started",
                "close-allowed",
            )
        if self.flow == 11:
            return (
                "armed",
                "held",
                "rejected",
                "value-sent",
                "disable-allowed",
                "silence-started",
                "close-allowed",
            )
        if self.flow == 10:
            return ("armed", "held", "rejected", "close-allowed")
        return WATCHDOG_PHASES if self.flow == 9 else PHASES

    @property
    def requests(self) -> tuple[str, ...]:
        if self.flow == 12:
            return (
                "baseline-enabled",
                "arm",
                "disable-dispatched",
                "reject",
                "reject-observed",
                "continued-notified",
                "disabled",
                "silence-done",
            )
        if self.flow == 11:
            return (
                "arm",
                "enable-dispatched",
                "reject",
                "recovery-enabled",
                "recovery-notified",
                "disabled",
                "silence-done",
            )
        if self.flow == 10:
            return ("arm", "write-dispatched", "reject", "recovery-done")
        return WATCHDOG_REQUESTS if self.flow == 9 else REQUESTS

    @property
    def filename(self) -> str:
        if self.flow == 12:
            return "ccc-disable-reject-control.json"
        if self.flow == 11:
            return "ccc-enable-reject-control.json"
        if self.flow == 10:
            return "write-reject-control.json"
        return WATCHDOG_CONTROL_NAME if self.flow == 9 else CONTROL_NAME

    @property
    def release_budget(self) -> float:
        return WATCHDOG_RELEASE_BUDGET if self.flow == 9 else RELEASE_BUDGET

    @property
    def directory(self) -> str:
        return f"flow-{self.flow}-{self.mode}"

    def uri(self, phase: str) -> str:
        if phase not in self.phases:
            raise ValueError("unknown RF control signal")
        return f"blehub-rf://control/{self.session}/{self.flow}/{self.mode}/{self.token}/{phase}"

    def notification(self, phase: str) -> str:
        self.uri(phase)
        return f"dev.blehub.rf.control.{self.session}.{self.flow}.{self.mode}.{self.token}.{phase}"

    def marker(self, kind: str, phase: str) -> str:
        return f"rf-control:{kind}:session={self.session}:flow={self.flow}:mode={self.mode}:token={self.token}:phase={phase}"


def read_document(path: Path) -> dict[str, Any]:
    return _json(_read_bytes(path))


def write_document(path: Path, value: dict[str, Any]) -> None:
    """Publish complete JSON atomically; attempt directories are privately owned."""
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
    temporary.replace(path)


def load_control(path: Path) -> Control:
    value = read_document(path)
    if set(value) != {"session", "flow", "mode", "token"}:
        raise ValueError("malformed RF control metadata")
    return Control(**value)


def require_multifunction(
    plan: FixtureSession | None, *, required_capabilities: frozenset[str] = frozenset()
) -> FixtureSession:
    """Check the admitted variant and opt-in needs; receipt verification stays separate."""
    if (
        plan is None
        or plan.contract.fixture_role != "multifunction-rf"
        or plan.contract.capabilities
        not in tuple(
            frozenset(variant) for variant in MULTIFUNCTION_CAPABILITY_VARIANTS
        )
    ):
        raise ValueError(
            "normal9 cancellation requires live-attested multifunction-rf; legacy active probing is unsupported"
        )
    missing = required_capabilities - plan.contract.capabilities
    if missing:
        raise ValueError(
            "multifunction-rf missing required capabilities: "
            + ", ".join(sorted(missing))
        )
    return plan


def prepare_control(root: Path, plan: FixtureSession) -> Control:
    require_multifunction(plan)
    control = Control(plan.session_id, 8, "rf-cancellation", str(uuid.uuid4()))
    write_document(root / CONTROL_NAME, asdict(control))
    return control


def prepare_suite_controls(root: Path, plan: FixtureSession) -> tuple[Control, Control]:
    cancellation = prepare_control(root, plan)
    watchdog = Control(plan.session_id, 9, "rf-watchdog", str(uuid.uuid4()))
    write_document(root / WATCHDOG_CONTROL_NAME, asdict(watchdog))
    return cancellation, watchdog


def load_suite_controls(root: Path) -> tuple[Control, Control]:
    controls = (
        load_control(root / CONTROL_NAME),
        load_control(root / WATCHDOG_CONTROL_NAME),
    )
    if (
        controls[0].flow != 8
        or controls[1].flow != 9
        or controls[0].session != controls[1].session
        or controls[0].token == controls[1].token
    ):
        raise ValueError("continuous hold control bundle drift")
    return controls


class SuiteControlRelay:
    """One ordered protocol across both holds, with no second runtime owner."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.controls = load_suite_controls(root)
        self.relays = [ControlRelay(root, control) for control in self.controls]
        self.control = self.controls[0]
        self.directory = self.relays[0].directory

    def observe(self, text: str) -> None:
        if load_suite_controls(self.root) != self.controls:
            raise ValueError("continuous control generation drift")
        records = re.findall(r"BLEHUB_NITRO_RUNTIME (rf-control:[^\r\n]+)", text)
        expected = [
            record for control in self.controls for record in control_markers(control)
        ]
        if records != expected[: len(records)] or len(records) > len(expected):
            raise ValueError("foreign, early, duplicate or dropped suite hold signal")
        # The provider reads this immutable-prefix snapshot, not a second log capture.
        temporary = self.root / "runtime.log.tmp"
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(self.root / "runtime.log")
        for relay in self.relays:
            scoped = "\n".join(
                line
                for line in text.splitlines()
                if "BLEHUB_NITRO_RUNTIME rf-control:" not in line
                or f":token={relay.control.token}:" in line
            )
            relay.observe(scoped)

    def pending_control(self) -> tuple[ControlRelay, str] | None:
        for relay in self.relays:
            phase = relay.pending()
            if phase is not None:
                return relay, phase
        return None

    def physical_complete(self) -> bool:
        return all(
            (relay.directory / "physical-evidence.json").exists()
            and read_document(relay.directory / "physical-evidence.json").get("status")
            == "passed"
            for relay in self.relays
        )


def verify_control(
    root: Path, plan: FixtureSession, identity: tuple[int, int]
) -> Control:
    require_multifunction(plan)
    verify_receipt(plan, fixture_identity=identity)
    control = load_control(root / CONTROL_NAME)
    if control.session != plan.session_id:
        raise ValueError("RF control session drift")
    return control


class ControlRelay:
    """Validate unique ordered JS requests and provider signals before forwarding."""

    def __init__(self, root: Path, control: Control) -> None:
        self.root, self.control = root, control
        self.directory = root / control.directory
        self.seen: list[str] = []
        self.sent: list[str] = []
        self.records: list[str] = []

    def observe(self, text: str) -> None:
        records = re.findall(r"BLEHUB_NITRO_RUNTIME (rf-control:[^\r\n]+)", text)
        if any(line.startswith("rf-control:host-failed:") for line in records):
            raise RuntimeError("native host rejected RF control signal")
        expected_records = control_markers(self.control)
        if (
            records != expected_records[: len(records)]
            or len(records) > len(expected_records)
            or records[: len(self.records)] != self.records
        ):
            raise ValueError(
                "foreign, duplicate, early or dropped RF control acknowledgement"
            )
        for phase in self.control.phases:
            if (
                self.control.marker("observed", phase) in records
                and phase not in self.sent
            ):
                raise ValueError("RF acknowledgement preceded forwarding")
        self.records = records
        ready = [line for line in records if line.startswith("rf-control:ready:")]
        expected = [self.control.marker("ready", p) for p in self.control.requests]
        if (
            ready != expected[: len(ready)]
            or len(ready) > len(expected)
            or ready[: len(self.seen)] != self.seen
        ):
            raise ValueError("foreign, duplicate, early or dropped RF control request")
        listeners = [
            line for line in records if line.startswith("rf-control:listener:")
        ]
        if listeners and listeners != [self.control.marker("listener", "registered")]:
            raise ValueError("foreign or duplicate RF control listener")
        if ready and listeners != [self.control.marker("listener", "registered")]:
            raise ValueError("RF control listener missing or duplicated")
        for index in range(len(self.seen), len(ready)):
            if index > len(self.sent):
                raise ValueError("RF control request preceded provider signal")
            phase = self.control.requests[index]
            if phase == "release" and self.control.flow == 9:
                from tools.react_native.watchdog_ledger import release_gate

                release_gate(text)
            if phase == "release" and self.control.flow == 8:
                terminal = "BLEHUB_NITRO_RUNTIME rf:step:cancellation-terminal:passed:commandId=9028:error=cancelled"
                if text.count(terminal) != 1 or text.index(terminal) >= text.index(
                    self.control.marker("ready", phase)
                ):
                    raise ValueError(
                        "release requires the exact queued Cancelled terminal"
                    )
            self.directory.mkdir(exist_ok=True)
            write_document(
                self.directory / f"request-{phase}.json", asdict(self.control)
            )
            self.seen.append(ready[index])

    def pending(self) -> str | None:
        if load_control(self.root / self.control.filename) != self.control:
            raise ValueError("RF control metadata generation drift")
        failure = self.directory / "failure.json"
        if failure.exists():
            raise RuntimeError(
                f"physical cancellation barrier failed: {read_document(failure)}"
            )
        if len(self.sent) == len(self.control.phases):
            return None
        phase = self.control.phases[len(self.sent)]
        path = self.directory / f"signal-{phase}.json"
        if not path.exists():
            return None
        if len(self.seen) <= len(self.sent) or read_document(path) != asdict(
            self.control
        ):
            raise ValueError("provider signal is early or has foreign metadata")
        evidence = read_document(self.directory / "physical-evidence.json")
        boundaries = evidence.get("boundaries", [])
        boundary_name = {
            "armed": "armed",
            "held": "held",
            "released": "retired-after-watchdog"
            if self.control.flow == 9
            else "read-completed",
            "recovery": "post-retirement-old-route-silence",
        }[phase]
        names = [boundary.get("boundary") for boundary in boundaries]
        if (
            evidence.get("control") != asdict(self.control)
            or evidence.get("primary_error")
            or evidence.get("cleanup_errors")
            or evidence.get("physical_cleanup_errors")
            or evidence.get("status") not in ("running", "passed")
            or evidence.get("schema_version") != 2
            or names.count("armed") != 1
            or names.count(boundary_name) != 1
        ):
            raise ValueError("RF signal lacks matching physical boundary")
        from .fault_control import (
            Operation,
            Outcome,
            Phase,
            Winner,
            cleanup_ready,
            decode_status,
            encode_status,
            owner_nonce,
            read_completed,
            ticket_from_document,
        )

        armed = next(b for b in boundaries if b["boundary"] == "armed")
        row = next(b for b in boundaries if b["boundary"] == boundary_name)
        if phase == "recovery":
            row = next(
                b for b in boundaries if b["boundary"] == "retired-after-watchdog"
            )

        def ticket_of(boundary):
            document = (
                boundary["ticket"]
                if self.control.flow == 8
                else {k: boundary[k] for k in ("wire_hex", "sequence", "handle")}
            )
            return ticket_from_document(document)

        ticket = ticket_of(row)
        raw = row["status_wire"]
        status = decode_status(bytes.fromhex(raw))
        if (
            ticket != ticket_of(armed)
            or ticket != status.ticket
            or ticket.owner_nonce != owner_nonce(self.control.session)
            or ticket.operation is not Operation.READ
            or ticket.handle != evidence["target_handle"]
            or encode_status(status).hex() != raw
        ):
            raise ValueError("RF signal lacks exact v2 ticket/owner/STATUS proof")
        if phase in ("armed", "held"):
            if status.phase is not (Phase.ARMED if phase == "armed" else Phase.HELD):
                raise ValueError("RF signal has wrong fault phase")
        elif self.control.flow == 8:
            if not read_completed(status):
                raise ValueError("RF signal lacks NORMAL_NO_COMMIT/resource-zero proof")
        elif not (
            cleanup_ready(status)
            and status.winner is Winner.DISCONNECT
            and status.outcome is Outcome.DISCONNECTED_UNCOMMITTED
        ):
            raise ValueError(
                "RF signal lacks disconnected retained resource-zero proof"
            )
        anchor = armed["monotonic"]
        now = time.monotonic()
        elapsed = now - anchor
        if not 0 <= now - row["monotonic"]:
            raise TimeoutError("RF signal physical boundary is in the future")
        if phase in ("armed", "held"):
            remaining = (
                status.remaining.arm_ms
                if phase == "armed"
                else status.remaining.hold_ms
            )
            if not 0 <= now - row["monotonic"] < remaining / 1000:
                raise TimeoutError("RF signal STATUS deadline expired")
        if self.control.flow == 9 and phase == "released":
            held_rows = [b for b in boundaries if b.get("boundary") == "held"]
            if len(held_rows) != 1:
                raise ValueError("watchdog release lacks one HELD deadline anchor")
            held = held_rows[0]
            captured = decode_status(bytes.fromhex(held["status_wire"]))
            if captured.ticket != ticket or captured.phase is not Phase.HELD:
                raise ValueError("watchdog release HELD ticket/phase drift")
            deadline = min(
                anchor + self.control.release_budget,
                held["monotonic"] + 16,
                held["monotonic"] + captured.remaining.hold_ms / 1000,
            )
            if not 0 <= now - held["monotonic"] or now >= deadline:
                raise TimeoutError(
                    "watchdog release signal exceeded the absolute hold/host budget"
                )
        if phase in ("armed", "held") or self.control.flow == 8:
            budget = 8 if self.control.flow == 9 else self.control.release_budget
            if not 0 <= elapsed < budget:
                raise TimeoutError("RF signal arrived outside native release budget")
        elif not 0 <= elapsed < self.control.release_budget + 70:
            raise TimeoutError("RF recovery signal deadline exceeded")
        if elapsed < 0:
            raise TimeoutError("RF signal arrived outside pre-native-watchdog budget")
        return phase

    def forwarded(self, phase: str) -> None:
        if phase != self.control.phases[len(self.sent)]:
            raise ValueError("RF signal forwarding drift")
        self.sent.append(phase)


def control_markers(control: Control) -> list[str]:
    return [
        control.marker("listener", "registered"),
        *(
            marker
            for request, phase in zip(control.requests, control.phases, strict=True)
            for marker in (
                control.marker("ready", request),
                control.marker("observed", phase),
            )
        ),
    ]


def abort_control(root: Path) -> None:
    """Notify only this attempt owner; never issue a second BTP request."""
    paths = [root / CONTROL_NAME, root / WATCHDOG_CONTROL_NAME]
    for path in paths:
        if not path.exists():
            continue
        control = load_control(path)
        directory = root / control.directory
        directory.mkdir(exist_ok=True)
        (directory / "abort").touch()


def validate_preparation(
    preparation: Any,
    evidence: dict[str, Any],
    *,
    boot_nonce: bytes,
    armed_at: float,
) -> None:
    """Validate continuous flow-8 preparation without weakening the hold ledger."""
    from .gap_commands import GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED

    bound = {
        "control",
        "session_id",
        "contract_sha256",
        "profile_sha256",
        "resource_identity",
        "fixture_pid",
        "fixture_pgid",
        "target_uuid",
        "target_handle",
    }
    fields = bound | {
        "schema_version",
        "status",
        "before",
        "expected",
        "readback",
        "boot_nonce",
        "engine_flags",
        "peer_present",
        "peer_count",
        "monotonic",
        "passive_recovery_close",
    }
    if (
        type(preparation) is not dict
        or set(preparation) != fields
        or any(preparation[key] != evidence[key] for key in bound)
        or any(
            type(preparation[key]) is not int
            for key in ("fixture_pid", "fixture_pgid", "target_handle")
        )
        or type(preparation["schema_version"]) is not int
        or preparation["schema_version"] != 1
        or preparation["status"] != "prepared"
        or type(preparation["engine_flags"]) is not int
        or preparation["engine_flags"] != 0
        or preparation["peer_present"] is not False
        or type(preparation["peer_count"]) is not int
        or preparation["peer_count"] != 0
        or preparation["expected"] != "00"
        or preparation["readback"] != "00"
        or preparation["boot_nonce"] != boot_nonce.hex()
        or type(preparation["before"]) is not str
        or re.fullmatch(r"(?:[0-9a-f]{2})*", preparation["before"]) is None
    ):
        raise ValueError(
            "cancellation preparation shape/owner/control/target/boot/readback drift"
        )

    def instant(value: Any) -> float:
        if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
            raise ValueError("cancellation preparation invalid clock")
        return value

    prepared_at = instant(preparation["monotonic"])
    if prepared_at > instant(armed_at):
        raise ValueError("cancellation preparation occurred after ARM")
    close = preparation["passive_recovery_close"]
    if (
        type(close) is not dict
        or set(close)
        != {"peer", "events", "advertising_stopped", "peer_count", "monotonic"}
        or close["advertising_stopped"] is not True
        or type(close["peer_count"]) is not int
        or close["peer_count"] != 0
        or type(close["peer"]) is not list
        or len(close["peer"]) != 2
        or type(close["peer"][0]) is not str
        or re.fullmatch(r"[0-9a-f]{12}", close["peer"][0]) is None
        or type(close["peer"][1]) is not int
        or close["peer"][1] not in range(4)
        or type(close["events"]) is not list
        or len(close["events"]) != 4
    ):
        raise ValueError("cancellation preparation passive recovery close shape drift")
    wire_peer = bytes([close["peer"][1]]) + bytes.fromhex(close["peer"][0])[::-1]
    previous = 0.0
    for row, opcode in zip(
        close["events"],
        (GAP_DEVICE_CONNECTED, GAP_DEVICE_DISCONNECTED) * 2,
        strict=True,
    ):
        if (
            type(row) is not dict
            or set(row) != {"opcode", "index", "payload", "monotonic"}
            or type(row["opcode"]) is not int
            or row["opcode"] != opcode
            or type(row["index"]) is not int
            or row["index"] != 0
            or type(row["payload"]) is not str
            or re.fullmatch(
                r"[0-9a-f]{26}" if opcode == GAP_DEVICE_CONNECTED else r"[0-9a-f]{14}",
                row["payload"],
            )
            is None
            or not row["payload"].startswith(wire_peer.hex())
        ):
            raise ValueError(
                "cancellation preparation passive GAP owner/order/wire drift"
            )
        current = instant(row["monotonic"])
        if current < previous:
            raise ValueError("cancellation preparation passive GAP clock order drift")
        previous = current
    if not previous <= instant(close["monotonic"]) <= prepared_at:
        raise ValueError("cancellation preparation passive close clock order drift")


def check_physical_evidence(path: Path | None, log: str, service_uuid: str) -> None:
    """Offline gate: correlate physical boundaries with the frozen live receipt."""
    from .fixture_session import REQUEST_NAME, load_session, load_session_profile

    if path is None:
        raise ValueError(
            "normal9 requires physical cancellation evidence, not JS-only markers"
        )
    evidence = read_document(path)
    keys = {
        "schema_version",
        "control",
        "session_id",
        "contract_sha256",
        "profile_sha256",
        "resource_identity",
        "fixture_pid",
        "fixture_pgid",
        "target_uuid",
        "target_handle",
        "release_budget_seconds",
        "status",
        "boundaries",
        "cleanup_errors",
    }
    continuous = (
        "BLEHUB_NITRO_RUNTIME rf-lab-suite:flow-started:index=8:mode=rf-cancellation\n"
        in log
    )
    if continuous:
        keys.add("preparation")
    if (
        set(evidence) != keys
        or type(evidence["schema_version"]) is not int
        or evidence["schema_version"] != 2
        or evidence["status"] != "passed"
        or evidence["cleanup_errors"] != []
    ):
        raise ValueError("physical cancellation evidence is incomplete or failed")
    root = path.parent.parent
    control = load_control(root / CONTROL_NAME)
    if path != root / control.directory / "physical-evidence.json" or evidence[
        "control"
    ] != asdict(control):
        raise ValueError("physical cancellation control/attempt drift")
    plan = require_multifunction(load_session(root / "fixture-session" / REQUEST_NAME))
    identity = (evidence["fixture_pid"], evidence["fixture_pgid"])
    if any(type(value) is not int or value <= 0 for value in identity):
        raise ValueError("invalid physical fixture process identity")
    verify_control(root, plan, identity)
    if (
        any(
            evidence[field] != getattr(plan, field)
            for field in (
                "session_id",
                "contract_sha256",
                "profile_sha256",
                "resource_identity",
            )
        )
        or plan.service_uuid != service_uuid.casefold()
    ):
        raise ValueError("physical evidence session/profile/resource drift")
    profile = load_session_profile(plan.contract, plan.profile_path)
    rw = next(c for c in profile.characteristics if c.role == "read-write")
    target = evidence["target_handle"]
    if (
        evidence["target_uuid"] != rw.uuid
        or type(target) is not int
        or not 0 < target <= 65535
        or evidence["release_budget_seconds"] != RELEASE_BUDGET
        or (continuous and (rw.initial_value != b"\x00" or rw.permissions != 0x03))
    ):
        raise ValueError("physical target/profile or release budget drift")
    boundaries = evidence["boundaries"]
    names = (
        "armed",
        "held",
        "still-held",
        "resolve-dispatched",
        "read-completed",
        "closed",
    )
    if not isinstance(boundaries, list) or len(boundaries) != len(names):
        raise ValueError("physical boundary coverage is incomplete")
    times: list[float] = []
    from .fault_control import (
        Operation,
        Phase,
        decode_status,
        encode_status,
        owner_nonce,
        read_completed,
        ticket_from_document,
    )

    ticket = None
    peer = None
    for name, boundary in zip(names, boundaries, strict=True):
        fields = {"boundary", "monotonic"}
        fields |= {"ticket"}
        if name != "resolve-dispatched":
            fields |= {"status_wire"}
        if name == "closed":
            fields |= {"hold_inactive", "peer_count"}
        elif name != "resolve-dispatched":
            fields |= {"peer"}
        if (
            not isinstance(boundary, dict)
            or set(boundary) != fields
            or boundary["boundary"] != name
        ):
            raise ValueError("physical boundary order/schema drift")
        instant = boundary["monotonic"]
        if (
            type(instant) not in (float, int)
            or not math.isfinite(instant)
            or instant < 0
            or (times and instant < times[-1])
        ):
            raise ValueError("physical boundary clock drift")
        times.append(instant)
        current = ticket_from_document(boundary["ticket"])
        if (
            current.operation is not Operation.READ
            or current.owner_nonce != owner_nonce(plan.session_id)
            or current.handle != target
            or (ticket is not None and ticket != current)
        ):
            raise ValueError("physical STATUS ticket/owner/handle drift")
        ticket = current
        if name != "resolve-dispatched":
            raw = boundary["status_wire"]
            snapshot = decode_status(bytes.fromhex(raw))
            if snapshot.ticket != ticket or encode_status(snapshot).hex() != raw:
                raise ValueError("physical STATUS ticket/canonical wire drift")
            if name in ("read-completed", "closed"):
                if not read_completed(snapshot):
                    raise ValueError(
                        "physical read lacks resource-zero NORMAL_NO_COMMIT"
                    )
            elif snapshot.phase is not (Phase.ARMED if name == "armed" else Phase.HELD):
                raise ValueError("physical STATUS phase drift")
        if name == "closed":
            if (
                boundary["hold_inactive"] is not True
                or type(boundary["peer_count"]) is not int
                or boundary["peer_count"] != 0
            ):
                raise ValueError("physical close did not release hold/peer")
            continue
        if name != "resolve-dispatched":
            current_peer = boundary["peer"]
            if (
                not isinstance(current_peer, list)
                or len(current_peer) != 2
                or not isinstance(current_peer[0], str)
                or not re.fullmatch(r"[0-9a-f]{12}", current_peer[0])
                or type(current_peer[1]) is not int
                or current_peer[1] not in (0, 1, 2, 3)
                or (peer is not None and current_peer != peer)
            ):
                raise ValueError("physical GAP peer drift")
            peer = current_peer
            if snapshot.peer != bytes([peer[1]]) + bytes.fromhex(peer[0])[::-1]:
                raise ValueError("physical STATUS/GAP peer drift")
    if continuous:
        validate_preparation(
            evidence["preparation"],
            evidence,
            boot_nonce=ticket.boot_nonce,
            armed_at=times[0],
        )
    if times[4] - times[3] >= 2:
        raise ValueError("physical resolution exceeded firmware bound")
    if times[4] - times[0] >= RELEASE_BUDGET:
        raise ValueError("physical release missed pre-native-watchdog budget")
    records = re.findall(r"BLEHUB_NITRO_RUNTIME (rf-control:[^\r\n]+)", log)
    if records != control_markers(control):
        raise ValueError("missing, foreign, early or duplicated RF control signals")
    ordered = [
        "rf:step:gatt-discovery:passed:",
        control.marker("ready", "arm"),
        control.marker("observed", "armed"),
        control.marker("ready", "read-dispatched"),
        control.marker("observed", "held"),
        "rf:step:cancellation-active-rejection:passed:",
        "rf:step:cancellation-queued-accepted:passed:",
        "rf:step:cancellation-terminal:passed:commandId=9028:error=cancelled",
        control.marker("ready", "release"),
        control.marker("observed", "released"),
        "rf:step:cancellation-active-completion:passed:",
        "rf:step:cancellation-recovery:passed:",
        "rf-cancellation:event:",
        "rf:step:close:passed:",
        "rf-cancellation:passed:",
    ]
    ledger = re.findall(r"BLEHUB_NITRO_RUNTIME (rf-cancellation:event:[^\r\n]+)", log)
    if (
        len(ledger) != 1
        or ":commandId=9028|9027|9027|9029|9029:" not in ledger[0]
        or not ledger[0].endswith(":value=none|00|none|00|none")
    ):
        raise ValueError(
            "held cancellation ledger requires queued terminal before active read and exact 00/recovery"
        )
    positions = [log.index(marker) for marker in ordered]
    if positions != sorted(positions):
        raise ValueError("RF control cancellation/terminal/release order drift")
