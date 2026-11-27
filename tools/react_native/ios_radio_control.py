"""iOS radio-control observation vocabulary and audit; no I/O, RF or cleanup claim.

The iOS radio lease persists cooperative UI control observations around an
adapter mutation. This module owns *what a complete control observation must
look like* — the ordered step vocabulary, the native/JS identity binding and the
exact-schema audit — so the lease does not depend on a retired scenario ledger
for its own contract.

Identities are cooperative UI source records, not independent process
provenance. Settings/runtime source evidence still needs binding in the final
bundle, and a passing audit is not a physical RF or cleanup guarantee.
"""

from __future__ import annotations

import copy
import math
import re
import unicodedata
from typing import Any
from urllib.parse import unquote

from tools.react_native.evidence_io import EvidenceError, require
from tools.react_native.host_contract import BUNDLE_ID

_UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
_NONCE = re.compile(r"[0-9a-z]+-[0-9a-z]+-[0-9a-z]+")

# (action, requires the same runtime identity, Settings Switch readback).
CONTROL_STEPS = (
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


def require_uuid(value: Any, label: str) -> None:
    require(type(value) is str and _UUID.fullmatch(value) is not None, label)


def require_executable(value: Any) -> None:
    require(
        type(value) is str
        and re.fullmatch(r"file:///[^\s?#\\]+", value) is not None
        and re.search(r"%(?![0-9a-fA-F]{2})", value) is None,
        "native executable must be a local file URL",
    )
    try:
        decoded = unquote(value, errors="strict")
    except UnicodeError as error:
        raise EvidenceError("native executable URL encoding") from error
    require(
        not decoded.endswith("/")
        and not any(unicodedata.category(char) in {"Cc", "Cf"} for char in decoded),
        "native executable file URL path/control characters",
    )


def require_native_identity(boot: Any, run_id: str) -> None:
    require(
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
    require_uuid(boot["boot_id"], "canonical native boot_id required")
    require(
        boot["run_id"] == run_id
        and boot["adapter_mode"] == "startup-off"
        and type(boot["pid"]) is int
        and 0 < boot["pid"] <= 2_147_483_647
        and boot["bundle_id"] == BUNDLE_ID
        and boot["source"] == "launch",
        "native bootstrap identity/mode/PID/bundle/source",
    )
    require_executable(boot["executable"])


def require_nonce(value: Any) -> None:
    require(
        type(value) is str
        and len(value) <= 128
        and _NONCE.fullmatch(value) is not None,
        "runtime canonical three-part base36 nonce",
    )


def identity(value: Any, run_id: str) -> dict:
    require(
        type(value) is dict and set(value) == {"native", "run_id", "nonce"},
        "startup-off identity exact schema",
    )
    require(value["run_id"] == run_id, "startup-off identity run_id")
    require_native_identity(value["native"], run_id)
    require_nonce(value["nonce"])
    return value


def require_receipt_sha256(value: Any) -> None:
    require(
        type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        "canonical fixture receipt SHA-256 required",
    )


def verify_control(control: dict, *, runtime: dict, receipt_sha256: str) -> dict:
    """Audit only a complete successful cooperative UI control observation.

    ``runtime`` is the separately audited runtime ledger. Identity fields are UI
    source records bound to that ledger, not independent process provenance.
    Failed attempts (including cleanup-restored-on) never become passing audits.
    """
    require(type(runtime) is dict, "startup-off runtime must be an object")
    run_id = runtime.get("run_id")
    require_uuid(run_id, "canonical runtime run_id required")
    observed_identity = identity(
        {
            "native": runtime.get("native_bootstrap"),
            "run_id": run_id,
            "nonce": runtime.get("runtime_nonce"),
        },
        run_id,
    )
    require_receipt_sha256(receipt_sha256)
    require(
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
    require(
        type(control["schema_version"]) is int
        and control["schema_version"] == 1
        and control["scope"] == "ios-adapter-startup-control-window"
        and control["run_id"] == run_id
        and control["fixture_receipt_sha256"] == receipt_sha256,
        "startup-off control version/scope/run/receipt",
    )
    require(
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
    require(
        type(observations) is list and len(observations) == len(CONTROL_STEPS),
        "startup-off control requires exactly twelve observations",
    )
    previous = 0
    for observation, (action, identified, switch) in zip(observations, CONTROL_STEPS):
        require(
            type(observation) is dict
            and set(observation)
            == {"action", "elapsed_seconds", "identity", "switch_value"},
            "startup-off control observation exact schema",
        )
        require(
            observation["action"] == action and observation["switch_value"] == switch,
            f"startup-off control action/switch: {action}",
        )
        elapsed = observation["elapsed_seconds"]
        require(
            (type(elapsed) is int or type(elapsed) is float and math.isfinite(elapsed))
            and elapsed >= previous,
            f"startup-off control finite nonnegative nondecreasing elapsed: {action}",
        )
        previous = elapsed
        if identified:
            observed = identity(observation["identity"], run_id)
            require(
                observed == observed_identity, f"startup-off control identity: {action}"
            )
        else:
            require(
                observation["identity"] is None,
                f"startup-off control unexpected identity: {action}",
            )
    return {
        "schema_version": 1,
        "scope": "cooperative-control-observations-only",
        "control_status": "passed",
        "run_id": run_id,
        "fixture_receipt_sha256": receipt_sha256,
        "identity": copy.deepcopy(observed_identity),
        "observations": copy.deepcopy(observations),
        "explicit_limits": {
            "identity": "UI-sourced native PID/boot and JS nonce binding only, not independent PID provenance.",
            "evidence": "Settings UI and runtime source evidence still require final bundle binding; no physical RF or overall bundle verdict.",
            "cleanup": "Cooperative restored observation only, not a cleanup guarantee.",
            "timing": "Nondecreasing elapsed observations only, not deadlines or absolute cross-clock correlation.",
        },
    }
