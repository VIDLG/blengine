"""Semantic composition gate; raw lineage is retained, never synthesized as capture."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict
from pathlib import Path

from tests.peripherals.nrf52840 import fixture_session as session
from tests.peripherals.nrf52840 import full_suite as protocol
from tests.peripherals.nrf52840 import recovery_ten as recovery
from tests.peripherals.nrf52840.cancellation_control import (
    load_suite_controls,
    read_document,
    write_document,
)
from tools.react_native import check_runtime_log, recovery_ten_ledger
from tools.react_native.host_contract import ANDROID_PACKAGE, BUNDLE_ID, SCHEME

same = recovery_ten_ledger._same
require = recovery_ten_ledger._require
PREFIX = protocol.PREFIX


def begin_url(run_id: str) -> str:
    return f"blehub-rn-spike://recover/{run_id}/begin"


def native_identity(value: dict) -> None:
    require(type(value.get("pid")) is int and value["pid"] > 0, "positive native PID")
    require(str(uuid.UUID(value["boot_id"])) == value["boot_id"], "native boot UUID")
    require(value.get("bundle_id") in (ANDROID_PACKAGE, BUNDLE_ID), "native bundle")
    require(
        type(value.get("executable")) is str
        and value["executable"].startswith("file:"),
        "native executable URI",
    )


def normalized(text: str) -> list[str]:
    lines = text.splitlines()
    require(text.endswith("\n") and bool(lines), "complete runtime lines")
    require(all(l.count(PREFIX) == 1 for l in lines), "unambiguous runtime line")
    return [l[l.index(PREFIX) :] for l in lines]


def recovery_projection(text: str) -> str:
    return "".join(l + "\n" for l in text.splitlines() if recovery.PREFIX in l)


def _binding(path: Path) -> dict:
    raw = session._read_bytes(path)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def check_base_identity(text: str, log: Path, root: Path) -> None:
    """Full owner's strict sidecar format, used by the unchanged base checker."""
    identity = read_document(log.with_suffix(".identity.json"))
    capture = read_document(log.with_suffix(".capture.json"))
    same(identity.get("scenario"), "full-suite", "capture owner scenario")
    same(capture.get("scenario"), "full-suite", "capture scenario")
    require(
        identity.get("status") in ("pinned", "passed")
        and capture.get("status") in ("running", "passed"),
        "live/full capture status",
    )
    same(capture.get("base_status"), "passed", "base gate readiness")
    controls = load_suite_controls(root)
    boot = capture.get("control_bootstrap")
    require(type(boot) is dict, "control native bootstrap")
    native_identity(boot)
    expected = {
        **asdict(controls[0]),
        **{k: identity.get(k) for k in ("pid", "executable")},
        "bundle_id": ANDROID_PACKAGE
        if identity.get("platform") == "android"
        else BUNDLE_ID,
        "boot_id": (identity.get("bootstrap") or {}).get("boot_id"),
    }
    same(boot, expected, "control/capture native bootstrap binding")
    same(
        identity.get("bootstrap"),
        {
            "run_id": controls[0].session,
            "source": "launch",
            **{k: boot[k] for k in ("pid", "boot_id", "bundle_id", "executable")},
        },
        "shared recovery bootstrap",
    )
    schedule = [(c, p) for c in controls for p in c.phases]
    actions = capture.get("control_actions")
    require(
        type(actions) is list and len(actions) == len(schedule),
        "seven base native controls",
    )
    signals = []
    for action, (control, phase) in zip(actions, schedule, strict=True):
        same(action.get("control"), asdict(control), "control action identity")
        same(action.get("phase"), phase, "control action phase")
        _ack_times(action)
        signals.append({**boot, **asdict(control), "phase": phase})
    same(capture.get("control_signals"), signals, "native control ACK schedule")
    same(
        capture.get("control_js_acknowledged"),
        [c.marker("observed", p) for c, p in schedule],
        "JS control ACK schedule",
    )
    records = []
    for line in normalized(text):
        marker = line.removeprefix(PREFIX)
        if marker.startswith("rf-host:"):
            kind, payload = marker.removeprefix("rf-host:").split(":", 1)
            records.append((kind, json.loads(payload)))
    same(
        records,
        [("bootstrap", boot), *[("signal", s) for s in signals]],
        "raw native control records",
    )
    raw = log.with_suffix(".raw.log").read_text(encoding="utf-8")
    source = [l for l in raw.splitlines() if PREFIX in l]
    same(text.splitlines(), source[: len(text.splitlines())], "base raw stream lineage")
    command = capture.get("command")
    require(
        type(command) is list
        and type(capture.get("capture_pid")) is int
        and capture["capture_pid"] > 0,
        "one owned capture child",
    )
    if identity["platform"] == "android":
        require(
            command.count(f"--pid={identity['pid']}") == 1 and "logcat" in command,
            "pinned logcat command",
        )
        from tools.react_native.capture_io import THREADTIME

        for line in text.splitlines():
            header = THREADTIME.match(line)
            require(
                header is not None and int(header[1]) == identity["pid"],
                "raw Android PID lineage",
            )
    else:
        require(
            command.count("--console") == 1
            and command.count("--terminate-existing") == 1,
            "one initial iOS console launch",
        )
        from tools.react_native.ios_sdk import official_result

        installed = official_result(
            Path(identity["install_json"]),
            "devicectl.device.install.app",
            identity["device"],
        )
        rows = installed.get("installedApplications")
        require(
            type(rows) is list and all(type(r) is dict for r in rows),
            "official installed applications",
        )
        matches = [r for r in rows if r.get("bundleID") == BUNDLE_ID]
        require(len(matches) == 1, "unique official installed host")
        same(
            matches[0].get("installationURL", "") + SCHEME,
            identity["executable"],
            "installed host executable URI",
        )
        same(
            installed["deviceIdentifier"],
            identity["device_identifier"],
            "official install device",
        )
        for boundary in ("bootstrap", "begin-before"):
            path = log.with_suffix(f".{boundary}-processes.json")
            # begin-before is queried after the pre-begin base check, but is
            # mandatory at the final parent gate.
            if boundary == "begin-before" and capture["begin"] is None:
                continue
            result = official_result(
                path, "devicectl.device.info.processes", identity["device"]
            )
            same(
                result["deviceIdentifier"],
                identity["device_identifier"],
                "official process device",
            )
            matches = [
                r
                for r in result["runningProcesses"]
                if r.get("processIdentifier") == identity["pid"]
            ]
            require(
                len(matches) == 1
                and matches[0].get("executable") == identity["executable"],
                "official native process identity",
            )


def _ack_times(action: dict) -> None:
    times = [
        recovery_ten_ledger._time(action.get(k))
        for k in ("requested_at", "native_ack_at", "js_ack_at")
    ]
    require(
        times[0] <= times[1] <= times[2] < times[0] + recovery.ACK_SECONDS,
        "original hard 2s native/JS ACK deadline",
    )


def check_base(text: str, log: Path, root: Path, service: str, local_name: str) -> dict:
    lines = text.splitlines()
    summary = PREFIX + protocol.BASE_PASSED
    matches = [i for i, l in enumerate(normalized(text)) if l == summary]
    require(len(matches) == 1, "one exact base suite terminal")
    base = "".join(l + "\n" for l in lines[: matches[0] + 1])
    check_runtime_log.check_rf_lab_suite(
        base,
        service,
        log,
        expected_local_name=local_name,
        cancellation_evidence=root
        / "flow-8-rf-cancellation"
        / "physical-evidence.json",
    )
    runtime = re.findall(
        r"rf-lab-suite:runtime:session=([^:]+):nonce=([^:]+):phase=start", base
    )
    owner = read_document(log.with_suffix(".identity.json"))
    return {
        "schema_version": 1,
        "status": "passed",
        "run_id": runtime[0][0],
        "session_id": runtime[0][0],
        "runtime_nonce": runtime[0][1],
        "native_bootstrap": owner["bootstrap"],
        "service_uuid": service,
        "flows": list(protocol.MODES),
        "runtime_sha256": hashlib.sha256(base.encode()).hexdigest(),
        "physical": [
            _binding(root / f"flow-{i}-{m}" / "physical-evidence.json")
            for i, m in ((8, "rf-cancellation"), (9, "rf-watchdog"))
        ],
    }


def check_retired_fault(boundary: dict, document: dict) -> None:
    from tests.peripherals.nrf52840.fault_control import (
        Outcome,
        Winner,
        cleanup_ready,
        decode_status,
        encode_status,
        ticket_from_document,
    )

    ticket = ticket_from_document(document)
    raw = boundary.get("status_wire")
    require(type(raw) is str, "retained STATUS wire required")
    status = decode_status(bytes.fromhex(raw))
    same(encode_status(status).hex(), raw, "canonical retained STATUS")
    same(status.ticket, ticket, "retained STATUS exact ticket")
    require(
        cleanup_ready(status)
        and status.winner is Winner.DISCONNECT
        and status.outcome is Outcome.DISCONNECTED_UNCOMMITTED,
        "retired/resource-zero/rearmable disconnected handoff",
    )


def check_handoff(
    value: dict, run_id: str, identity: tuple[int, int], root: Path
) -> None:
    for key, expected in {
        "schema_version": 1,
        "status": "ready",
        "run_id": run_id,
        "fixture_identity": {"pid": identity[0], "pgid": identity[1]},
        "hold_inactive": True,
        "peer_count": 0,
        "advertising_stop_confirmed": True,
        "canonical_values_restored": True,
        "recovery_url": begin_url(run_id),
    }.items():
        same(value.get(key), expected, f"handoff {key}")
    plan = session.load_session(root / "fixture-session" / session.REQUEST_NAME)
    same(plan.session_id, run_id, "handoff session")
    session.verify_receipt(plan, fixture_identity=identity)
    profile = session.load_session_profile(plan.contract, plan.profile_path)
    watchdog = read_document(root / "flow-9-rf-watchdog" / "physical-evidence.json")
    same(
        (watchdog["fixture_pid"], watchdog["fixture_pgid"]),
        identity,
        "handoff/watchdog owner",
    )
    armed = [b for b in watchdog["boundaries"] if b["boundary"] == "armed"]
    require(len(armed) == 1, "one watchdog hold generation")
    same(
        value.get("fault_ticket"),
        {k: armed[0][k] for k in ("wire_hex", "sequence", "handle")},
        "handoff owned hold generation",
    )
    handles = {v["role"]: v["handle"] for v in watchdog["canonical_values"]}
    same(
        value.get("canonical_values"),
        [
            {
                "role": c.role,
                "handle": handles[c.role],
                "value": c.initial_value.hex(),
                "readback": c.initial_value.hex() if c.permissions & 1 else None,
            }
            for c in profile.characteristics
        ],
        "handoff canonical profile/handles/readbacks",
    )
    boundaries = value.get("boundaries")
    require(
        type(boundaries) is list and bool(boundaries), "handoff physical boundaries"
    )
    names = [b.get("boundary") for b in boundaries]
    require(
        names[0] == "base-physical-closed" and "fresh-idle" in names,
        "handoff physical idle boundaries",
    )
    times = [recovery_ten_ledger._time(b.get("monotonic")) for b in boundaries]
    require(times == sorted(times), "handoff physical boundary order")
    require(
        times[0]
        >= max(
            recovery_ten_ledger._time(b.get("monotonic"))
            for b in watchdog["boundaries"]
        ),
        "handoff must follow watchdog physical close on the same provider clock",
    )
    for b in boundaries:
        if b["boundary"] == "fresh-idle":
            same(
                b.get("fault_ticket"),
                value["fault_ticket"],
                "fresh idle hold identity",
            )
            same(b.get("peer_count"), 0, "fresh idle peer count")
            check_retired_fault(b, value["fault_ticket"])


def check_final_physical(
    value: dict,
    handoff: dict | None,
    root: Path,
    run_id: str,
    identity: tuple[int, int],
) -> None:
    require(handoff is not None, "physical final requires handoff")
    check_handoff(handoff, run_id, identity, root)
    expected = {
        "schema_version": 1,
        "mode": protocol.MODE,
        "run_id": run_id,
        "status": "passed",
        "recovery_status": "passed",
        "fixture_identity": {"pid": identity[0], "pgid": identity[1]},
        "handoff_path": protocol.HANDOFF_NAME,
        "recovery_physical_path": f"recovery/{recovery.PHYSICAL_NAME}",
        "base_physical_paths": [
            f"flow-{i}-{m}/physical-evidence.json"
            for i, m in ((8, "rf-cancellation"), (9, "rf-watchdog"))
        ],
        "primary_error": None,
        "cleanup_errors": [],
    }
    for key, expected_value in expected.items():
        same(value.get(key), expected_value, f"full physical {key}")
    child = read_document(root / "recovery" / recovery.PHYSICAL_NAME)
    same(
        value.get("handles"),
        child.get("handles"),
        "same handles across physical suites",
    )
    same(
        value.get("handles"),
        {v["role"]: v["handle"] for v in handoff["canonical_values"]},
        "handoff/final physical handles",
    )
    boundaries = value.get("boundaries")
    require(
        type(boundaries) is list and len(boundaries) >= 2, "full physical boundaries"
    )
    count = len(handoff["boundaries"])
    same(boundaries[:count], handoff["boundaries"], "immutable handoff boundary prefix")
    tail = boundaries[count:]
    require(len(tail) >= 3, "handoff ready, fresh idle and begin authorization")
    same(tail[0].get("boundary"), "handoff-ready", "physical handoff ready")
    same(tail[-1].get("boundary"), "begin-authorized", "physical begin authorization")
    for boundary in tail[1:-1]:
        same(boundary.get("boundary"), "fresh-idle", "pre-begin physical idle check")
        same(
            boundary.get("fault_ticket"),
            handoff["fault_ticket"],
            "pre-begin owned hold",
        )
        same(boundary.get("peer_count"), 0, "pre-begin no physical peer")
        check_retired_fault(boundary, handoff["fault_ticket"])
    times = [recovery_ten_ledger._time(b.get("monotonic")) for b in boundaries]
    require(times == sorted(times), "physical begin order")


def verify(
    text: str,
    *,
    log: Path,
    platform: str,
    run_id: str,
    service_uuid: str,
    fixture_request: Path,
    fixture_identity: tuple[int, int],
    host_cycles: tuple[int, ...],
) -> dict:
    root = fixture_request.parent.parent
    plan = session.load_session(fixture_request)
    receipt = session.verify_receipt(plan, fixture_identity=fixture_identity)
    profile = session.load_session_profile(plan.contract, plan.profile_path)
    base = check_base(text, log, root, service_uuid, profile.local_name)
    recovery_text = recovery_projection(text)
    same(
        (log.parent / "recovery-ten-runtime.log").read_text(encoding="utf-8"),
        recovery_text,
        "raw recovery projection",
    )
    same(
        (root / "recovery" / "runtime.log").read_text(encoding="utf-8"),
        "".join(l + "\n" for l in normalized(recovery_text)),
        "provider recovery projection",
    )
    same(
        (root / "runtime.log").read_text(encoding="utf-8"),
        "".join(l + "\n" for l in normalized(text)),
        "provider full stream",
    )
    recovery_ledger = recovery_ten_ledger.verify(
        "".join(line + "\n" for line in normalized(recovery_text)),
        platform=platform,
        run_id=run_id,
        service_uuid=service_uuid,
        physical_path=root / "recovery" / recovery.PHYSICAL_NAME,
        fixture_request=fixture_request,
        fixture_identity=fixture_identity,
        host_cycles=host_cycles,
    )
    identity = read_document(log.with_suffix(".identity.json"))
    capture = read_document(log.with_suffix(".capture.json"))
    same(identity["status"], "passed", "full capture identity passed")
    same(capture["status"], "passed", "full capture passed")
    same(identity["platform"], platform, "capture platform")
    for value in (identity, capture):
        same(value.get("run_id"), run_id, "capture run identity")
        same(value.get("platform"), platform, "capture platform identity")
        same(value.get("device"), identity.get("device"), "capture device identity")
    same(
        identity["bootstrap"],
        recovery_ledger["native_bootstrap"],
        "recovery ledger native boot",
    )
    same(capture["host_cycle_actions"], list(range(10)), "ten host actions")
    for field in ("native_acknowledged", "js_acknowledged"):
        same(identity[field], list(range(10)), f"ten {field}")
    handoff = read_document(root / protocol.HANDOFF_NAME)
    recovery_physical = read_document(root / "recovery" / recovery.PHYSICAL_NAME)
    same(
        recovery_physical.get("retained_ticket"),
        handoff["fault_ticket"],
        "recovery read-only witness is the exact full-suite handoff ticket",
    )
    same(capture["handoff"], handoff, "captured immutable handoff")
    full_physical = read_document(root / protocol.PHYSICAL_NAME)
    check_final_physical(full_physical, handoff, root, run_id, fixture_identity)
    begin = capture["begin"]
    require(type(begin) is dict, "one host begin action")
    same(begin["url"], begin_url(run_id), "host begin URL")
    _ack_times(begin)
    signal = {
        **identity["bootstrap"],
        "source": "payload-url",
        "url": begin_url(run_id),
    }
    same(begin["native_signal"], signal, "host begin signal identity")
    same(
        full_physical["boundaries"][-1].get("native_signal"),
        signal,
        "provider begin signal",
    )
    markers = [l.removeprefix(PREFIX) for l in normalized(text)]
    nonce = recovery_ledger["runtime_nonce"]
    same(capture["runtime_nonce"], nonce, "capture shared JS nonce")
    milestones = [
        f"rf-lab-full:runtime:session={run_id}:nonce={nonce}:phase=start",
        f"rf-lab-suite:runtime:session={run_id}:nonce={nonce}:phase=start",
        f"rf-lab-suite:runtime:session={run_id}:nonce={nonce}:phase=close",
        protocol.BASE_PASSED,
        f"rf-lab-full:recovery-awaiting:runId={run_id}",
        "rf-lab-full:native:signal:" + json.dumps(signal),
        f"rf-lab-full:recovery-ready:runId={run_id}:source=linking",
        f"rf-recovery-ten:runtime:runId={run_id}:nonce={nonce}:phase=start",
        f"rf-recovery-ten:runtime:runId={run_id}:nonce={nonce}:phase=close",
        f"rf-recovery-ten:passed:cycles=10:runId={run_id}",
        f"rf-lab-full:runtime:session={run_id}:nonce={nonce}:phase=close",
        f"rf-lab-full:passed:flows=10:cycles=10:runId={run_id}",
    ]
    # JSON whitespace/key order is not identity. Everything else remains exact.
    for i, marker in enumerate(markers):
        if marker.startswith("rf-lab-full:native:signal:"):
            same(
                json.loads(marker.removeprefix("rf-lab-full:native:signal:")),
                signal,
                "raw begin signal",
            )
            markers[i] = milestones[5]
    indices = []
    for marker in milestones:
        require(markers.count(marker) == 1, f"exactly one milestone {marker}")
        indices.append(markers.index(marker))
    recovery_ten_ledger._ordered(indices, "same-runtime full-suite phase boundaries")
    for prefix in ("rf-host:bootstrap:", "rf-recovery-ten:native:bootstrap:"):
        boots = [i for i, m in enumerate(markers) if m.startswith(prefix)]
        require(
            len(boots) == 1 and boots[0] < indices[0],
            "launch bootstraps precede both runtime phases",
        )
    full = [m for m in markers if m.startswith("rf-lab-full:")]
    same(
        full,
        [milestones[i] for i in (0, 4, 5, 6, 10, 11)],
        "no foreign/duplicate full-suite records",
    )
    require(
        not any(
            re.search(
                r"(?:^|:)(?:failed|flow-failed|host-failed)(?::|$)|:close:rejected:", m
            )
            for m in markers
        ),
        "no runtime failure",
    )
    raw_lines = log.with_suffix(".raw.log").read_text(encoding="utf-8").splitlines()
    source = [(i + 1, l) for i, l in enumerate(raw_lines) if PREFIX in l]
    same(text.splitlines(), [l for _, l in source], "complete continuous raw lineage")
    same(
        session.verify_receipt(plan, fixture_identity=fixture_identity),
        receipt,
        "live receipt stable after full audit",
    )
    write_document(log.parent / "continuous10-ledger.json", base)
    write_document(log.parent / "recovery-ten-ledger.json", recovery_ledger)
    return {
        "schema_version": 1,
        "scenario": "full-suite",
        "mode": protocol.MODE,
        "status": "passed",
        "platform": platform,
        "run_id": run_id,
        "session_id": plan.session_id,
        "service_uuid": service_uuid,
        "runtime_nonce": nonce,
        "native_bootstrap": identity["bootstrap"],
        "fixture": recovery_ledger["fixture"],
        "live_receipt": receipt,
        "begin": begin,
        "handoff": handoff,
        "counts": {
            "app_runtimes": 1,
            "capture_children": 1,
            "base_flows": 10,
            "recovery_cycles": 10,
            "recovery_terminals": recovery_ledger["counts"]["terminals"],
        },
        "ledgers": {
            "continuous10": _binding(log.parent / "continuous10-ledger.json"),
            "recovery_ten": _binding(log.parent / "recovery-ten-ledger.json"),
        },
        "physical": {
            "handoff": _binding(root / protocol.HANDOFF_NAME),
            "full_suite": _binding(root / protocol.PHYSICAL_NAME),
            "recovery": _binding(root / "recovery" / recovery.PHYSICAL_NAME),
            "base": base["physical"],
        },
        "capture": {
            "identity": _binding(log.with_suffix(".identity.json")),
            "receipt": _binding(log.with_suffix(".capture.json")),
            "raw_log": {
                "path": str(log.with_suffix(".raw.log")),
                "sha256": hashlib.sha256(
                    log.with_suffix(".raw.log").read_bytes()
                ).hexdigest(),
            },
            "raw_line_numbers": [i for i, _ in source],
            "recovery_raw_line_numbers": [i for i, l in source if recovery.PREFIX in l],
        },
        "explicit_limits": {
            "scope": "cooperative app/native/provider records with live receipt, not independent OS memory or cryptographic process provenance; host-only tests are not RF evidence"
        },
    }
