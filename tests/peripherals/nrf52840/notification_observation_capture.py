"""Attempt-bound CCC diagnostic on the provider's existing leased BTP session.

No BLE stimulus, ACK wait, retry, reset, serial owner or delivery verdict lives here.
The unreleased firmware is not installed by this helper. Wire samples in tests are
explicitly synthetic; they are not a C/firmware crossing or RF result.
"""

from __future__ import annotations

import hashlib
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import notification_observation as wire
from .cancellation_control import write_document
from .fault_control import owner_nonce
from .fixture_session import FixtureSession, _json, _read_bytes

CAPABILITY = "notification-tx-observation-v1"
REQUEST_NAME = "notification-observation-request.json"
CAPTURE_NAME = "notification-observation.json"
MAX_EVENTS = 128
MAX_READS = 8  # Per update, including FINAL; never wait for a completion.
PHASE_VALUES = {
    "baseline-value-sent": "ef",
    "continued-value-sent": "e1",
    "silence-started": "f0",
}
LIMITATION = (
    "Local firmware observations only; not air, link ACK, Central or JS delivery."
)


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(f"notification observation: {message}")


def require_capability(plan: FixtureSession) -> None:
    require(
        CAPABILITY in plan.contract.capabilities, "explicit fixture capability required"
    )


def request_from_bytes(raw: bytes, session: str) -> dict[str, Any]:
    value = _json(raw)
    require(
        set(value) == {"schema_version", "session", "request"}
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["session"] == session
        and value["request"] is True,
        "exact schema/session/explicit request required",
    )
    return value


def prepare_request(root: Path, plan: FixtureSession) -> None:
    """Freeze explicit opt-in, before provider/App start; never upgrade capabilities."""
    require_capability(plan)
    require(not (root / REQUEST_NAME).exists(), "refusing existing request")
    write_document(
        root / REQUEST_NAME,
        {"schema_version": 1, "session": plan.session_id, "request": True},
    )


def source_identity(plan: FixtureSession) -> dict[str, Any]:
    return {
        "session": plan.session_id,
        "resource_identity": plan.resource_identity,
        "contract_sha256": plan.contract_sha256,
        "profile_sha256": plan.profile_sha256,
        "image_sha256": plan.contract.image_sha256,
    }


def route(limits: Any, handle: int, value: bytes) -> dict[str, Any]:
    return {
        "boot": limits.boot_nonce.hex(),
        "peer": limits.peer.hex(),
        "peer_present": limits.peer_present,
        "connection_epoch": limits.connection_epoch,
        "bearer_epoch": limits.bearer_epoch,
        "database_epoch": limits.database_epoch,
        "value_handle": handle,
        "payload_hex": value.hex(),
    }


class NotificationObservationCapture:
    """Narrow diagnostic owner; caller guards every entry with its fixture lease.

    Construct only for an existing request, after fault GET_LIMITS initializes boot.
    start() records each CAS handoff separately, including partial rotation failure.
    before_set()/after_set() bracket only the existing SET_VALUE. poll() is called
    from the existing pause, at most twice/second, outside the held fault window.
    finalize() takes FINAL before disabling only this exact owner/generation. It
    attempts FINAL even after a prior diagnostic failure, but never retries writes.
    """

    def __init__(
        self, session: Any, root: Path, plan: FixtureSession, boot: bytes
    ) -> None:
        require_capability(plan)
        self.root, self.plan, self.boot = root, plan, boot
        self.request_bytes = _read_bytes(root / REQUEST_NAME)
        request_from_bytes(self.request_bytes, plan.session_id)
        self.controller = wire.ObserverController(session)
        self.owner = owner_nonce(plan.session_id)
        self.current: wire.ObservationQuery | None = None
        self.next_poll = 0.0
        self.finalized = False
        self.failed = False
        self.document: dict[str, Any] = {
            "schema_version": 1,
            "source": source_identity(plan),
            "boot": boot.hex(),
            "owner": self.owner.hex(),
            "request_hex": self.request_bytes.hex(),
            "request_sha256": hashlib.sha256(self.request_bytes).hexdigest(),
            "limitation": LIMITATION,
            "status": "running",
            "events": [],
            "updates": [],
            "primary_errors": [],
            "cleanup_errors": [],
        }
        self.publish()

    def publish(self) -> None:
        write_document(self.root / CAPTURE_NAME, self.document)

    def check_request(self) -> None:
        require(
            _read_bytes(self.root / REQUEST_NAME) == self.request_bytes,
            "request bytes drift",
        )

    def exchange(self, phase: str, method: str, **kwargs: Any) -> Any:
        self.check_request()
        require(len(self.document["events"]) < MAX_EVENTS, "event budget exhausted")
        event: dict[str, Any] = {
            "phase": phase,
            "method": method,
            "monotonic": time.monotonic(),
            "wall_time": datetime.now(UTC).isoformat(),
        }
        self.document["events"].append(event)
        try:
            result = getattr(self.controller, method)(**kwargs)
            event["wire"] = result.document()
            if isinstance(result, wire.ObservationQuery):
                require(result.boot == self.boot, "boot drift")
                self.current = result
            self.publish()
            return result
        except BaseException as error:
            self.failed = True
            event["error"] = f"{type(error).__name__}: {error}"
            if isinstance(error, wire.ObservationCommandError):
                event["wire"] = error.document()
            self.document["primary_errors"].append(event["error"])
            try:
                self.publish()
            except BaseException as publication:  # noqa: BLE001 - publication never replaces primary
                error.add_note(f"observer error publication: {publication}")
            raise

    def query(self, phase: str) -> wire.ObservationQuery:
        return self.exchange(phase, "query", expected_boot=self.boot)

    def configure(
        self, phase: str, new_owner: bytes, enable: bool
    ) -> wire.ObservationQuery:
        require(self.current is not None, "missing current owner query")
        return self.exchange(
            phase,
            "configure",
            expected_boot=self.boot,
            expected_owner=self.current.owner,
            expected_owner_generation=self.current.owner_generation,
            new_owner=new_owner,
            enable=enable,
        )

    def start(self) -> None:
        current = self.query("initial-query")
        if current.owner not in (wire.ZERO_OWNER, self.owner):
            # This explicit request plus the physical lease authorizes handoff, not
            # arbitrary trace cleanup. Prevalidate exhaustion before turning it off.
            require(
                current.owner_generation < wire.U32_MAX, "owner generation exhausted"
            )
            self.configure("handoff-off", current.owner, False)
            self.configure("handoff-rotate-off", self.owner, False)
        self.owned(self.configure("enable", self.owner, True))

    def owned(self, query: wire.ObservationQuery) -> None:
        require(
            query.owner == self.owner
            and self.current is not None
            and query.owner_generation
            == self.document.get("owner_generation", query.owner_generation)
            and query.enabled,
            "observer owner/generation/enable drift",
        )
        self.document["owner_generation"] = query.owner_generation

    def before_set(self, phase: str, limits: Any, handle: int, value: bytes) -> int:
        require(
            phase in PHASE_VALUES and value.hex() == PHASE_VALUES[phase],
            "unknown update",
        )
        require(len(self.document["updates"]) < 3, "update budget")
        query = self.query(phase + ":before-set")
        self.owned(query)
        update = {
            "phase": phase,
            "before": route(limits, handle, value),
            "before_query": len(self.document["events"]) - 1,
            "after": None,
            "after_query": None,
            "sequence": None,
            "reads": [],
        }
        self.document["updates"].append(update)
        self.publish()
        return len(self.document["updates"]) - 1

    def after_set(self, index: int, limits: Any) -> None:
        update = self.document["updates"][index]
        before = update["before"]
        update["after"] = route(
            limits, before["value_handle"], bytes.fromhex(before["payload_hex"])
        )
        query = self.query(update["phase"] + ":after-set")
        self.owned(query)
        update["after_query"] = len(self.document["events"]) - 1
        previous = wire.ObserverController.query_from_document(
            self.document["events"][update["before_query"]]["wire"]
        )
        # Absence is unobserved, not TX failure or delivery. More than one update
        # cannot be assigned to this stimulus by matching its bytes.
        require(
            query.last_sequence in (previous.last_sequence, previous.last_sequence + 1),
            "ambiguous update sequence",
        )
        if query.last_sequence > previous.last_sequence:
            update["sequence"] = query.last_sequence
        self.publish()

    def read_updates(self, phase: str) -> None:
        for update in self.document["updates"]:
            sequence = update["sequence"]
            limit = MAX_READS if phase == "FINAL" else MAX_READS - 1
            if sequence is None or len(update["reads"]) >= limit:
                continue
            record = self.exchange(
                phase + ":" + update["phase"],
                "read",
                expected_boot=self.boot,
                record_owner=self.owner,
                sequence=sequence,
            )
            update["reads"].append(len(self.document["events"]) - 1)
            validate_record(
                record,
                update["before"],
                self.owner,
                self.document["owner_generation"],
                sequence,
            )
        self.publish()

    def poll(self) -> None:
        now = time.monotonic()
        pending = any(
            u["sequence"] is not None and len(u["reads"]) < MAX_READS - 1
            for u in self.document["updates"]
        )
        if self.failed or self.finalized or not pending or now < self.next_poll:
            return
        self.next_poll = now + 0.5
        query = self.query("poll")
        self.owned(query)
        self.read_updates("poll")

    def finalize(self) -> None:
        if self.finalized:
            return
        self.finalized = True
        errors: list[BaseException] = []
        authorized = False
        try:
            query = self.query("FINAL")
            self.owned(query)
            authorized = True
            self.read_updates("FINAL")
        except BaseException as error:  # noqa: BLE001 - preserve FINAL failure while attempting owned-off
            self.document["cleanup_errors"].append(
                f"FINAL: {type(error).__name__}: {error}"
            )
            errors.append(error)
        # Never disable a foreign generation, including after an ambiguous write.
        # A successful FINAL query is required to authorize this CAS.
        try:
            if authorized:
                self.configure("owned-off", self.owner, False)
        except BaseException as error:  # noqa: BLE001 - preserve primary versus cleanup on interruption
            self.document["cleanup_errors"].append(f"{type(error).__name__}: {error}")
            errors.append(error)
        self.document["status"] = "failed" if errors or self.failed else "complete"
        if (
            not errors
            and not self.failed
            and any(u["sequence"] is None for u in self.document["updates"])
        ):
            self.document["status"] = "unobserved"
        try:
            self.publish()
        except BaseException as publication:
            if not errors:
                raise
            errors[0].add_note(f"observer final publication: {publication}")
        if errors:
            for extra in errors[1:]:
                errors[0].add_note(f"observer cleanup: {extra}")
            raise errors[0]


def validate_record(
    record: wire.ObservationRecord,
    expected: dict[str, Any],
    owner: bytes,
    generation: int,
    sequence: int,
) -> None:
    require(
        record.boot.hex() == expected["boot"]
        and record.owner == owner
        and record.owner_generation == generation
        and record.sequence == sequence
        and record.value_handle == expected["value_handle"]
        and record.live_peer_present == expected["peer_present"]
        and record.live_peer is not None
        and record.live_peer.hex() == expected["peer"]
        and (record.selected_peer is None or record.selected_peer == record.live_peer)
        and (record.connection_epoch, record.bearer_epoch, record.database_epoch)
        == tuple(
            expected[k] for k in ("connection_epoch", "bearer_epoch", "database_epoch")
        )
        and record.value.hex() == expected["payload_hex"]
        and record.value_length == record.copied_length == len(record.value),
        "record boot/owner/sequence/target/peer/entry epochs/payload drift",
    )


def verify_capture(root: Path, *, complete: bool) -> None:
    """Replay typed wire and frozen identity even in failed/partial bundles.

    complete additionally requires all three diagnostics and FINAL/owned-off; no
    transport observation here can satisfy a missing App notification witness.
    """
    request_path, capture_path = root / REQUEST_NAME, root / CAPTURE_NAME
    if not request_path.exists():
        require(not capture_path.exists(), "orphan sidecar without explicit request")
        return
    fixture = _json(_read_bytes(root / "fixture-session/fixture-request.json"))
    raw = _read_bytes(request_path)
    request_from_bytes(raw, fixture["session_id"])
    contract = _json(_read_bytes(root / "fixture-session/contract.json"))
    require(CAPABILITY in contract["capabilities"], "request without capability")
    if not capture_path.exists():
        require(not complete, "missing requested capture")
        return
    doc = _json(_read_bytes(capture_path))
    require(
        set(doc)
        == {
            "schema_version",
            "source",
            "boot",
            "owner",
            "request_hex",
            "request_sha256",
            "limitation",
            "status",
            "events",
            "updates",
            "primary_errors",
            "cleanup_errors",
        }
        | ({"owner_generation"} if "owner_generation" in doc else set()),
        "sidecar keys",
    )
    require(
        type(doc["schema_version"]) is int and doc["schema_version"] == 1,
        "sidecar schema",
    )
    require(
        doc["request_hex"] == raw.hex()
        and doc["request_sha256"] == hashlib.sha256(raw).hexdigest(),
        "frozen request bytes drift",
    )
    expected_source = {
        k: fixture[k]
        for k in ("resource_identity", "contract_sha256", "profile_sha256")
    }
    expected_source.update(
        session=fixture["session_id"], image_sha256=contract["image_sha256"]
    )
    require(
        doc["source"] == expected_source and doc["limitation"] == LIMITATION,
        "source identity drift",
    )
    require(
        hashlib.sha256(_read_bytes(root / "fixture-session/contract.json")).hexdigest()
        == fixture["contract_sha256"],
        "contract hash drift",
    )
    require(
        hashlib.sha256(_read_bytes(root / "fixture-session/profile.json")).hexdigest()
        == fixture["profile_sha256"],
        "profile hash drift",
    )
    owner = owner_nonce(fixture["session_id"])
    require(doc["owner"] == owner.hex(), "attempt owner drift")
    physical: dict[str, Any] | None = None
    physical_path = root / "ccc-disable-reject-physical.json"
    if physical_path.exists():
        physical = _json(_read_bytes(physical_path))
        initial = [r for r in physical["boundaries"] if r["boundary"] == "no-peer"]
        require(
            len(initial) == 1 and doc["boot"] == initial[0]["limits"]["boot_nonce"],
            "fault GET_LIMITS boot drift",
        )
    else:
        require(not complete, "missing fault boot source")
    events, updates = doc["events"], doc["updates"]
    require(
        isinstance(events, list)
        and len(events) <= MAX_EVENTS
        and isinstance(updates, list)
        and len(updates) <= 3,
        "capture bounds",
    )
    decoded: dict[int, Any] = {}
    configurations: list[tuple[str, wire.ObservationQuery]] = []
    current = None
    previous_time = -1.0
    for index, event in enumerate(events):
        require(
            set(event) <= {"phase", "method", "monotonic", "wall_time", "wire", "error"}
            and {"phase", "method", "monotonic", "wall_time"} <= set(event),
            "event keys",
        )
        timestamp = event["monotonic"]
        require(
            type(timestamp) in (int, float)
            and math.isfinite(timestamp)
            and timestamp >= previous_time,
            "host timestamp ordering",
        )
        previous_time = timestamp
        require(
            datetime.fromisoformat(event["wall_time"]).tzinfo is not None,
            "wall timestamp timezone",
        )
        require(event["method"] in ("query", "configure", "read"), "event method")
        allowed_phases = {
            "query": {"initial-query", "poll", "FINAL"}
            | {
                p + suffix
                for p in PHASE_VALUES
                for suffix in (":before-set", ":after-set")
            },
            "configure": {"handoff-off", "handoff-rotate-off", "enable", "owned-off"},
            "read": {
                prefix + ":" + p for prefix in ("poll", "FINAL") for p in PHASE_VALUES
            },
        }
        require(
            event["phase"] in allowed_phases[event["method"]], "unknown boundary phase"
        )
        if "wire" not in event:
            require(bool(event.get("error")), "missing raw receipt")
            continue
        try:
            parsed = (
                wire.ObserverController.record_from_document
                if event["method"] == "read"
                else wire.ObserverController.query_from_document
            )(event["wire"])
        except wire.ObservationCommandError as error:
            require(
                bool(event.get("error")) and event["wire"] == error.document(),
                "unlabelled/changed firmware error",
            )
            continue
        require(
            "error" not in event and parsed.boot.hex() == doc["boot"],
            "receipt boot/error drift",
        )
        decoded[index] = parsed
        if isinstance(parsed, wire.ObservationQuery):
            require(
                parsed.opcode
                == (wire.CONFIGURE if event["method"] == "configure" else wire.QUERY),
                "method/opcode drift",
            )
            if parsed.opcode == wire.CONFIGURE:
                configurations.append((event["phase"], parsed))
                require(
                    current is not None
                    and parsed.request[17:33] == current.owner
                    and int.from_bytes(parsed.request[33:37], "little")
                    == current.owner_generation,
                    "CAS current owner/generation drift",
                )
                if current.owner not in (wire.ZERO_OWNER, parsed.owner):
                    require(
                        not current.enabled and not parsed.enabled,
                        "rotation without off",
                    )
            current = parsed
    referenced_reads: set[int] = set()
    for index, update in enumerate(updates):
        require(
            set(update)
            == {
                "phase",
                "before",
                "after",
                "before_query",
                "after_query",
                "sequence",
                "reads",
            },
            "update keys",
        )
        require(
            update["phase"] == tuple(PHASE_VALUES)[index]
            and update["before"]["payload_hex"] == PHASE_VALUES[update["phase"]],
            "update phase/payload drift",
        )
        expected = update["before"]
        require(
            set(expected)
            == {
                "boot",
                "peer",
                "peer_present",
                "connection_epoch",
                "bearer_epoch",
                "database_epoch",
                "value_handle",
                "payload_hex",
            },
            "entry route keys",
        )
        require(
            expected["boot"] == doc["boot"]
            and expected["peer_present"] is True
            and all(
                type(expected[k]) is int and 0 < expected[k] <= wire.U32_MAX
                for k in (
                    "connection_epoch",
                    "bearer_epoch",
                    "database_epoch",
                    "value_handle",
                )
            )
            and expected["value_handle"] <= 65535,
            "entry route identity/types",
        )
        if physical is not None:
            require(
                expected["value_handle"] == physical.get("value_handle"),
                "physical target drift",
            )
            boundaries = [
                r for r in physical["boundaries"] if r["boundary"] == update["phase"]
            ]
            if boundaries:
                require(len(boundaries) == 1, "duplicate physical update")
                boundary = boundaries[0]
                local = boundary["local"]["limits_before"]
                require(
                    all(
                        expected[k] == local[k]
                        for k in (
                            "peer",
                            "peer_present",
                            "connection_epoch",
                            "bearer_epoch",
                            "database_epoch",
                        )
                    )
                    and expected["boot"] == local["boot_nonce"]
                    and boundary["submission"]["handle"] == expected["value_handle"]
                    and boundary["submission"]["value"] == expected["payload_hex"],
                    "physical entry route/payload drift",
                )
            else:
                require(not complete, "missing physical update boundary")
        before = decoded[update["before_query"]]
        require(
            isinstance(before, wire.ObservationQuery)
            and before.owner == owner
            and before.enabled
            and before.owner_generation == doc["owner_generation"],
            "update control owner drift",
        )
        require(
            events[update["before_query"]]["phase"] == update["phase"] + ":before-set",
            "before phase drift",
        )
        if update["after_query"] is not None:
            after = decoded[update["after_query"]]
            require(
                events[update["after_query"]]["phase"] == update["phase"] + ":after-set"
                and after.owner == before.owner
                and after.owner_generation == before.owner_generation
                and after.enabled,
                "after identity/phase drift",
            )
            expected_after = dict(
                update["before"], database_epoch=update["before"]["database_epoch"] + 1
            )
            require(
                update["after"] == expected_after
                and before.last_sequence
                <= after.last_sequence
                <= before.last_sequence + 1,
                "SET_VALUE entry/exit drift",
            )
            require(
                update["sequence"]
                == (
                    after.last_sequence
                    if after.last_sequence > before.last_sequence
                    else None
                ),
                "sequence drift",
            )
        require(
            isinstance(update["reads"], list)
            and len(update["reads"]) <= MAX_READS
            and len(set(update["reads"])) == len(update["reads"]),
            "read budget/duplicates",
        )
        for read in update["reads"]:
            require(read not in referenced_reads, "read belongs to multiple updates")
            referenced_reads.add(read)
            require(
                isinstance(decoded.get(read), wire.ObservationRecord)
                and events[read]["phase"]
                in ("poll:" + update["phase"], "FINAL:" + update["phase"])
                and read > update["after_query"],
                "read reference drift",
            )
            validate_record(
                decoded[read],
                update["before"],
                owner,
                before.owner_generation,
                update["sequence"],
            )
        if complete:
            require(
                update["sequence"] is not None and update["reads"], "update unobserved"
            )
            require(
                not any(
                    decoded[r].flags
                    & (
                        wire.RecordFlags.INCOMPLETE
                        | wire.RecordFlags.TRUNCATED
                        | wire.RecordFlags.TRACE_LOST
                        | wire.RecordFlags.PDU_MISMATCH
                    )
                    or decoded[r].trace_loss
                    for r in update["reads"]
                ),
                "incomplete diagnostic record",
            )
    require(
        referenced_reads
        == {i for i, p in decoded.items() if isinstance(p, wire.ObservationRecord)},
        "unbound raw read receipt",
    )
    if complete:
        require(
            doc["status"] == "complete"
            and not doc["primary_errors"]
            and not doc["cleanup_errors"]
            and len(updates) == 3,
            "incomplete requested diagnostic",
        )
        require(
            events[0]["phase"] == "initial-query"
            and any(e["phase"] == "FINAL" for e in events)
            and events[-1]["phase"] == "owned-off"
            and current is not None
            and current.owner == owner
            and not current.enabled,
            "missing FINAL/owned-off",
        )
        initial = decoded[0]
        expected_phases = ["enable", "owned-off"]
        if initial.owner not in (wire.ZERO_OWNER, owner):
            expected_phases = ["handoff-off", "handoff-rotate-off", *expected_phases]
        require(
            [phase for phase, _ in configurations] == expected_phases,
            "control handoff sequence drift",
        )
        for phase, query in configurations:
            require(
                query.owner == (initial.owner if phase == "handoff-off" else owner)
                and query.enabled == (phase == "enable"),
                "handoff target/enable drift",
            )
        require(
            all(
                (
                    p.profile,
                    p.flush_ms,
                    p.batch_flags
                    & (wire.BatchFlags.COMPILED | wire.BatchFlags.AUTO_BATCHING),
                )
                == (
                    initial.profile,
                    initial.flush_ms,
                    initial.batch_flags
                    & (wire.BatchFlags.COMPILED | wire.BatchFlags.AUTO_BATCHING),
                )
                for p in decoded.values()
                if isinstance(p, wire.ObservationQuery)
            ),
            "batching configuration drift",
        )
        require(
            not any(
                isinstance(p, wire.ObservationQuery) and p.state_flags
                for p in decoded.values()
            ),
            "incomplete query history",
        )
