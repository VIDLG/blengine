"""RN-only source samples, not complete bundles, live authority or RF evidence.

The Swift-exported golden journal remains unchanged. Derived documents below are
new synthetic inputs. Expected UI steps are independent of the production ledger:
never generate a test oracle from that verifier's private transition table.
"""

from __future__ import annotations

import copy
import json
import plistlib
from pathlib import Path

from tools.react_native.host_contract import BUNDLE_ID, SCHEME

ROOT = Path(__file__).resolve().parents[4]
DEVICE = "00008110-001234567890001E"  # Synthetic physical-shaped ID; never queried.
RUN = "12345678-1234-4234-8234-123456789abc"
SERVICE = "11111111-1111-4111-8111-111111111111"
_PREFIX = "BLEHUB_NITRO_RUNTIME rf-adapter-transition:"
_UI_SOURCE_STEPS = (
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


def put(path: Path, value: object) -> bytes:
    """Write caller-supplied bytes unchanged or deterministic synthetic JSON."""
    raw = value if type(value) is bytes else json.dumps(value, indent=2).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def fake_app(root: Path) -> Path:
    """Minimal artifact-validator input; not an executable or signed App."""
    app = root / "BleHubNitroHost.app"
    app.mkdir()
    put(
        app / "Info.plist",
        plistlib.dumps({"CFBundleExecutable": SCHEME, "CFBundleIdentifier": BUNDLE_ID}),
    )
    put(
        app / SCHEME,
        bytes.fromhex("cffaedfe") + b"synthetic native; not executable evidence",
    )
    (app / SCHEME).chmod(0o755)
    put(app / "main.jsbundle", b"synthetic JS; not RF")
    return app


def journal_document(
    run_id: str = RUN,
    *,
    service_uuid: str = SERVICE,
    native_pid: int | None = None,
    boot_id: str | None = None,
    nonce: str | None = None,
    executable: str | None = None,
) -> dict:
    """Derive independent identity axes without rewriting the golden attachment.

    Native JSON is changed structurally, separately from ON service reports. A
    service UUID equal to the original sample's boot UUID must not alias them.
    """
    source = json.loads(
        (ROOT / "tests/support/rf_lab/ios_adapter_startup_journal.json").read_bytes()
    )
    records = []
    for original in source["records"]:
        record = original.replace(RUN, run_id)
        body = record.removeprefix(_PREFIX)
        if body.startswith("native:"):
            _, family, payload = body.split(":", 2)
            native = json.loads(payload)
            if native_pid is not None:
                native["pid"] = native_pid
            if boot_id is not None:
                native["boot_id"] = boot_id
            if nonce is not None and "nonce" in native:
                native["nonce"] = nonce
            if executable is not None:
                native["executable"] = executable
            record = (
                _PREFIX
                + "native:"
                + family
                + ":"
                + json.dumps(native, separators=(",", ":"))
            )
        elif body.startswith("runtime:") and nonce is not None:
            record = record.replace(":nonce=boot-123-abc:", ":nonce=" + nonce + ":")
        elif body.startswith("on:flow:"):
            record = record.replace(SERVICE, service_uuid)
        records.append(record)
    return {**source, "run_id": run_id, "records": records}


def session_identity(journal: dict) -> dict:
    """Project source identity without consulting the semantic verifier."""
    native = json.loads(
        journal["records"][0].removeprefix(_PREFIX + "native:bootstrap:")
    )
    start = journal["records"][1]
    nonce = start.split(":nonce=", 1)[1].removesuffix(":phase=start")
    return {"native": native, "run_id": journal["run_id"], "nonce": nonce}


def passed_control(identity: dict, receipt_sha256: str) -> dict:
    """One independently authored UI source sequence; each call owns its data."""
    return {
        "schema_version": 1,
        "scope": "ios-adapter-startup-control-window",
        "run_id": identity["run_id"],
        "fixture_receipt_sha256": receipt_sha256,
        "status": "passed",
        "mutation_attempted": True,
        "restored": True,
        "same_session": True,
        "observations": [
            {
                "action": action,
                "elapsed_seconds": index / 2,
                "identity": copy.deepcopy(identity) if identified else None,
                "switch_value": switch,
            }
            for index, (action, identified, switch) in enumerate(_UI_SOURCE_STEPS)
        ],
        "primary_error": "",
        "cleanup_error": "",
    }


def summary_document(device: str = DEVICE) -> dict:
    """Synthetic successful single-method XCTest summary, not test execution."""
    counts = {"passedTests": 1, "failedTests": 0, "skippedTests": 0}
    return {
        **counts,
        "totalTestCount": 1,
        "result": "Passed",
        "testFailures": [],
        "devicesAndConfigurations": [{**counts, "device": {"deviceId": device}}],
    }
