"""Native BLeng iOS system-loss host protocol.

This module owns the host-side state machine for one explicitly authorized
system-loss attempt. It does not access XCUITest, xcrun, a phone, a provider or
RF itself. A live adapter is injected by the future device runner, which keeps
radio mutation and its evidence separate from the protocol rules.

The state machine is intentionally terminal and one-shot:

    pending -> off-ready -> radio-off -> disconnected -> restored-on
             -> recovered -> finished

OFF is never retried. Once mutation is attempted, the caller may make one
bounded ON restoration attempt. An uncertain restoration is quarantined by the
caller and cannot be reported as a pass. A bounded platform timeout or explicit
manual-reconnect result is a distinct terminal outcome: the phone may be safely
restored and archived, but the BLE gate remains failed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

SCOPE = "ios-bleng-system-loss-host"
SCHEMA_VERSION = 1
_OFF = "0"
_ON = "1"
# Provider RF safety remains independently bounded by its ARM-originated budget.
# This larger host budget covers real iOS Settings OFF readback and receipt relay.
MAX_HOST_OFF_BUDGET_SECONDS = 30.0

_TERMINAL_RESULTS = {
    "connection-lost",
    "watchdog-timeout",
    "operation-aborted",
    "backend-error",
}


class SystemLossHostError(RuntimeError):
    """A protocol, identity, deadline or restoration violation."""


class RadioSwitch(Protocol):
    """The deliberately narrow live UI adapter required by the host."""

    def read(self) -> str:
        """Return the verified system switch value, ``0`` or ``1``."""

    def set_off(self) -> None:
        """Perform exactly one verified OFF action."""

    def set_on(self) -> None:
        """Perform exactly one verified ON restoration action."""


@dataclass(frozen=True)
class HostIdentity:
    """The native owner identity that must survive the radio transition."""

    pid: int
    boot_id: str
    owner_id: str
    backend_id: str

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> HostIdentity:
        if set(value) != {"pid", "boot_id", "owner_id", "backend_id"}:
            raise SystemLossHostError("native owner identity schema")
        if type(value["pid"]) is not int or not 0 < value["pid"] <= 2**31 - 1:
            raise SystemLossHostError("native PID")

        def canonical_uuid(name: str) -> bool:
            candidate = value[name]
            try:
                return (
                    type(candidate) is str
                    and str(uuid.UUID(candidate)).lower() == candidate
                )
            except (ValueError, AttributeError):
                return False

        if not all(
            canonical_uuid(name) for name in ("boot_id", "owner_id", "backend_id")
        ):
            raise SystemLossHostError("native owner UUID identity")
        return cls(
            pid=value["pid"],
            boot_id=value["boot_id"],
            owner_id=value["owner_id"],
            backend_id=value["backend_id"],
        )

    def document(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "boot_id": self.boot_id,
            "owner_id": self.owner_id,
            "backend_id": self.backend_id,
        }


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemLossHostError(message)


def _hash(value: Any, name: str) -> None:
    _require(
        type(value) is str
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value),
        f"{name} must be lowercase SHA-256",
    )


def _id(value: Any, name: str) -> str:
    _require(
        type(value) is str
        and value.isascii()
        and value.isdecimal()
        and not value.startswith("0")
        and 0 < int(value) <= 2**64 - 1,
        f"{name} must be a canonical nonzero u64 string",
    )
    return value


def verify_ui_ledger(document: Mapping[str, Any], *, run_id: str) -> str:
    """Validate the narrow XCUITest Settings ledger and return switch value."""
    _require(
        set(document)
        == {
            "schema_version",
            "scope",
            "run_id",
            "status",
            "mutation_attempted",
            "restored",
            "actions",
        },
        "system-loss UI ledger schema",
    )
    _require(
        document["schema_version"] == 1
        and document["scope"] == "ios-bleng-system-loss-ui"
        and document["run_id"] == run_id,
        "foreign system-loss UI ledger",
    )
    _require(
        type(document["actions"]) is list and 1 <= len(document["actions"]) <= 3,
        "UI action bound",
    )
    expected = [("initial-on", "1")]
    if len(document["actions"]) >= 2:
        expected.append(("off", "0"))
    if len(document["actions"]) == 3:
        expected.append(("restored-on", "1"))
    previous = -1.0
    for row, (action, switch) in zip(document["actions"], expected, strict=True):
        _require(
            type(row) is dict
            and set(row) == {"action", "elapsed_seconds", "switch_value"}
            and row["action"] == action
            and row["switch_value"] == switch,
            "UI action order/readback",
        )
        elapsed = row["elapsed_seconds"]
        _require(
            type(elapsed) in (int, float)
            and math.isfinite(elapsed)
            and elapsed >= previous,
            "UI action timing",
        )
        previous = float(elapsed)
    status = document["status"]
    expected_status = (
        "initial-on"
        if len(expected) == 1
        else "off-readback"
        if len(expected) == 2
        else "restored"
    )
    _require(status == expected_status, "UI status/action mismatch")
    _require(document["mutation_attempted"] is (len(expected) >= 2), "UI mutation flag")
    _require(document["restored"] is (len(expected) == 3), "UI restored flag")
    return expected[-1][1]


class FileRadioSwitch:
    """Live host adapter for the existing XCUITest Settings ledger.

    XCUITest owns the actual tap. The host only writes an authorization marker
    and waits for the signed test's readback ledger; it never toggles Settings
    through a second mechanism.
    """

    def __init__(
        self,
        coordination: Path,
        *,
        run_id: str,
        timeout_seconds: float = 300.0,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        refresh: Callable[[], None] | None = None,
    ) -> None:
        self.coordination = coordination
        self.run_id = run_id
        self.timeout_seconds = timeout_seconds
        self.now = now
        self.sleep = sleep
        self.refresh = refresh or (lambda: None)

    def _ledger(self) -> dict[str, Any]:
        path = self.coordination / "system-loss-ui.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise SystemLossHostError(
                "system-loss UI ledger is not readable"
            ) from error
        if not isinstance(value, dict):
            raise SystemLossHostError("system-loss UI ledger is not an object")
        verify_ui_ledger(value, run_id=self.run_id)
        return value

    def read(self) -> str:
        document = self._ledger()
        return verify_ui_ledger(document, run_id=self.run_id)

    def _authorize(self, name: str, action: str) -> None:
        self.coordination.mkdir(parents=True, exist_ok=True)
        marker = {
            "schema_version": 1,
            "run_id": self.run_id,
            "action": action,
        }
        temporary = self.coordination / f".{name}.{self.run_id}.tmp"
        destination = self.coordination / name
        temporary.write_bytes(_canonical(marker))
        temporary.replace(destination)

    def _wait_for(self, expected: str) -> None:
        deadline = self.now() + self.timeout_seconds
        while self.now() < deadline:
            try:
                self.refresh()
                if self.read() == expected:
                    return
            except SystemLossHostError:
                pass
            self.sleep(0.2)
        raise SystemLossHostError(
            f"timed out waiting for UI switch readback {expected}"
        )

    def set_off(self) -> None:
        self._authorize("off-authorized", "off-authorized")
        self._wait_for("0")

    def set_on(self) -> None:
        self._authorize("restore-on-authorized", "restore-on-authorized")
        self._wait_for("1")


class SystemLossHost:
    """One native active-loss attempt; no implicit retry or cleanup verdict."""

    def __init__(
        self,
        *,
        run_id: str,
        case: str,
        identity: Mapping[str, Any],
        switch: RadioSwitch,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        _require(type(run_id) is str and bool(run_id), "run_id is required")
        _require(type(case) is str and bool(case), "system-loss case is required")
        self.run_id = run_id
        self.case = case
        self.identity = HostIdentity.from_document(identity)
        self.switch = switch
        self.now = now
        self.state = "pending"
        self.primary_error: str | None = None
        self.cleanup_errors: list[str] = []
        self.mutation_attempted = False
        self._off_count = 0
        self._on_count = 0
        self._old_commands: tuple[str, ...] = ()
        self._old_link: dict[str, str] = {}
        self._binding: dict[str, str] | None = None
        self._off_deadline: float | None = None
        self._receipt: dict[str, Any] | None = None
        self._receipt_sha256: str | None = None
        self._recovery: dict[str, Any] | None = None
        self._same_session_recovery = False
        self._observations: list[dict[str, Any]] = []

    @property
    def off_count(self) -> int:
        return self._off_count

    @property
    def on_count(self) -> int:
        return self._on_count

    def _observe(self, action: str, *, switch_value: str, **extra: Any) -> None:
        self._require_switch(switch_value)
        row = {
            "action": action,
            "elapsed_seconds": round(max(0.0, self.now() - self._started), 6),
            "switch_value": switch_value,
            **extra,
        }
        self._observations.append(row)

    def _require_switch(self, expected: str) -> None:
        value = self.switch.read()
        _require(
            value == expected,
            f"radio switch readback is {value!r}, expected {expected!r}",
        )

    def bind_pending(
        self,
        *,
        control_sha256: str,
        binding_sha256: str,
        witness_sha256: str,
        old_command_ids: list[str] | tuple[str, ...],
        old_link: Mapping[str, str],
        off_budget_seconds: float,
    ) -> dict[str, Any]:
        """Bind fresh provider/native pending evidence before any radio action."""
        _require(self.state == "pending", "pending evidence can only be bound once")
        for value, name in (
            (control_sha256, "control_sha256"),
            (binding_sha256, "binding_sha256"),
            (witness_sha256, "witness_sha256"),
        ):
            _hash(value, name)
        _require(
            type(off_budget_seconds) in (int, float)
            and math.isfinite(off_budget_seconds)
            and 0 < off_budget_seconds <= MAX_HOST_OFF_BUDGET_SECONDS,
            "OFF budget must be finite and within the host platform budget",
        )
        commands = tuple(_id(value, "old command ID") for value in old_command_ids)
        _require(commands and len(set(commands)) == len(commands), "old command IDs")
        _require(
            set(old_link) == {"connection_id", "gatt_session_id", "subscription_id"},
            "old link schema",
        )
        self._old_link = {
            key: _id(value, f"old {key}") for key, value in old_link.items()
        }
        self._started = self.now()
        self._off_deadline = self._started + float(off_budget_seconds)
        self._old_commands = commands
        self._binding = {
            "control_sha256": control_sha256,
            "binding_sha256": binding_sha256,
            "witness_sha256": witness_sha256,
        }
        self._require_switch(_ON)
        self._observe(
            "pending-bound", switch_value=_ON, binding=copy.deepcopy(self._binding)
        )
        return self.document()

    def authorize_off(self, provider: Mapping[str, Any]) -> dict[str, Any]:
        """Validate provider readiness; this method does not mutate the radio."""
        _require(self.state == "pending", "OFF authorization state")
        _require(self._binding is not None, "pending binding is required")
        _require(
            set(provider)
            == {
                "schema_version",
                "state",
                "radio_authorized",
                "control_sha256",
                "binding_sha256",
                "witness_sha256",
            },
            "provider OFF readiness schema",
        )
        _require(
            provider["schema_version"] == 1
            and provider["state"] == "off-ready"
            and provider["radio_authorized"] is False,
            "provider is not fresh OFF-ready",
        )
        for key in ("control_sha256", "binding_sha256", "witness_sha256"):
            _require(provider[key] == self._binding[key], f"provider {key} drift")
        self._require_deadline()
        self._require_switch(_ON)
        self.state = "off-ready"
        self._observe("off-authorized", switch_value=_ON)
        return self.document()

    def _loss_authorization_document(self) -> dict[str, Any]:
        _require(self._binding is not None, "pending binding is required")
        return {
            "schema_version": 1,
            "session_id": self.run_id,
            "action": "authorize-loss",
            "control_sha256": self._binding["control_sha256"],
            "witness_sha256": self._binding["witness_sha256"],
        }

    def loss_authorization_command(self) -> dict[str, Any]:
        """Authorize App loss-terminal observation without mutating the radio.

        This phase is deliberately separate from provider OFF authorization:
        the App must consume it while foreground before the short provider
        disconnect-retirement window begins.
        """
        _require(self.state == "pending", "loss authorization command state")
        return self._loss_authorization_document()

    def authorization_command(self) -> dict[str, Any]:
        """Return the immutable App relay command after provider readiness."""
        _require(self.state == "off-ready", "loss authorization command state")
        return self._loss_authorization_document()

    def _require_deadline(self) -> None:
        _require(self._off_deadline is not None, "OFF deadline is not bound")
        _require(self.now() < self._off_deadline, "absolute OFF deadline expired")

    def perform_off(self) -> dict[str, Any]:
        """Perform one OFF action; a failed/unknown result is terminal."""
        _require(self.state == "off-ready", "OFF action is not authorized")
        self._require_deadline()
        _require(self._off_count == 0, "OFF retry is forbidden")
        self._off_count += 1
        self.mutation_attempted = True
        try:
            self.switch.set_off()
            self._require_switch(_OFF)
        except BaseException as error:
            self.primary_error = f"OFF action: {error}"
            self.state = "radio-unknown"
            raise SystemLossHostError(self.primary_error) from error
        self.state = "radio-off"
        self._observe("radio-off", switch_value=_OFF)
        return self.document()

    def record_disconnect(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Accept only the retained provider disconnect receipt."""
        _require(self.state == "radio-off", "disconnect receipt state")
        self._require_deadline()
        _require(
            set(receipt)
            == {
                "schema_version",
                "state",
                "outcome",
                "control_sha256",
                "binding_sha256",
                "witness_sha256",
            },
            "disconnect receipt schema",
        )
        _require(
            receipt["schema_version"] == 1
            and receipt["state"] == "disconnected-retired"
            and receipt["outcome"] == "disconnected-uncommitted",
            "disconnect must be a retained uncommitted loss receipt",
        )
        for key in ("control_sha256", "binding_sha256", "witness_sha256"):
            _require(receipt[key] == self._binding[key], f"disconnect {key} drift")
        self._receipt = dict(receipt)
        self._receipt_sha256 = _digest(receipt)
        self.state = "disconnected"
        self._observe(
            "provider-disconnected-retired", switch_value=_OFF, receipt=dict(receipt)
        )
        return self.document()

    def restore_on(self) -> dict[str, Any]:
        """Make one bounded ON restoration attempt; never retries an uncertain ON."""
        _require(
            self.state in {"radio-unknown", "disconnected"}, "ON restoration state"
        )
        _require(self._on_count == 0, "ON restoration retry is forbidden")
        self._on_count += 1
        try:
            self.switch.set_on()
            self._require_switch(_ON)
        except BaseException as error:
            self.cleanup_errors.append(f"ON restoration: {error}")
            self.state = "quarantined"
            raise SystemLossHostError(self.cleanup_errors[-1]) from error
        self.state = "restored-on"
        self._observe("radio-restored-on", switch_value=_ON)
        return self.document()

    def restoration_command(self, terminals_sha256: str) -> dict[str, Any]:
        """Return the command that lets the App continue on the restored radio."""
        _require(self.state == "restored-on", "radio restoration command state")
        _hash(terminals_sha256, "terminals_sha256")
        _require(self._binding is not None, "pending binding is required")
        _require(self._receipt_sha256 is not None, "disconnect receipt is required")
        return {
            "schema_version": 1,
            "session_id": self.run_id,
            "action": "restore-on",
            "control_sha256": self._binding["control_sha256"],
            "witness_sha256": self._binding["witness_sha256"],
            "terminals_sha256": terminals_sha256,
            "disconnect_receipt_sha256": self._receipt_sha256,
        }

    def record_recovery(
        self,
        *,
        identity: Mapping[str, Any],
        old_terminals: list[Mapping[str, Any]],
        new_link: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate same-owner retirement and a genuinely new recovered link."""
        _require(self.state == "restored-on", "recovery state")
        _require(
            HostIdentity.from_document(identity) == self.identity,
            "native owner identity changed",
        )
        _require(
            len(old_terminals) == len(self._old_commands),
            "each old operation must have exactly one terminal",
        )
        seen: list[str] = []
        for terminal in old_terminals:
            _require(set(terminal) == {"command_id", "result"}, "old terminal schema")
            command = _id(terminal["command_id"], "terminal command ID")
            _require(
                command in self._old_commands and command not in seen,
                "old terminal identity",
            )
            _require(terminal["result"] in _TERMINAL_RESULTS, "old terminal result")
            seen.append(command)
        _require(set(seen) == set(self._old_commands), "old operation terminal set")
        _require(
            set(new_link) == {"connection_id", "gatt_session_id", "subscription_id"},
            "new recovery link schema",
        )
        new_link_ids = {key: _id(new_link[key], key) for key in new_link}
        for key, value in new_link_ids.items():
            _require(value != self._old_link[key], f"recovery reused old {key}")
        self._recovery = {
            "identity": self.identity.document(),
            "old_terminals": [dict(row) for row in old_terminals],
            "new_link": dict(new_link),
        }
        self._same_session_recovery = True
        self.state = "recovered"
        self._observe(
            "same-owner-recovered",
            switch_value=_ON,
            recovery=copy.deepcopy(self._recovery),
        )
        return self.document()

    def record_fresh_owner_recovery(
        self,
        *,
        old_identity: Mapping[str, Any],
        new_identity: Mapping[str, Any],
        old_terminals: list[Mapping[str, Any]],
        new_link: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate a same-process recovery with a newly-created owner/backend."""
        _require(self.state == "restored-on", "fresh-owner recovery state")
        _require(
            HostIdentity.from_document(old_identity) == self.identity,
            "fresh-owner old identity changed",
        )
        new = HostIdentity.from_document(new_identity)
        _require(
            new.pid == self.identity.pid and new.boot_id == self.identity.boot_id,
            "fresh-owner recovery changed process identity",
        )
        _require(
            new.owner_id != self.identity.owner_id
            and new.backend_id != self.identity.backend_id,
            "fresh-owner recovery reused owner/backend identity",
        )
        _require(
            len(old_terminals) == len(self._old_commands),
            "each old operation must have exactly one terminal",
        )
        seen: list[str] = []
        for terminal in old_terminals:
            _require(set(terminal) == {"command_id", "result"}, "old terminal schema")
            command = _id(terminal["command_id"], "terminal command ID")
            _require(
                command in self._old_commands and command not in seen,
                "old terminal identity",
            )
            _require(terminal["result"] in _TERMINAL_RESULTS, "old terminal result")
            seen.append(command)
        _require(set(seen) == set(self._old_commands), "old operation terminal set")
        _require(
            set(new_link) == {"connection_id", "gatt_session_id", "subscription_id"},
            "fresh-owner recovery link schema",
        )
        new_link_ids = {key: _id(new_link[key], key) for key in new_link}
        for key, value in new_link_ids.items():
            _require(value != self._old_link[key], f"fresh-owner reused old {key}")
        self._recovery = {
            "recovery_mode": "fresh-owner",
            "old_identity": dict(old_identity),
            "new_identity": dict(new_identity),
            "old_terminals": [dict(row) for row in old_terminals],
            "new_link": dict(new_link),
        }
        self._same_session_recovery = False
        self.state = "fresh-recovered"
        self._observe(
            "fresh-owner-recovered",
            switch_value=_ON,
            recovery=copy.deepcopy(self._recovery),
        )
        return self.document()

    def finish(self) -> dict[str, Any]:
        _require(
            self.state in {"recovered", "fresh-recovered"},
            "cannot finish before BLE recovery",
        )
        self.state = "finished"
        return self.document()

    def record_recovery_terminal(
        self,
        *,
        outcome: str,
        reason: str,
        diagnostic: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a bounded non-success recovery result after ON restoration.

        This is deliberately not ``finish``: a platform limitation or an
        explicit manual reconnect requirement must remain a failed BLE gate.
        It is nevertheless a valid terminal host observation when the radio is
        already verified ON, so lease cleanup can archive it instead of
        misclassifying a safely restored phone as quarantined.
        """
        _require(self.state == "restored-on", "recovery terminal state")
        _require(
            outcome in {"platform-timeout", "manual-reconnect-required"},
            "unsupported recovery terminal outcome",
        )
        _require(type(reason) is str and bool(reason), "recovery terminal reason")
        if diagnostic is not None:
            _require(
                diagnostic.get("schema_version") == 1
                and diagnostic.get("state") == "recovery-diagnostic"
                and diagnostic.get("session_id") == self.run_id
                and diagnostic.get("control_sha256")
                == (self._binding or {}).get("control_sha256")
                and diagnostic.get("witness_sha256")
                == (self._binding or {}).get("witness_sha256")
                and diagnostic.get("recovery_outcome") == outcome
                and diagnostic.get("recovery_failure_reason") == reason,
                "recovery diagnostic identity or outcome drift",
            )
            observed_identity = diagnostic.get("owner_identity")
            _require(
                isinstance(observed_identity, Mapping)
                and HostIdentity.from_document(observed_identity) == self.identity,
                "recovery diagnostic owner identity drift",
            )
        self._recovery = {
            "recovery_outcome": outcome,
            "recovery_failure_reason": reason,
            "diagnostic": copy.deepcopy(dict(diagnostic))
            if diagnostic is not None
            else None,
        }
        self.primary_error = f"{outcome}: {reason}"
        self.state = outcome
        self._observe(
            outcome,
            switch_value=_ON,
            recovery=copy.deepcopy(self._recovery),
        )
        return self.document()

    def fail(self, error: BaseException | str) -> dict[str, Any]:
        """Retain the primary error without silently restoring or passing the gate."""
        if self.primary_error is None:
            self.primary_error = str(error)
        if self.state not in {
            "finished",
            "quarantined",
            "platform-timeout",
            "manual-reconnect-required",
        }:
            self.state = "failed"
        return self.document()

    def lease_control(self, fixture_receipt_sha256: str) -> dict[str, Any]:
        """Project this attempt into the shared iOS lease restoration schema."""
        _hash(fixture_receipt_sha256, "fixture_receipt_sha256")
        passed = self.state == "finished"
        mutated = self.mutation_attempted
        restored = self.state in {
            "restored-on",
            "recovered",
            "fresh-recovered",
            "platform-timeout",
            "manual-reconnect-required",
            "finished",
        }
        same_session = self._same_session_recovery and self.state in {
            "recovered",
            "finished",
        }
        observations = [
            {
                "action": row["action"],
                "elapsed_seconds": row["elapsed_seconds"],
                "identity": None,
                "switch_value": row["switch_value"],
            }
            for row in self._observations
        ]
        return {
            "schema_version": 1,
            "scope": "ios-bleng-system-loss-control",
            "run_id": self.run_id,
            "fixture_receipt_sha256": fixture_receipt_sha256,
            "status": "passed" if passed else "failed",
            "mutation_attempted": mutated,
            "restored": restored,
            "same_session": same_session,
            "observations": observations,
            "primary_error": self.primary_error
            or ("attempt did not reach recovery" if not passed else ""),
            "cleanup_error": "; ".join(self.cleanup_errors),
        }

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": SCOPE,
            "run_id": self.run_id,
            "case": self.case,
            "state": self.state,
            "identity": self.identity.document(),
            "binding": copy.deepcopy(self._binding),
            "mutation_attempted": self.mutation_attempted,
            "off_count": self._off_count,
            "on_count": self._on_count,
            "observations": copy.deepcopy(self._observations),
            "disconnect_receipt": copy.deepcopy(self._receipt),
            "recovery": copy.deepcopy(self._recovery),
            "primary_error": self.primary_error,
            "cleanup_errors": list(self.cleanup_errors),
        }

    def json_bytes(self) -> bytes:
        return _canonical(self.document())

    def digest(self) -> str:
        return hashlib.sha256(self.json_bytes()).hexdigest()
