"""Non-RF iOS XCTest stdout handshake preflight.

This owns only one bounded test-runner window and a transport marker. It never
opens Settings, changes the radio, launches a BLE Probe, starts a Peripheral,
or creates a phone/fixture lease.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import plistlib
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from tools.react_native.capture_io import PrefixStream, stop_local
from tools.react_native.ios_device import (
    canonical_physical_udid,
    require_ready_unlocked_device,
)
from tools.react_native.ios_sdk import xcodebuild_test_without_building
from tools.react_native.rf_evidence_bundle import _read_small, _recovery_json, _require

TARGET = "BleHubNitroHostUITests"
METHOD = TARGET + "/testAdapterReadyTransportWithoutRadio"
IDENTIFIER = TARGET + "/" + METHOD
PREFIX = "BLEHUB_IOS_ADAPTER_HANDSHAKE_READY "
MAX_BYTES = 8 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 180


def _validate_marker(value: object, run_id: str) -> dict[str, Any]:
    _require(type(value) is dict, "handshake marker must be an object")
    _require(
        set(value)
        == {"schema_version", "run_id", "nonce", "source", "test_process_id"},
        "handshake marker exact schema",
    )
    _require(value["schema_version"] == 1, "handshake marker schema")
    _require(value["run_id"] == run_id, "handshake marker run identity")
    _require(
        type(value["nonce"]) is str
        and len(value["nonce"]) == 36
        and str(uuid.UUID(value["nonce"])) == value["nonce"],
        "handshake marker nonce",
    )
    _require(value["source"] == "ios-ui-handshake", "handshake marker source")
    _require(
        type(value["test_process_id"]) is int and value["test_process_id"] > 0,
        "handshake marker PID",
    )
    return value


def _reject_options(value: object) -> None:
    if isinstance(value, str):
        _require(
            not any(
                token in value.lower()
                for token in ("retry-tests", "run-tests-until", "test-iterations")
            ),
            "handshake repetition/retry value is forbidden",
        )
    elif isinstance(value, dict):
        for key, child in value.items():
            _require(type(key) is str, "handshake xctestrun keys must be strings")
            _require(
                not any(
                    word in key.lower() for word in ("retry", "repet", "iteration")
                ),
                "handshake repetition/retry option is forbidden",
            )
            _reject_options(child)
    elif isinstance(value, list):
        for child in value:
            _reject_options(child)


def stage_test_run(source: Path, destination: Path, app: Path, *, run_id: str) -> str:
    """Stage one frozen UI target and return its SHA-256 without building."""
    _require(
        app.is_dir() and app.name == "BleHubNitroHost.app", "signed host App required"
    )
    _require(
        not destination.exists() and not destination.is_symlink(),
        "refusing existing handshake staging",
    )
    raw = _read_small(source, MAX_BYTES)
    document = plistlib.loads(raw)
    _require(
        type(document) is dict
        and TARGET in document
        and set(document)
        <= {
            TARGET,
            "BleHubNitroHostNativeTests",
            "__xctestrun_metadata__",
        },
        "handshake xctestrun contains an unsupported target",
    )
    target = document[TARGET]
    _require(
        type(target) is dict and target.get("IsUITestBundle") is True,
        "handshake UI target required",
    )

    def relocate(value: object) -> object:
        if isinstance(value, str):
            return value.replace("__TESTROOT__", str(source.resolve().parent))
        if isinstance(value, list):
            return [relocate(item) for item in value]
        if isinstance(value, dict):
            return {key: relocate(item) for key, item in value.items()}
        return value

    target = relocate(target)
    _reject_options(target)
    for key in ("CommandLineArguments", "UITargetAppCommandLineArguments"):
        _require(target.get(key, []) == [], f"handshake {key} must be empty")
    for field in (
        "EnvironmentVariables",
        "TestingEnvironmentVariables",
        "UITargetAppEnvironmentVariables",
    ):
        values = target.get(field, {})
        _require(
            type(values) is dict
            and all(
                type(key) is str and type(value) is str for key, value in values.items()
            ),
            f"handshake {field} must contain only string keys and values",
        )
        _require(
            not any("BLEHUB" in key for key in values),
            f"conflicting handshake BLEHUB opt-in in {field}",
        )
    environment = target.get("EnvironmentVariables", {})
    target["EnvironmentVariables"] = {
        **environment,
        "BLEHUB_IOS_ADAPTER_HANDSHAKE_GATE": "1",
        "BLEHUB_IOS_ADAPTER_HANDSHAKE_RUN_ID": run_id,
    }
    target["OnlyTestIdentifiers"] = [METHOD]
    target["SkipTestIdentifiers"] = []
    original_app = target.get("UITargetAppPath")
    dependencies = target.get("DependentProductPaths")
    _require(
        type(original_app) is str
        and original_app.endswith(".app")
        and type(dependencies) is list
        and dependencies.count(original_app) == 1,
        "handshake App dependency shape",
    )
    frozen_app = str(app.resolve())
    target["DependentProductPaths"] = [
        frozen_app + item[len(original_app) :]
        if item == original_app or item.startswith(original_app + "/")
        else item
        for item in dependencies
    ]
    target["UITargetAppPath"] = frozen_app
    target["UserAttachmentLifetime"] = "keepAlways"
    target["SystemAttachmentLifetime"] = "keepAlways"
    staged = {
        TARGET: target,
        "__xctestrun_metadata__": document.get("__xctestrun_metadata__", {}),
    }
    with destination.open("xb") as stream:
        plistlib.dump(staged, stream)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def _collect_summary(result: Path, log: Path) -> dict[str, Any]:
    summary_path = log.with_suffix(".summary.json")
    with (
        summary_path.open("xb") as output,
        log.with_suffix(".summary.log").open("xb") as diagnostics,
    ):
        subprocess.run(
            [
                "xcrun",
                "xcresulttool",
                "get",
                "test-results",
                "summary",
                "--path",
                str(result),
                "--compact",
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=diagnostics,
            check=True,
            timeout=30,
        )
    return _recovery_json(_read_small(summary_path, MAX_BYTES).decode("utf-8"))


def _runner_initialization_failed(summary: dict[str, Any], device: str) -> bool:
    configurations = summary.get("devicesAndConfigurations")
    failures = summary.get("testFailures")
    return (
        summary.get("result") == "Failed"
        and all(
            type(summary.get(key)) is int
            for key in ("totalTestCount", "passedTests", "failedTests")
        )
        and summary.get("totalTestCount") == 1
        and summary.get("passedTests") == 0
        and summary.get("failedTests") == 1
        and isinstance(configurations, list)
        and len(configurations) == 1
        and isinstance(configurations[0], dict)
        and isinstance(configurations[0].get("device"), dict)
        and configurations[0]["device"].get("deviceId") == device
        and isinstance(failures, list)
        and len(failures) == 1
        and isinstance(failures[0], dict)
        and failures[0].get("targetName") == TARGET
        and isinstance(failures[0].get("failureText"), str)
        and "The test runner failed to initialize for UI testing."
        in failures[0]["failureText"]
        and isinstance(failures[0].get("testIdentifierString"), str)
        and re.fullmatch(
            re.escape(TARGET) + r"-Runner \([1-9][0-9]*\) encountered an error",
            failures[0]["testIdentifierString"],
        )
        is not None
    )


def run(
    device: str, *, app: Path, test_run: Path, log: Path, timeout: float = 90
) -> Path:
    """Run exactly one non-RF XCTest and prove live marker delivery."""
    device = canonical_physical_udid(device)
    _require(
        type(timeout) in (int, float)
        and math.isfinite(timeout)
        and 0 < timeout <= MAX_TIMEOUT_SECONDS,
        "handshake timeout bound",
    )
    require_ready_unlocked_device(
        device, cwd=Path(__file__).resolve().parents[2], timeout=min(60, timeout)
    )
    staged = log.with_suffix(".xctestrun")
    result = log.with_suffix(".xcresult")
    capture = log.with_suffix(".capture.json")
    raw_log = log.with_suffix(".raw.log")
    for path in (log, raw_log, capture, staged, result):
        _require(
            not path.exists() and not path.is_symlink(),
            f"refusing existing handshake evidence: {path}",
        )
    stage_hash = stage_test_run(test_run, staged, app, run_id=str(uuid.uuid4()))
    staged_doc = plistlib.loads(staged.read_bytes())
    run_id = staged_doc[TARGET]["EnvironmentVariables"][
        "BLEHUB_IOS_ADAPTER_HANDSHAKE_RUN_ID"
    ]
    command = xcodebuild_test_without_building(
        xctestrun=staged,
        device=device,
        result=result,
        only_testing=IDENTIFIER,
    )
    started = time.time()
    process: subprocess.Popen[bytes] | None = None
    marker: dict[str, Any] | None = None
    marker_seen_at: float | None = None
    stream: PrefixStream | None = None
    lines: list[str] = []
    primary: BaseException | None = None
    summary: dict[str, Any] | None = None
    stage = "ui-runner-launch"
    try:
        with raw_log.open("xb") as output:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        stage = "ui-handshake"
        stream = PrefixStream(raw_log, None, prefix=PREFIX)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            new_lines = stream.poll()
            lines.extend(new_lines)
            for line in new_lines:
                _require(marker is None, "duplicate handshake marker")
                marker = _validate_marker(
                    _recovery_json(line.removeprefix(PREFIX)), run_id
                )
                marker_seen_at = time.time()
                _require(
                    process.poll() is None, "handshake marker arrived after XCTest exit"
                )
            code = process.poll()
            if code is not None:
                final_lines = stream.poll()
                lines.extend(final_lines)
                _require(
                    marker is not None, "XCTest exited without live handshake marker"
                )
                _require(code == 0, f"handshake XCTest failed with exit {code}")
                break
            time.sleep(0.1)
        else:
            raise TimeoutError("live handshake marker deadline exceeded")
        _require(
            marker is not None and marker_seen_at is not None,
            "missing live handshake marker",
        )
        stage = "ui-summary-validation"
        summary = _collect_summary(result, log)
        _require(
            summary.get("result") == "Passed"
            and summary.get("passedTests") == 1
            and summary.get("failedTests") == 0
            and summary.get("skippedTests") == 0,
            "handshake XCTest summary must pass exactly one test",
        )
        document = {
            "scope": "ios-adapter-handshake",
            "status": "passed",
            "device": device,
            "run_id": run_id,
            "command": command,
            "stage_sha256": stage_hash,
            "marker": marker,
            "marker_seen_at": marker_seen_at,
            "started_at": started,
            "finished_at": time.time(),
            "radio_mutated": False,
            "fixture_started": False,
            "probe_started": False,
            "summary": summary,
            "test_body_started": True,
            "failure": None,
        }
        log.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        capture.write_text(
            json.dumps(document, sort_keys=True) + "\n", encoding="utf-8"
        )
        return capture
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_errors = []
        if process is not None and process.poll() is None:
            try:
                stop_local(process, console=False)
            except Exception as cleanup:
                if primary is None:
                    raise
                cleanup_errors.append(str(cleanup))
                primary.add_note(f"handshake cleanup: {cleanup}")
        if primary is not None:
            diagnostic_errors = []
            if summary is None and result.is_dir() and stage != "ui-summary-validation":
                try:
                    summary = _collect_summary(result, log)
                except Exception as diagnostic:  # noqa: BLE001 - diagnostics cannot replace execution failure
                    diagnostic_errors.append(str(diagnostic))
                    primary.add_note(f"handshake summary collection: {diagnostic}")
            body_started = True if marker is not None else None
            if (
                marker is None
                and summary is not None
                and _runner_initialization_failed(summary, device)
            ):
                stage = "ui-runner-initialization"
                body_started = False
            document = {
                "scope": "ios-adapter-handshake",
                "status": "failed",
                "device": device,
                "run_id": run_id,
                "command": command,
                "stage_sha256": stage_hash,
                "marker": marker,
                "marker_seen_at": marker_seen_at,
                "radio_mutated": False,
                "fixture_started": False,
                "probe_started": False,
                "error": str(primary),
                "summary": summary,
                "test_body_started": body_started,
                "failure": {
                    "stage": stage,
                    "exception_type": type(primary).__name__,
                    "detail": str(primary),
                    "root_cause": "undetermined; runner startup is not BLE/TCC denial",
                },
                "cleanup_errors": cleanup_errors,
                "diagnostic_errors": diagnostic_errors,
            }
            try:
                capture.write_text(
                    json.dumps(document, sort_keys=True) + "\n", encoding="utf-8"
                )
            except OSError as publication:
                primary.add_note(f"handshake result publication: {publication}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device")
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--xctestrun", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    try:
        print(
            run(
                args.device,
                app=args.app,
                test_run=args.xctestrun,
                log=args.log,
                timeout=args.timeout,
            )
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        TimeoutError,
    ) as error:
        print(f"ios-adapter-handshake:failed {error}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
