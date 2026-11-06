"""Restore iOS Bluetooth once, then collect a fresh read-only ON proof.

This command owns the existing RN Settings XCTest target and iOS inspection
archiver. It performs one explicit ON-only mutation from the current phone
state; the following inspection test is read-only and is the proof consumed by
phone-lease quarantine restoration. It never performs an OFF mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import sys
import uuid
from pathlib import Path

from tools.react_native import ios_sdk
from tools.react_native.host_paths import PROJECT_ROOT
from tools.react_native.ios_device import canonical_physical_udid
from tools.react_native.ios_radio_restoration import collect_inspection

ROOT = PROJECT_ROOT
TARGET = "BleHubNitroHostUITests"
METHOD = TARGET + "/testBluetoothSettingsOffAndRestore"
ONLY_TESTING = METHOD
RESTORE_FLAG = "BLEHUB_IOS_RADIO_CONTROL_RESTORE_ONLY"
INSPECT_FLAG = "BLEHUB_IOS_RADIO_CONTROL_INSPECT_ONLY"
GATE_FLAG = "BLEHUB_IOS_RADIO_CONTROL_GATE"


class RadioRestoreError(RuntimeError):
    """The bounded restore/inspection sequence did not complete safely."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RadioRestoreError(message)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, value: object) -> None:
    _require(not path.exists(), f"refusing existing evidence path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _stage_restore_xctestrun(source: Path) -> Path:
    """Stage only the restore flag while preserving source product resolution."""
    _require(
        source.is_file() and not source.is_symlink(), f"missing xctestrun: {source}"
    )
    try:
        document = plistlib.loads(source.read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        raise RadioRestoreError(f"invalid xctestrun: {source}") from error
    target = document.get(TARGET)
    _require(isinstance(target, dict), f"xctestrun lacks {TARGET}")
    target = dict(target)
    selected = target.get("OnlyTestIdentifiers", [])
    skipped = target.get("SkipTestIdentifiers", [])
    _require(selected in ([], [METHOD]), "xctestrun selected method drift")
    _require(skipped == [], "xctestrun skip list drift")
    target["OnlyTestIdentifiers"] = [METHOD]
    target["SkipTestIdentifiers"] = []
    environment = target.get("EnvironmentVariables")
    _require(isinstance(environment, dict), "xctestrun environment is not an object")
    environment = dict(environment)
    environment[GATE_FLAG] = "1"
    environment[INSPECT_FLAG] = "0"
    environment[RESTORE_FLAG] = "1"
    target["EnvironmentVariables"] = environment
    document[TARGET] = target
    destination = source.with_name(
        f"{source.stem}-restore-only-{uuid.uuid4().hex}.xctestrun"
    )
    destination.write_bytes(plistlib.dumps(document, sort_keys=False))
    return destination


def _assert_one_passed_test(result: Path) -> None:
    command = [
        "xcrun",
        "xcresulttool",
        "get",
        "test-results",
        "summary",
        "--path",
        str(result),
        "--compact",
    ]
    completed = ios_sdk.run_command(command, timeout=30, check=False)
    _require(completed.returncode == 0, "xcresult summary could not be read")
    try:
        summary = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RadioRestoreError("xcresult summary is not JSON") from error
    _require(
        summary.get("totalTestCount") == 1
        and summary.get("passedTests") == 1
        and summary.get("failedTests") == 0
        and summary.get("skippedTests") == 0,
        f"xctest did not run exactly one passed test: {summary}",
    )


def _run_test(
    *,
    xctestrun: Path,
    device: str,
    result: Path,
    log: Path,
    timeout: float,
) -> list[str]:
    _require(not result.exists(), f"refusing existing result bundle: {result}")
    _require(not log.exists(), f"refusing existing test log: {log}")
    command = ios_sdk.xcodebuild_test_without_building(
        xctestrun=xctestrun,
        device=device,
        result=result,
        only_testing=ONLY_TESTING,
        quiet=True,
        launcher="xcodebuild",
    )
    completed = ios_sdk.run_logged(
        command,
        cwd=ROOT,
        log=log,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        details = log.read_text(encoding="utf-8", errors="replace")[-3000:]
        raise RadioRestoreError(
            f"iOS radio test failed with exit {completed.returncode}:\n{details}"
        )
    _assert_one_passed_test(result)
    return command


def run(
    *,
    device: str,
    xctestrun: Path,
    restore_result: Path,
    inspect_result: Path,
    owner_sha256: str,
    journal_sha256: str,
    inspection_input: Path,
    inspection_output: Path,
    timeout: float,
) -> dict[str, object]:
    device = canonical_physical_udid(device)
    xctestrun = xctestrun.absolute()
    restore_result, inspect_result = (
        restore_result.absolute(),
        inspect_result.absolute(),
    )
    inspection_input, inspection_output = (
        inspection_input.absolute(),
        inspection_output.absolute(),
    )
    _require(
        len(owner_sha256) == 64 and len(journal_sha256) == 64, "invalid lease hash"
    )
    _require(
        xctestrun.is_file() and not xctestrun.is_symlink(), "missing source xctestrun"
    )
    _require(not inspection_input.exists(), "inspection input already exists")
    _require(not inspection_output.exists(), "inspection output already exists")
    inspection_output.mkdir(parents=True)

    restore_xctestrun = _stage_restore_xctestrun(xctestrun)
    restore_command = _run_test(
        xctestrun=restore_xctestrun,
        device=device,
        result=restore_result,
        log=restore_result.with_name(restore_result.name + ".log"),
        timeout=timeout,
    )

    source_raw = xctestrun.read_bytes()
    inspect_command = _run_test(
        xctestrun=xctestrun,
        device=device,
        result=inspect_result,
        log=inspect_result.with_name(inspect_result.name + ".log"),
        timeout=timeout,
    )
    _require(inspect_command[0:1] == ["xcodebuild"], "inspection launcher drift")
    _write_json(
        inspection_input,
        {
            "command": inspect_command,
            "original_journal_sha256": journal_sha256,
            "owner_sha256": owner_sha256,
            "result": str(inspect_result),
            "staged": str(xctestrun),
            "staged_sha256": _digest(source_raw),
        },
    )
    proof = collect_inspection(
        inspection_input,
        inspection_output,
        device=device,
        owner_sha256=owner_sha256,
        journal_sha256=journal_sha256,
    )
    proof_path = inspection_output.with_name(inspection_output.name + ".json")
    _write_json(proof_path, proof)
    return {
        "restore_xctestrun": str(restore_xctestrun),
        "restore_result": str(restore_result),
        "restore_command": restore_command,
        "inspection_input": str(inspection_input),
        "inspection_output": str(inspection_output),
        "inspection_proof": str(proof_path),
        "inspection_result": str(inspect_result),
        "inspection_command": inspect_command,
        "inspected_switch_value": proof.get("inspected_switch_value"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--xctestrun", type=Path, required=True)
    parser.add_argument("--restore-result", type=Path, required=True)
    parser.add_argument("--inspection-result", type=Path, required=True)
    parser.add_argument("--owner-sha256", required=True)
    parser.add_argument("--journal-sha256", required=True)
    parser.add_argument("--inspection-input", type=Path, required=True)
    parser.add_argument("--inspection-output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)
    try:
        value = run(
            device=args.device,
            xctestrun=args.xctestrun,
            restore_result=args.restore_result,
            inspect_result=args.inspection_result,
            owner_sha256=args.owner_sha256,
            journal_sha256=args.journal_sha256,
            inspection_input=args.inspection_input,
            inspection_output=args.inspection_output,
            timeout=args.timeout,
        )
    except (RadioRestoreError, ios_sdk.IosSdkError, OSError, ValueError) as error:
        print(f"ios-radio-restore=failed error={error}", file=sys.stderr)
        return 1
    print(json.dumps(value, sort_keys=True, indent=2))
    print("ios-radio-restore=passed inspected_switch_value=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
