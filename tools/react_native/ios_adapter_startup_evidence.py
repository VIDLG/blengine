"""Offline startup-off evidence binding; no device access or installed-binary claim.

The window log is an XCTest marker projection, never an App console. Semantic
owners are imported at replay time: the journal/ledger already depend on the
bundle's bounded readers and errors. Failed bundles retain partial inventory
without requiring a successful UI restoration or physical replay.
"""

from __future__ import annotations

import hashlib
import math
import plistlib
import re
from pathlib import Path
from typing import Any

SCENARIO = "ios-adapter-startup"
LOG = SCENARIO + ".log"
WINDOW_PREFIX = "BLEHUB_IOS_ADAPTER_WINDOW_READY "
TEST_TARGET = "BleHubNitroHostUITests"
TEST_METHOD = TEST_TARGET + "/testBluetoothStartupOffAndFreshProbeRecovery"
ATTACHMENT_METHOD = TEST_METHOD + "()"
CONTROL_ATTACHMENT = "ios-adapter-startup-control"


def names() -> dict[str, str]:
    """Fixed attempt roles; exported attachment filenames remain inventory-bound."""
    stem = SCENARIO
    return {
        "window_log": LOG,
        "raw_runtime": stem + ".raw.log",
        "runtime_capture": stem + ".capture.json",
        "test_run": stem + ".xctestrun",
        "journal": stem + ".journal.json",
        "journal_copy": stem + ".journal-copy.json",
        "off_journal": stem + ".off-journal.json",
        "off_journal_copy": stem + ".off-journal-copy.json",
        "ready_before_processes": stem + ".ready-before-processes.json",
        "startup_ready_payload": stem + ".startup-ready-payload.json",
        "startup_ready_after_processes": stem + ".startup-ready-after-processes.json",
        "device_install": "device-install.json",
        "control": stem + "-control.json",
        "summary": stem + "-summary.json",
        "attachments": "xcresult-attachments/manifest.json",
        "phone_lease": "ios-radio-lease.json",
        "ledger": stem + "-ledger.json",
        "fixture_request": "coordination/fixture-session/fixture-request.json",
        "fixture_receipt": "coordination/fixture-session/fixture-receipt.json",
        "readiness": "coordination/ios-adapter-startup-ready.json",
        "physical": "coordination/ios-adapter-startup-physical.json",
    }


def index_runtime(text: str, relative: str) -> dict[str, Any]:
    """Index only the unchanged window projection, including partial failed logs."""
    from tools.react_native.rf_evidence_bundle import EvidenceError, _recovery_json

    raw = text.encode("utf-8")
    result: dict[str, Any] = {
        "scenario": SCENARIO,
        "source": relative,
        "sha256": hashlib.sha256(raw).hexdigest() if raw else None,
        "size": len(raw),
        "windows": [],
        "invalid_markers": [],
        "error_markers": [],
    }
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        try:
            if not line.startswith(WINDOW_PREFIX) or not line.endswith("\n"):
                raise EvidenceError("not a complete window marker")
            value = _recovery_json(line[len(WINDOW_PREFIX) :])
            result["windows"].append({"line": number, "window": value})
        except (ValueError, TypeError):
            result["invalid_markers"].append(number)
    return result


def verify_structure(document: dict) -> None:
    """A pass requires one window and all fixed evidence roles, not status counts."""
    from tools.react_native.rf_evidence_bundle import _require

    index = document["flow_index"]
    _require(
        document["platform"] == "ios"
        and document["runtime_log"] == LOG
        and "adapter_mode" not in document,
        "startup evidence requires its explicit iOS scenario, not active-loss",
    )
    _require(
        index["scenario"] == SCENARIO
        and index["source"] == LOG
        and not index["invalid_markers"]
        and not index["error_markers"]
        and len(index["windows"]) == 1,
        "startup evidence requires exactly one window marker",
    )
    _require(
        set(document["references"]) == set(names())
        and all(document["references"].values()),
        "missing startup evidence references",
    )


def _same(actual: Any, expected: Any, label: str) -> None:
    from tools.react_native.rf_evidence_bundle import _canonical, _require

    _require(_canonical(actual) == _canonical(expected), f"startup {label} drift")


def _phone(value: dict, document: dict, control_hash: str) -> None:
    from tools.react_native.rf_evidence_bundle import _require

    expected = {
        "schema_version": 1,
        "platform": "ios",
        "device": document["device"],
        "run_id": document["run_id"],
        "status": "released",
        "scope": "ios-adapter-startup-phone-owner",
        "control_sha256": control_hash,
        "restoration": "on-readback",
        "error": None,
    }
    _require(
        set(value)
        == set(expected) | {"host_pid", "acquired_monotonic", "released_monotonic"},
        "startup phone lease exact schema",
    )
    for key, item in expected.items():
        _same(value[key], item, f"phone {key}")
    _require(type(value["host_pid"]) is int and value["host_pid"] > 0, "phone host PID")
    for key in ("acquired_monotonic", "released_monotonic"):
        clock = value[key]
        _require(
            type(clock) in (int, float) and math.isfinite(clock) and clock >= 0,
            "phone finite monotonic",
        )
    _require(
        value["released_monotonic"] >= value["acquired_monotonic"],
        "phone lease clock order",
    )


def _xctest(attempt: Path, document: dict, load: Any, read: Any) -> None:
    from tools.react_native.rf_evidence_bundle import _require, _safe_name

    summary = load("summary")
    for key, expected in {
        "totalTestCount": 1,
        "passedTests": 1,
        "failedTests": 0,
        "skippedTests": 0,
    }.items():
        _same(summary.get(key), expected, f"XCTest {key}")
    _same(summary.get("result"), "Passed", "XCTest result")
    _same(summary.get("testFailures"), [], "XCTest failures")
    _same(summary.get("expectedFailures", 0), 0, "XCTest expected failures")
    configurations = summary.get("devicesAndConfigurations")
    _require(
        type(configurations) is list and len(configurations) == 1,
        "XCTest unique device/configuration",
    )
    configuration = configurations[0]
    _same(
        configuration.get("expectedFailures", 0), 0, "XCTest device expected failures"
    )
    _same(configuration["device"]["deviceId"], document["device"], "XCTest device")
    for key, expected in {
        "passedTests": 1,
        "failedTests": 0,
        "skippedTests": 0,
    }.items():
        _same(configuration.get(key), expected, f"XCTest device {key}")

    manifest = load("attachments")
    _require(
        type(manifest) is list and len(manifest) == 1, "XCTest unique attachment method"
    )
    group = manifest[0]
    _same(group.get("testIdentifier"), ATTACHMENT_METHOD, "XCTest method")
    attachments = group.get("attachments")
    _require(type(attachments) is list and bool(attachments), "XCTest attachments")
    matches, exported = [], set()
    inventory = {row["path"] for row in document["files"]}
    for attachment in attachments:
        _same(attachment.get("deviceId"), document["device"], "attachment device")
        _require(
            attachment.get("isAssociatedWithFailure") is False,
            "failure-associated attachment",
        )
        name = _safe_name(attachment["exportedFileName"])
        _require(
            "/" not in name and name not in exported,
            "nonunique/unsafe exported attachment",
        )
        exported.add(name)
        relative = "xcresult-attachments/" + name
        _require(
            relative in inventory and (attempt / relative).is_file(),
            "missing exported attachment bytes",
        )
        suggested = attachment.get("suggestedHumanReadableName", "")
        _require(type(suggested) is str, "attachment suggested name")
        if re.fullmatch(
            re.escape(CONTROL_ATTACHMENT) + r"_[0-9]+_[0-9A-Fa-f-]+\.json", suggested
        ):
            matches.append(attempt / relative)
    _require(len(matches) == 1, "XCTest requires exactly one fixed control attachment")
    from tools.react_native.rf_evidence_bundle import MAX_DOCUMENT_BYTES, _read_small

    _require(
        _read_small(matches[0], MAX_DOCUMENT_BYTES) == read("control"),
        "startup original control attachment bytes drift",
    )


def _test_run(
    raw: bytes, original: Path, document: dict, runtime: dict, receipt_hash: str
) -> None:
    from tools.react_native.host_contract import BUNDLE_ID
    from tools.react_native.rf_evidence_bundle import _require

    value = plistlib.loads(raw)
    _require(
        type(value) is dict and set(value) <= {TEST_TARGET, "__xctestrun_metadata__"},
        "startup UI-only xctestrun",
    )
    target = value[TEST_TARGET]
    expected = {
        "IsUITestBundle": True,
        "OnlyTestIdentifiers": [TEST_METHOD],
        "SkipTestIdentifiers": [],
        "UITargetAppPath": str(original / document["artifact"]["path"]),
        "UITargetAppBundleIdentifier": BUNDLE_ID,
    }
    for key, item in expected.items():
        _same(target.get(key), item, f"xctestrun {key}")

    environment = {
        "BLEHUB_IOS_ADAPTER_STARTUP_GATE": "1",
        "BLEHUB_IOS_ADAPTER_RUN_ID": document["run_id"],
        "BLEHUB_IOS_ADAPTER_SERVICE_UUID": runtime["service_uuid"],
        "BLEHUB_IOS_ADAPTER_FIXTURE_RECEIPT_SHA256": receipt_hash,
    }
    for field in (
        "EnvironmentVariables",
        "TestingEnvironmentVariables",
        "UITargetAppEnvironmentVariables",
    ):
        values = target.get(field, {})
        _require(
            type(values) is dict
            and all(type(k) is str and type(v) is str for k, v in values.items()),
            "xctestrun environment strings",
        )
        blehub = {k: v for k, v in values.items() if "BLEHUB" in k}
        if field == "EnvironmentVariables":
            _same(
                blehub,
                {**environment, "BLEHUB_IOS_ADAPTER_LOCAL_NAME": runtime["local_name"]},
                "xctestrun gate environment",
            )
        else:
            _same(blehub, {}, "xctestrun conflicting environment")
    for key in ("CommandLineArguments", "UITargetAppCommandLineArguments"):
        _same(target.get(key, []), [], f"xctestrun {key}")
    dependencies = target.get("DependentProductPaths")
    app = expected["UITargetAppPath"]
    _require(
        type(dependencies) is list
        and all(type(p) is str for p in dependencies)
        and dependencies.count(app) == 1,
        "xctestrun frozen App dependency",
    )
    _require(
        all(
            "__TESTROOT__" not in p
            and ("BleHubNitroHost.app" not in p or p == app or p.startswith(app + "/"))
            for p in dependencies
        ),
        "xctestrun foreign App dependency",
    )


def verify_hooks(attempt: Path, document: dict) -> None:
    """Replay all semantic owners, then bind raw bytes and official SDK identities."""
    from tests.peripherals.nrf52840.ios_adapter_startup import verify_physical
    from tools.react_native import ios_adapter_startup_ledger as ledger
    from tools.react_native.host_contract import APP_NAME, BUNDLE_ID, SCHEME
    from tools.react_native.ios_adapter_startup_journal import parse_journal
    from tools.react_native.ios_sdk import ios_executables_match, official_result
    from tools.react_native.rf_evidence_bundle import (
        MAX_DOCUMENT_BYTES,
        MAX_LOG_BYTES,
        _read_small,
        _recovery_json,
        _require,
    )

    verify_structure(document)
    paths = names()

    def read(role: str) -> bytes:
        return _read_small(attempt / paths[role], MAX_DOCUMENT_BYTES)

    def load(role: str) -> Any:
        text = read(role).decode("utf-8")
        # Reuse strict duplicate-key/nonfinite decoding for the exported array.
        if role == "attachments":
            wrapped = _recovery_json('{"attachments":' + text + "}")
            _require(
                set(wrapped) == {"attachments"}, "attachment manifest JSON envelope"
            )
            return wrapped["attachments"]
        return _recovery_json(text)

    def official(role: str, command_type: str) -> tuple[dict, list]:
        try:
            result = official_result(
                attempt / paths[role], command_type, document["device"]
            )
        except (RuntimeError, TypeError, KeyError, IndexError) as error:
            raise ValueError(f"startup official {role}: {error}") from error
        arguments = load(role)["info"]["arguments"]
        _require(
            not any(
                flag in arguments for flag in ("--terminate-existing", "--console")
            ),
            "startup forbids replacement/console",
        )
        return result, arguments

    run = document["run_id"]
    request, receipt = load("fixture_request"), load("fixture_receipt")
    receipt_hash = hashlib.sha256(read("fixture_receipt")).hexdigest()
    journal = parse_journal(read("journal"), run)
    runtime = ledger.verify_runtime(journal, request["service_uuid"])
    off_journal = parse_journal(read("off_journal"), run)
    off = ledger.verify_off_journal(off_journal)
    _same(
        list(off_journal.records), list(journal.records[:8]), "OFF/full journal prefix"
    )
    _same(len(off_journal.records), 8, "OFF record count")
    control = ledger.verify_control(
        load("control"), runtime=runtime, receipt_sha256=receipt_hash
    )
    try:
        physical = verify_physical(
            load("physical"),
            request=request,
            receipt=receipt,
            receipt_sha256=receipt_hash,
            runtime=runtime,
            readiness=load("readiness"),
        )
    except RuntimeError as error:
        # The provider's installed-image parser uses its own RuntimeError subtype.
        raise ValueError(f"startup physical receipt replay: {error}") from error
    _same(physical["off"], off, "physical OFF journal replay")
    _same(
        load("ledger"),
        {
            "schema_version": 1,
            "scope": "ios-adapter-startup-host-audit",
            "run_id": run,
            "runtime": runtime,
            "control": control,
            "physical": physical,
        },
        "host ledger replay",
    )

    capture = load("runtime_capture")
    for key, expected in {
        "schema_version": 1,
        "platform": "ios",
        "device": document["device"],
        "run_id": run,
        "scope": "ios-adapter-startup-window-candidate",
        "fixture_receipt_sha256": receipt_hash,
        "status": "window-completed-awaiting-evidence",
        "ready_delivered": True,
        "primary_error": None,
        "cleanup_errors": [],
        "actions": [],
        "test_run_sha256": hashlib.sha256(read("test_run")).hexdigest(),
    }.items():
        _same(capture.get(key), expected, f"capture {key}")
    _require(
        type(capture.get("capture_pid")) is int and capture["capture_pid"] > 0,
        "capture runner PID",
    )
    window = document["flow_index"]["windows"][0]["window"]
    identity = ledger.verify_window_ready(window, run, receipt_hash)
    _same(capture.get("window"), window, "capture window")
    _same(identity, control["identity"], "window/control identity")
    projection = []
    raw = _read_small(attempt / paths["raw_runtime"], MAX_LOG_BYTES).decode("utf-8")
    for line in raw.splitlines(keepends=True):
        if WINDOW_PREFIX not in line:
            continue
        _require(
            line.endswith("\n") and line.count(WINDOW_PREFIX) == 1,
            "ambiguous/truncated raw window",
        )
        projection.append(line[line.index(WINDOW_PREFIX) :].rstrip("\r\n") + "\n")
    _require(len(projection) == 1, "raw window occurrence must be exactly once")
    _require(
        "".join(projection).encode() == read("window_log"),
        "startup raw window projection bytes/hash drift",
    )

    installed, arguments = official("device_install", "devicectl.device.install.app")
    candidate = Path(arguments[-1])
    _require(candidate.is_absolute(), "absolute frozen install path")
    original = candidate.parents[len(Path(document["artifact"]["path"]).parts) - 1]
    _same(
        str(candidate),
        str(original / document["artifact"]["path"]),
        "frozen install path",
    )
    rows = installed.get("installedApplications")
    _require(
        type(rows) is list and all(type(row) is dict for row in rows),
        "official installed applications",
    )
    matches = [row for row in rows if row.get("bundleID") == BUNDLE_ID]
    _require(len(matches) == 1, "unique official host installation")
    url = matches[0].get("installationURL")
    _require(
        type(url) is str
        and url.startswith("file:///")
        and url.endswith(f"/{APP_NAME}/"),
        "official installation URL",
    )
    executable, pid = url + SCHEME, runtime["pid"]
    _require(
        ios_executables_match(executable, runtime["native_bootstrap"]["executable"]),
        "installed runtime executable",
    )
    _same(
        document["artifact"]["fingerprint"]["executable"],
        SCHEME,
        "candidate executable",
    )
    device_identifier = installed["deviceIdentifier"]
    for role in ("ready_before_processes", "startup_ready_after_processes"):
        result, _ = official(role, "devicectl.device.info.processes")
        _same(result["deviceIdentifier"], device_identifier, "official process device")
        rows = result.get("runningProcesses")
        _require(
            type(rows) is list
            and all(
                type(row) is dict
                and type(row.get("processIdentifier")) is int
                and row["processIdentifier"] > 0
                for row in rows
            ),
            "official process rows",
        )
        matches = [row for row in rows if row["processIdentifier"] == pid]
        _require(
            len(matches) == 1
            and ios_executables_match(executable, matches[0].get("executable", "")),
            "official PID/executable replaced",
        )
    result, arguments = official(
        "startup_ready_payload", "devicectl.device.process.launch"
    )
    _same(result["deviceIdentifier"], device_identifier, "payload device")
    process = result.get("process", {})
    _same(process.get("processIdentifier"), pid, "payload PID")
    _require(
        ios_executables_match(executable, process.get("executable", "")),
        "payload executable",
    )
    _require(
        arguments.count("--payload-url") == 1
        and arguments[arguments.index("--payload-url") + 1 :][:1]
        == [runtime["native_ready"]["url"]]
        and arguments[-1:] == [BUNDLE_ID],
        "official ready payload URL/bundle",
    )
    for role, destination in (
        ("journal_copy", "journal"),
        ("off_journal_copy", "off_journal"),
    ):
        result, arguments = official(role, "devicectl.device.copy.from")
        _same(result["deviceIdentifier"], device_identifier, "journal copy device")
        for option, expected in {
            "--source": f"Library/Caches/blehub-ios-adapter/{run}.json",
            "--domain-type": "appDataContainer",
            "--domain-identifier": BUNDLE_ID,
            "--destination": str(original / paths[destination]),
        }.items():
            _require(
                arguments.count(option) == 1
                and arguments[arguments.index(option) + 1 :][:1] == [expected],
                f"journal copy {option}",
            )
    _same(
        capture.get("command"),
        [
            "xcrun",
            "xcodebuild",
            "-xctestrun",
            str(original / paths["test_run"]),
            "-destination",
            f"id={document['device']}",
            "-resultBundlePath",
            str((original / LOG).with_suffix(".xcresult")),
            "-parallel-testing-enabled",
            "NO",
            "-collect-test-diagnostics",
            "never",
            "-only-testing:" + TEST_TARGET + "/" + TEST_METHOD,
            "test-without-building",
        ],
        "single XCTest command",
    )
    launch = next(r for r in journal.records if ":on:flow:rf:start:" in r)
    local_name = launch.split(":expectedLocalName=", 1)[1].split(":initCommandId=", 1)[
        0
    ]
    _test_run(
        read("test_run"),
        original,
        document,
        {**runtime, "local_name": local_name},
        receipt_hash,
    )
    _xctest(attempt, document, load, read)
    phone = load("phone_lease")
    _phone(phone, document, hashlib.sha256(read("control")).hexdigest())
    _same(
        receipt.get("fixture_pid"),
        phone["host_pid"],
        "in-process fixture/phone owner PID",
    )
