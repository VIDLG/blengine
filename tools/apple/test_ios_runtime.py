from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.apple import xcodegen_toolchain
from tools.apple.rf_fixture import (
    RfFixtureConfig,
    add_profile_arguments,
    fixture_from_arguments,
)
from tools.local_config import resolve_local_value

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "tests" / "hil" / "apple" / "ios"
NRF_PROFILE_PATH = (
    PROJECT_ROOT
    / "tests"
    / "conformance"
    / "scenarios"
    / "blehub-nrf-indication-v2.json"
)
PROJECT_PATH = PACKAGE_ROOT / "BleHubAppleIOSProbe.xcodeproj"
PROJECT_SPEC = PACKAGE_ROOT / "project.yml"
PROJECT_FILE = PROJECT_PATH / "project.pbxproj"
WORK_ROOT = PROJECT_ROOT / ".work" / "apple-ios-runtime"
SIGNING_MARKER = WORK_ROOT / "xcode-project-signing-required.txt"
DERIVED_DATA_ROOT = WORK_ROOT / "DerivedData"
APP_PATH = (
    DERIVED_DATA_ROOT
    / "Build"
    / "Products"
    / "Debug-iphoneos"
    / "BleHubAppleIOSProbe.app"
)
SCHEME = "BleHubAppleIOSProbe"
DEFAULT_BUNDLE_ID_PREFIX = "dev.blehub"
RESULT_SOURCE = "Documents/blehub-result.txt"
RF_SCENARIOS = {
    "io",
    "notification",
    "indication",
    "rssi",
    "passive-disconnect",
    "recovery",
}


@dataclass(frozen=True)
class IOSDevice:
    core_device_id: str
    udid: str
    name: str
    model: str
    os_version: str
    developer_mode: str


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"missing required macOS executable: {name}")
    return executable


def _display_command(command: list[str]) -> str:
    redacted: list[str] = []
    redact_next = False
    for argument in command:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
        elif argument in {"DEVELOPMENT_TEAM", "--team"}:
            redacted.append(argument)
            redact_next = True
        elif argument.startswith("DEVELOPMENT_TEAM="):
            redacted.append("DEVELOPMENT_TEAM=<redacted>")
        else:
            redacted.append(argument)
    return " ".join(redacted)


def _run_captured(
    command: list[str],
    *,
    timeout: int,
    success_marker: str | None = None,
    failure_log: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        print("+", _display_command(command), file=sys.stderr)
        if failure_log is None:
            if result.stdout:
                print(result.stdout, end="", file=sys.stderr)
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
        else:
            full_output = result.stdout + result.stderr
            failure_log.write_text(full_output, encoding="utf-8")
            important = [
                line
                for line in full_output.splitlines()
                if "error:" in line
                or "errSec" in line
                or "Command CodeSign failed" in line
                or line.startswith("** BUILD")
            ]
            for line in important[-40:]:
                print(line, file=sys.stderr)
            print(f"full failure log: {failure_log}", file=sys.stderr)
            if "errSecInternalComponent" in full_output:
                print(
                    "Apple signing could not access its key. In your local Terminal, "
                    "run `pixi run just apple unlock-login-keychain` and complete the "
                    "system prompts. If an SSH/agent retry still fails, run the "
                    "original runtime recipe in that same local Terminal; identity "
                    "visibility does not prove private-key access in another session. "
                    "Do not send the password to the agent.",
                    file=sys.stderr,
                )
        raise subprocess.CalledProcessError(result.returncode, command)
    if success_marker is not None:
        print(success_marker)
    return result


def _devicectl_json(
    xcrun: str,
    arguments: list[str],
    output_path: Path,
    *,
    timeout: int,
    success_marker: str | None = None,
) -> dict[str, Any]:
    output_path.unlink(missing_ok=True)
    _ = _run_captured(
        [
            xcrun,
            "devicectl",
            *arguments,
            "--quiet",
            "--json-output",
            str(output_path),
        ],
        timeout=timeout,
        success_marker=success_marker,
    )
    return json.loads(output_path.read_text(encoding="utf-8"))


def _connected_ios_device(xcrun: str) -> IOSDevice:
    def list_devices(output_name: str) -> list[dict[str, Any]]:
        data = _devicectl_json(
            xcrun,
            ["list", "devices"],
            WORK_ROOT / output_name,
            timeout=60,
        )
        return data.get("result", {}).get("devices", [])

    def connected_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            device
            for device in devices
            if device.get("connectionProperties", {}).get("tunnelState") == "connected"
            and device.get("hardwareProperties", {}).get("platform") == "iOS"
            and device.get("hardwareProperties", {}).get("reality") == "physical"
        ]

    devices = list_devices("devices.json")
    connected = connected_devices(devices)
    if not connected:
        wired = [
            device
            for device in devices
            if device.get("connectionProperties", {}).get("transportType") == "wired"
            and device.get("connectionProperties", {}).get("pairingState") == "paired"
            and device.get("hardwareProperties", {}).get("platform") == "iOS"
            and device.get("hardwareProperties", {}).get("reality") == "physical"
        ]
        for device in wired:
            identifier = device.get("identifier")
            if not isinstance(identifier, str):
                continue
            try:
                _ = subprocess.run(
                    [
                        xcrun,
                        "devicectl",
                        "device",
                        "info",
                        "details",
                        "--device",
                        identifier,
                        "--quiet",
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                continue
        if wired:
            connected = connected_devices(list_devices("devices-refreshed.json"))
    selector = os.environ.get("BLEHUB_APPLE_DEVICE")
    if selector:
        connected = [
            device
            for device in connected
            if selector
            in {
                device.get("identifier"),
                device.get("hardwareProperties", {}).get("udid"),
                device.get("deviceProperties", {}).get("name"),
            }
        ]
    if len(connected) != 1:
        detail = "set BLEHUB_APPLE_DEVICE to the connected device name, CoreDevice ID, or UDID"
        raise RuntimeError(
            f"expected exactly one connected physical iOS device, found {len(connected)}; {detail}"
        )

    device = connected[0]
    hardware = device.get("hardwareProperties", {})
    properties = device.get("deviceProperties", {})
    core_device_id = device.get("identifier")
    udid = hardware.get("udid")
    name = properties.get("name")
    model = hardware.get("marketingName")
    os_version = properties.get("osVersionNumber")
    developer_mode = properties.get("developerModeStatus")
    required = (core_device_id, udid, name, model, os_version, developer_mode)
    if not all(isinstance(value, str) and value for value in required):
        raise RuntimeError("connected iOS device metadata is incomplete")
    if developer_mode != "enabled":
        raise RuntimeError(f"Developer Mode is not enabled on {name}")
    return IOSDevice(
        core_device_id=core_device_id,
        udid=udid,
        name=name,
        model=model,
        os_version=os_version,
        developer_mode=developer_mode,
    )


def _development_team(defaults: str) -> str:
    configured = resolve_local_value(
        "apple", "development_team", "BLEHUB_APPLE_DEVELOPMENT_TEAM", None
    ).value
    if configured:
        if re.fullmatch(r"[A-Z0-9]{10}", configured) is None:
            raise RuntimeError(
                "the configured Apple development team must be a 10-character Team ID"
            )
        return configured

    result = subprocess.run(
        [defaults, "export", "com.apple.dt.Xcode", "-"],
        check=True,
        capture_output=True,
    )
    preferences = plistlib.loads(result.stdout)
    teams = preferences.get("IDEProvisioningTeamByIdentifier", {})
    paid_team_ids = {
        entry.get("teamID")
        for entries in teams.values()
        for entry in entries
        if not entry.get("isFreeProvisioningTeam", True)
        and isinstance(entry.get("teamID"), str)
    }
    if len(paid_team_ids) != 1:
        raise RuntimeError(
            "cannot select one non-Personal Xcode development team; set "
            "BLEHUB_APPLE_DEVELOPMENT_TEAM explicitly"
        )
    return paid_team_ids.pop()


def _bundle_id(team: str) -> str:
    configured = os.environ.get("BLEHUB_APPLE_IOS_PROBE_BUNDLE_ID")
    if configured:
        return configured
    return f"{DEFAULT_BUNDLE_ID_PREFIX}.{team.lower()}.ios-probe"


def _android_fixture_service_uuid() -> str:
    try:
        document = json.loads(NRF_PROFILE_PATH.read_text(encoding="utf-8"))
        service_uuids = document["advertising"]["service_uuids"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"cannot read Android Peripheral service UUID from {NRF_PROFILE_PATH}"
        ) from error
    if not isinstance(service_uuids, list) or len(service_uuids) != 1:
        raise RuntimeError(
            "Android Peripheral profile must advertise exactly one service UUID"
        )
    service_uuid = service_uuids[0]
    if not isinstance(service_uuid, str) or not service_uuid:
        raise RuntimeError("Android Peripheral service UUID must be a non-empty string")
    return service_uuid


def _run_xcodegen(xcodegen: str, project_root: Path = PACKAGE_ROOT) -> None:
    _ = _run_captured(
        [
            xcodegen,
            "generate",
            "--quiet",
            "--spec",
            str(PROJECT_SPEC),
            "--project",
            str(project_root),
        ],
        timeout=60,
        success_marker="apple_ios_xcode_project=generated",
    )
    project = project_root / PROJECT_PATH.name
    if not project.is_dir():
        raise RuntimeError(f"XcodeGen did not create {project}")


def build_unsigned() -> None:
    """Compile the actual iPhone app without touching its manually signed project."""
    if platform.system() != "Darwin":
        raise RuntimeError("the iOS HIL build requires macOS")
    project_root = WORK_ROOT / "unsigned-project"
    project_root.mkdir(parents=True, exist_ok=True)
    _run_xcodegen(str(xcodegen_toolchain.verify()), project_root)
    _ = _run_captured(
        [
            _required_executable("xcodebuild"),
            "-quiet",
            "-project",
            str(project_root / PROJECT_PATH.name),
            "-scheme",
            SCHEME,
            "-destination",
            "generic/platform=iOS",
            "-derivedDataPath",
            str(WORK_ROOT / "UnsignedDerivedData"),
            f"INFOPLIST_FILE={PACKAGE_ROOT / 'BleHubAppleIOSProbe' / 'Info.plist'}",
            "CODE_SIGNING_ALLOWED=NO",
            "build",
        ],
        timeout=600,
        success_marker="apple_ios_hil_unsigned_build=passed",
    )


def _mark_signing_required() -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(PROJECT_SPEC.read_bytes()).hexdigest()
    SIGNING_MARKER.write_text(
        f"project_spec_sha256={digest}\n"
        f"project_path={PROJECT_PATH}\n"
        "Open the project in Xcode, select a development Team, then run "
        "`pixi run just apple ios-project-confirm`.\n",
        encoding="utf-8",
    )


def _regenerate_project(xcodegen: str) -> None:
    _run_xcodegen(xcodegen)
    _mark_signing_required()
    print(f"apple_ios_xcode_project={PROJECT_PATH}")
    print("apple_ios_xcode_signing=automatic-or-interactive")
    print(
        "xcodebuild will try automatic provisioning; use `pixi run just apple ios-signing` for manual setup"
    )


def _generate_project(xcodegen: str) -> None:
    if not PROJECT_PATH.is_dir() or not PROJECT_FILE.is_file():
        _regenerate_project(xcodegen)
        return
    if PROJECT_SPEC.stat().st_mtime > PROJECT_FILE.stat().st_mtime:
        raise RuntimeError(
            "project.yml changed after the generated Xcode project; run "
            "`pixi run just apple ios-project-regenerate` before continuing"
        )
    print("apple_ios_xcode_project=existing")


def _confirm_project_signing() -> None:
    if not PROJECT_FILE.is_file():
        raise RuntimeError(
            "the iOS Xcode project is missing; run "
            "`pixi run just apple ios-project-regenerate`"
        )
    if PROJECT_SPEC.stat().st_mtime > PROJECT_FILE.stat().st_mtime:
        raise RuntimeError(
            "project.yml changed after the generated Xcode project; regenerate "
            "the project before signing it in Xcode"
        )
    project_text = PROJECT_FILE.read_text(encoding="utf-8")
    teams = sorted(set(re.findall(r"DEVELOPMENT_TEAM = ([A-Z0-9]{10});", project_text)))
    if not teams:
        raise RuntimeError(
            "Xcode has not selected a development Team; open the project and "
            "choose one under Signing & Capabilities"
        )
    if 'CODE_SIGN_IDENTITY = "Apple Development";' not in project_text:
        raise RuntimeError(
            "Xcode project is not configured with the Apple Development signing "
            "identity"
        )
    if "CODE_SIGN_STYLE = Automatic;" not in project_text:
        raise RuntimeError("Xcode project is not configured for Automatic code signing")
    SIGNING_MARKER.unlink(missing_ok=True)
    print(f"apple_ios_xcode_signing=confirmed team={','.join(teams)}")


def open_project() -> None:
    if not PROJECT_PATH.is_dir():
        raise RuntimeError(
            "the iOS Xcode project is missing; run "
            "`pixi run just apple ios-project-regenerate` first"
        )
    if platform.system() != "Darwin":
        raise RuntimeError("opening the iOS Xcode project requires macOS")
    subprocess.run(["open", "-a", "Xcode", str(PROJECT_PATH)], check=True)
    print(f"apple_ios_xcode_project_opened={PROJECT_PATH}")


def _build_app(xcodebuild: str, device: IOSDevice, team: str, bundle_id: str) -> None:
    shutil.rmtree(DERIVED_DATA_ROOT, ignore_errors=True)
    _ = _run_captured(
        [
            xcodebuild,
            "-quiet",
            "-project",
            str(PROJECT_PATH),
            "-scheme",
            SCHEME,
            "-configuration",
            "Debug",
            "-destination",
            f"id={device.udid}",
            "-derivedDataPath",
            str(DERIVED_DATA_ROOT),
            "-allowProvisioningUpdates",
            "-allowProvisioningDeviceRegistration",
            f"DEVELOPMENT_TEAM={team}",
            f"PRODUCT_BUNDLE_IDENTIFIER={bundle_id}",
            "CODE_SIGN_STYLE=Automatic",
            "ONLY_ACTIVE_ARCH=YES",
            "build",
        ],
        timeout=600,
        success_marker="apple_ios_signed_device_build=passed",
        failure_log=WORK_ROOT / "xcodebuild.log",
    )
    if not APP_PATH.is_dir():
        raise RuntimeError(f"Xcode did not produce the device app: {APP_PATH}")
    if not (APP_PATH / "embedded.mobileprovision").is_file():
        raise RuntimeError("the iOS probe app has no embedded provisioning profile")
    SIGNING_MARKER.unlink(missing_ok=True)


def _install_app(xcrun: str, device: IOSDevice) -> None:
    _ = _devicectl_json(
        xcrun,
        [
            "device",
            "install",
            "app",
            "--device",
            device.core_device_id,
            str(APP_PATH),
        ],
        WORK_ROOT / "install.json",
        timeout=120,
        success_marker="apple_ios_device_install=passed",
    )


def _launch_app(
    xcrun: str,
    device: IOSDevice,
    bundle_id: str,
    run_id: str,
    rf_service_uuid: str | None = None,
    rf_scenario: str | None = None,
    fixture: RfFixtureConfig | None = None,
) -> None:
    launch_arguments = [
        "device",
        "process",
        "launch",
        "--device",
        device.core_device_id,
        "--terminate-existing",
        "--activate",
        bundle_id,
        "--run-id",
        run_id,
    ]
    if fixture is not None:
        launch_arguments.extend(fixture.app_arguments())
        launch_arguments.extend(["--rf-external-provider", "nrf52840"])
    if rf_service_uuid is not None:
        launch_arguments.extend(["--rf-service-uuid", rf_service_uuid])
        if rf_scenario is not None:
            launch_arguments.extend(["--rf-scenario", rf_scenario])
        target_identifier = os.environ.get("BLEHUB_APPLE_RF_TARGET_IDENTIFIER")
        unfiltered = os.environ.get("BLEHUB_APPLE_RF_UNFILTERED") == "1"
        if target_identifier is not None:
            try:
                target_identifier = str(uuid.UUID(target_identifier))
            except ValueError as error:
                raise RuntimeError(
                    "BLEHUB_APPLE_RF_TARGET_IDENTIFIER must be a UUID"
                ) from error
            launch_arguments.extend(["--rf-target-identifier", target_identifier])
        if unfiltered or target_identifier is not None:
            launch_arguments.append("--rf-unfiltered")
        scan_seconds = os.environ.get("BLEHUB_APPLE_RF_SCAN_SECONDS")
        if scan_seconds is not None:
            try:
                if float(scan_seconds) <= 0:
                    raise ValueError
            except ValueError as error:
                raise RuntimeError(
                    "BLEHUB_APPLE_RF_SCAN_SECONDS must be positive"
                ) from error
            launch_arguments.extend(["--rf-scan-seconds", scan_seconds])
        if os.environ.get("BLEHUB_APPLE_RF_NO_DUPLICATES") == "1":
            launch_arguments.append("--rf-no-duplicates")
        if os.environ.get("BLEHUB_APPLE_RF_DUMP_ADVERTISEMENTS") == "1":
            launch_arguments.append("--rf-dump-advertisements")
    _ = _devicectl_json(
        xcrun,
        launch_arguments,
        WORK_ROOT / "launch.json",
        timeout=60,
        success_marker="apple_ios_device_launch=passed",
    )


def _try_pull_result(
    xcrun: str, device: IOSDevice, bundle_id: str, source: str = RESULT_SOURCE
) -> str | None:
    destination = WORK_ROOT / "blehub-result.txt"
    json_output = WORK_ROOT / "copy-result.json"
    destination.unlink(missing_ok=True)
    json_output.unlink(missing_ok=True)
    result = subprocess.run(
        [
            xcrun,
            "devicectl",
            "device",
            "copy",
            "from",
            "--device",
            device.core_device_id,
            "--source",
            source,
            "--destination",
            str(destination),
            "--domain-type",
            "appDataContainer",
            "--domain-identifier",
            bundle_id,
            "--quiet",
            "--json-output",
            str(json_output),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0 or not destination.is_file():
        return None
    return destination.read_text(encoding="utf-8")


def _wait_for_runtime_result(
    xcrun: str,
    device: IOSDevice,
    bundle_id: str,
    run_id: str,
    source: str = RESULT_SOURCE,
) -> str:
    deadline = time.monotonic() + 150
    expected_run_id = f"apple_ios_probe_run_id={run_id}"
    while time.monotonic() < deadline:
        result = _try_pull_result(xcrun, device, bundle_id, source)
        if result is None or expected_run_id not in result.splitlines():
            time.sleep(1)
            continue
        archive = WORK_ROOT / f"{run_id}-result.txt"
        archive.write_text(result, encoding="utf-8")
        print(f"apple_ios_result_path={archive}")
        print(result, end="")
        if "apple_ios_corebluetooth_runtime=passed" not in result.splitlines():
            raise RuntimeError("the iPhone CoreBluetooth probe reported failure")
        return result
    raise TimeoutError("timed out waiting for the iPhone CoreBluetooth result file")


def _default_login_keychain(security: str) -> str:
    return (
        subprocess.run(
            [security, "default-keychain", "-d", "user"],
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .strip('"')
    )


def _require_visible_signing_identity(security: str) -> set[str]:
    default_keychain = _default_login_keychain(security)
    result = subprocess.run(
        [security, "find-identity", "-v", "-p", "codesigning", default_keychain],
        capture_output=True,
        text=True,
        check=False,
    )
    valid_identities = re.search(
        r"^\s*(\d+) valid identities found$",
        result.stdout,
        re.MULTILINE,
    )
    if (
        result.returncode != 0
        or valid_identities is None
        or int(valid_identities.group(1)) == 0
    ):
        raise RuntimeError(
            "the login keychain has no visible valid code-signing identity; "
            f"run `security unlock-keychain {default_keychain}` once in a local "
            "interactive Terminal, then retry"
        )
    # find-identity enumerates certificates; it does not sign with the private key.
    print("apple_signing_identity=visible")
    return {
        team_id
        for team_id in re.findall(
            r'^\s*\d+\)\s+[A-F0-9]{40}\s+"Apple Development: [^"\n]+ \(([A-Z0-9]{10})\)"$',
            result.stdout,
            re.MULTILINE,
        )
    }


def unlock_login_keychain() -> None:
    if platform.system() != "Darwin":
        raise RuntimeError("the Apple login keychain exists only on macOS")
    security = _required_executable("security")
    default_keychain = _default_login_keychain(security)
    print("Enter the Mac login password locally; it is not read or stored by BleHub.")
    subprocess.run([security, "unlock-keychain", default_keychain], check=True)
    subprocess.run(
        [
            security,
            "set-key-partition-list",
            "-S",
            "apple-tool:,apple:,codesign:",
            "-s",
            default_keychain,
        ],
        check=True,
    )
    _require_visible_signing_identity(security)


def _environment() -> tuple[str, str, str, IOSDevice, str, str]:
    if platform.system() != "Darwin":
        raise RuntimeError("the Apple iOS runtime probe requires macOS")
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    xcodegen = str(xcodegen_toolchain.verify())
    xcodebuild = _required_executable("xcodebuild")
    xcrun = _required_executable("xcrun")
    defaults = _required_executable("defaults")
    security = _required_executable("security")
    keychain_team_ids = _require_visible_signing_identity(security)
    device = _connected_ios_device(xcrun)
    team = _development_team(defaults)
    if team not in keychain_team_ids:
        print("apple_ios_signing_identity=delegated_to_xcodebuild")
    bundle_id = _bundle_id(team)
    return xcodegen, xcodebuild, xcrun, device, team, bundle_id


def _print_environment(device: IOSDevice, bundle_id: str) -> None:
    print(f"apple_ios_device_name={device.name}")
    print(f"apple_ios_device_model={device.model}")
    print(f"apple_ios_device_version={device.os_version}")
    print(f"apple_ios_developer_mode={device.developer_mode}")
    print(f"apple_ios_probe_bundle_id={bundle_id}")


def doctor() -> None:
    xcodegen, _, _, device, _, bundle_id = _environment()
    _print_environment(device, bundle_id)
    _generate_project(xcodegen)
    print("apple_ios_device_doctor=passed")


def _copy_marker_to_device(
    xcrun: str, device: IOSDevice, bundle_id: str, run_id: str, marker: str
) -> None:
    local_marker = WORK_ROOT / f"{run_id}-{marker.replace('/', '-')}"
    local_marker.write_text("ready\n", encoding="ascii")
    destination = f"Documents/blehub-rf-{run_id}/{marker}"
    _ = _devicectl_json(
        xcrun,
        [
            "device",
            "copy",
            "to",
            "--device",
            device.core_device_id,
            "--source",
            str(local_marker),
            "--destination",
            destination,
            "--domain-type",
            "appDataContainer",
            "--domain-identifier",
            bundle_id,
        ],
        WORK_ROOT / f"copy-{marker.replace('/', '-')}.json",
        timeout=30,
    )


def _wait_for_device_marker(
    xcrun: str,
    device: IOSDevice,
    bundle_id: str,
    run_id: str,
    marker: str,
    *,
    timeout: int,
) -> None:
    source = f"Documents/blehub-rf-{run_id}/{marker}"
    result_source = f"Documents/blehub-rf-{run_id}/result.txt"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _try_pull_result(xcrun, device, bundle_id, source) is not None:
            return
        result = _try_pull_result(xcrun, device, bundle_id, result_source)
        if result is not None and "apple_ios_corebluetooth_rf=failed" in result:
            raise RuntimeError(
                f"iOS RF probe failed while waiting for marker {marker}: {result.strip()}"
            )
        time.sleep(1)
    raise TimeoutError(f"timed out waiting for iOS RF marker: {marker}")


def _wait_for_host_marker(path: Path, marker: str, *, timeout: int) -> None:
    marker_path = path / marker
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker_path.is_file():
            return
        time.sleep(0.2)
    raise TimeoutError(f"timed out waiting for nRF host marker: {marker_path}")


def _write_host_marker(path: Path, marker: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / marker).write_text("ready\n", encoding="ascii")


def _run_ios_nrf_host_scenario(
    xcrun: str,
    device: IOSDevice,
    bundle_id: str,
    run_id: str,
    scenario: str,
    coordination_path: Path,
) -> None:
    """Bridge iOS app-container markers to the single nRF BTP serial owner."""
    if scenario in {"notification", "indication"}:
        _wait_for_device_marker(
            xcrun, device, bundle_id, run_id, "notification-enabled", timeout=240
        )
        _write_host_marker(coordination_path, "subscription-enabled")
        _wait_for_host_marker(coordination_path, "value-applied", timeout=60)
        _wait_for_device_marker(
            xcrun, device, bundle_id, run_id, "notification-disabled", timeout=60
        )
        _write_host_marker(coordination_path, "post-disable-triggered")
        _copy_marker_to_device(
            xcrun, device, bundle_id, run_id, "post-disable-triggered"
        )
        return

    if scenario == "passive-disconnect":
        _wait_for_device_marker(
            xcrun, device, bundle_id, run_id, "notification-enabled", timeout=240
        )
        _write_host_marker(coordination_path, "subscription-enabled")
        _wait_for_host_marker(coordination_path, "value-applied", timeout=60)
        _wait_for_device_marker(
            xcrun, device, bundle_id, run_id, "radio-off-ready", timeout=60
        )
        _write_host_marker(coordination_path, "connected")
        _wait_for_host_marker(coordination_path, "fault-triggered", timeout=60)
        _copy_marker_to_device(xcrun, device, bundle_id, run_id, "radio-off-confirmed")
        _wait_for_device_marker(
            xcrun, device, bundle_id, run_id, "passive-disconnected", timeout=90
        )
        _write_host_marker(coordination_path, "passive-disconnected")
        _wait_for_host_marker(coordination_path, "recovery-ready", timeout=60)
        _copy_marker_to_device(xcrun, device, bundle_id, run_id, "radio-restored")
        return

    if scenario == "recovery":
        for cycle in range(1, 4):
            cycle_root = coordination_path / f"cycle-{cycle}"
            _wait_for_device_marker(
                xcrun,
                device,
                bundle_id,
                run_id,
                f"cycle-{cycle}/cycle-ready",
                timeout=120,
            )
            _wait_for_host_marker(cycle_root, "fixture-ready", timeout=90)
            _copy_marker_to_device(
                xcrun, device, bundle_id, run_id, f"cycle-{cycle}/fixture-ready"
            )
            _wait_for_device_marker(
                xcrun,
                device,
                bundle_id,
                run_id,
                f"cycle-{cycle}/notification-enabled",
                timeout=180,
            )
            _write_host_marker(cycle_root, "subscription-enabled")
            _wait_for_host_marker(cycle_root, "value-applied", timeout=60)
            _wait_for_device_marker(
                xcrun,
                device,
                bundle_id,
                run_id,
                f"cycle-{cycle}/connected",
                timeout=60,
            )
            _write_host_marker(cycle_root, "connected")
            _wait_for_host_marker(cycle_root, "fault-triggered", timeout=60)
            _copy_marker_to_device(
                xcrun, device, bundle_id, run_id, f"cycle-{cycle}/fault-triggered"
            )
            _wait_for_device_marker(
                xcrun,
                device,
                bundle_id,
                run_id,
                f"cycle-{cycle}/passive-disconnected",
                timeout=90,
            )
            _write_host_marker(cycle_root, "passive-disconnected")
            _wait_for_host_marker(cycle_root, "recovery-ready", timeout=60)
            _copy_marker_to_device(
                xcrun, device, bundle_id, run_id, f"cycle-{cycle}/recovery-ready"
            )
            _wait_for_device_marker(
                xcrun,
                device,
                bundle_id,
                run_id,
                f"cycle-{cycle}/cycle-passed",
                timeout=120,
            )
            _write_host_marker(cycle_root, "cycle-passed")
        return

    raise RuntimeError(
        f"external nRF iOS coordination does not yet support scenario {scenario!r}"
    )


def _run_ios_android_host_scenario(
    xcrun: str, device: IOSDevice, bundle_id: str, run_id: str, scenario: str
) -> None:
    from tools.apple.test_macos_runtime import _trigger_android_notification

    if scenario == "recovery":
        for cycle in range(1, 4):
            cycle_root = f"cycle-{cycle}"
            _wait_for_device_marker(
                xcrun,
                device,
                bundle_id,
                run_id,
                f"{cycle_root}/cycle-ready",
                timeout=120,
            )
            _copy_marker_to_device(
                xcrun, device, bundle_id, run_id, f"{cycle_root}/fixture-ready"
            )
            _wait_for_device_marker(
                xcrun,
                device,
                bundle_id,
                run_id,
                f"{cycle_root}/notification-enabled",
                timeout=180,
            )
            _trigger_android_notification("deadbeef", expect_success=True)
            _wait_for_device_marker(
                xcrun,
                device,
                bundle_id,
                run_id,
                f"{cycle_root}/notification-disabled",
                timeout=60,
            )
            _trigger_android_notification("cafebabe", expect_success=False)
            _copy_marker_to_device(
                xcrun,
                device,
                bundle_id,
                run_id,
                f"{cycle_root}/post-disable-triggered",
            )
            _wait_for_device_marker(
                xcrun,
                device,
                bundle_id,
                run_id,
                f"{cycle_root}/cycle-passed",
                timeout=180,
            )
        return

    if scenario not in {"notification", "indication", "passive-disconnect"}:
        return
    _wait_for_device_marker(
        xcrun, device, bundle_id, run_id, "notification-enabled", timeout=240
    )
    recipe = "android-indicate" if scenario == "indication" else "android-notify"
    _trigger_android_notification("deadbeef", expect_success=True, recipe=recipe)

    if scenario == "passive-disconnect":
        _wait_for_device_marker(
            xcrun, device, bundle_id, run_id, "radio-off-ready", timeout=60
        )
        radio_command = [
            _required_executable("pixi"),
            "run",
            "just",
            "peripheral",
            "android-radio-disable",
        ]
        subprocess.run(radio_command, cwd=PROJECT_ROOT, check=True, timeout=60)
        _copy_marker_to_device(xcrun, device, bundle_id, run_id, "radio-off-confirmed")
        radio_enable = [
            _required_executable("pixi"),
            "run",
            "just",
            "peripheral",
            "android-radio-enable",
        ]
        subprocess.run(radio_enable, cwd=PROJECT_ROOT, check=True, timeout=60)
        subprocess.run(
            [
                _required_executable("pixi"),
                "run",
                "just",
                "peripheral",
                "android-resume",
            ],
            cwd=PROJECT_ROOT,
            check=True,
            timeout=60,
        )
        _copy_marker_to_device(xcrun, device, bundle_id, run_id, "radio-restored")
        return

    _wait_for_device_marker(
        xcrun, device, bundle_id, run_id, "notification-disabled", timeout=60
    )
    _trigger_android_notification("cafebabe", expect_success=False, recipe=recipe)
    _copy_marker_to_device(xcrun, device, bundle_id, run_id, "post-disable-triggered")


def runtime(
    rf_service_uuid: str | None = None,
    scenario: str = "io",
    fixture: RfFixtureConfig | None = None,
    coordination_path: Path | None = None,
) -> None:
    if fixture is not None:
        if scenario not in RF_SCENARIOS:
            raise RuntimeError(f"unsupported external iOS RF scenario: {scenario}")
        if (
            scenario in {"notification", "indication", "passive-disconnect", "recovery"}
            and coordination_path is None
        ):
            raise RuntimeError(
                f"external iOS {scenario} requires --coordination-path for provider marker bridging"
            )
        if os.environ.get("BLEHUB_APPLE_RF_UNFILTERED") == "1" or os.environ.get(
            "BLEHUB_APPLE_RF_TARGET_IDENTIFIER"
        ):
            raise RuntimeError(
                "external RF requires a profile-filtered scan, not diagnostic target/unfiltered overrides"
            )
        rf_service_uuid = fixture.serviceUuid
    xcodegen, xcodebuild, xcrun, device, team, bundle_id = _environment()
    _print_environment(device, bundle_id)
    _generate_project(xcodegen)
    _build_app(xcodebuild, device, team, bundle_id)
    _install_app(xcrun, device)
    run_id = uuid.uuid4().hex
    print(f"apple_ios_probe_run_id={run_id}", flush=True)
    print(f"apple_ios_result_path={WORK_ROOT / f'{run_id}-result.txt'}", flush=True)
    _launch_app(
        xcrun,
        device,
        bundle_id,
        run_id,
        rf_service_uuid,
        scenario if rf_service_uuid else None,
        fixture,
    )
    if fixture is None:
        _run_ios_android_host_scenario(xcrun, device, bundle_id, run_id, scenario)
    elif coordination_path is not None:
        _run_ios_nrf_host_scenario(
            xcrun,
            device,
            bundle_id,
            run_id,
            scenario,
            coordination_path,
        )
    result = _wait_for_runtime_result(
        xcrun,
        device,
        bundle_id,
        run_id,
        f"Documents/blehub-rf-{run_id}/result.txt"
        if rf_service_uuid
        else RESULT_SOURCE,
    )
    if rf_service_uuid is not None:
        target_identifier = os.environ.get("BLEHUB_APPLE_RF_TARGET_IDENTIFIER")
        diagnostic_scan_only = (
            os.environ.get("BLEHUB_APPLE_RF_UNFILTERED") == "1"
            and target_identifier is None
        )
        expected_marker = (
            "apple_ios_corebluetooth_unfiltered_scan=passed"
            if diagnostic_scan_only
            else "apple_ios_corebluetooth_rf=passed"
        )
        required = [expected_marker]
        if fixture is not None:
            required.extend(fixture.evidence())
            required.extend(
                [
                    f"apple_ios_probe_run_id={run_id}",
                    f"apple_ios_rf_scenario={scenario}",
                    f"apple_ios_rf_{scenario}=passed",
                    "apple_ios_rf_ledger=passed",
                    "apple_ios_rf_post_dispose_silence=passed",
                ]
            )
        if not set(required).issubset(result.splitlines()):
            raise RuntimeError(
                "the iPhone CoreBluetooth RF probe did not pass the requested fixture contract"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the BleHub iPhone probe")
    _ = parser.add_argument(
        "command",
        choices=(
            "doctor",
            "runtime",
            "runtime-rf",
            "runtime-rf-android",
            "runtime-rf-external",
            "build-unsigned",
            "unlock-keychain",
            "project-regenerate",
            "project-confirm",
            "signing",
        ),
    )
    _ = parser.add_argument("scenario", nargs="?", choices=sorted(RF_SCENARIOS))
    add_profile_arguments(parser)
    _ = parser.add_argument(
        "--coordination-path",
        type=Path,
        help="host-side nRF coordination directory for external RF scenarios",
    )
    args = parser.parse_args()
    command = getattr(args, "command", None)
    if command != "runtime-rf-external" and (
        args.profile is not None
        or args.write_with_response != "102030"
        or args.write_without_response != "abcd"
        or args.coordination_path is not None
    ):
        parser.error("external RF options require runtime-rf-external")
    try:
        if command == "build-unsigned":
            build_unsigned()
        elif command == "project-regenerate":
            if platform.system() != "Darwin":
                raise RuntimeError("iOS Xcode project generation requires macOS")
            WORK_ROOT.mkdir(parents=True, exist_ok=True)
            _regenerate_project(str(xcodegen_toolchain.verify()))
        elif command == "project-confirm":
            _confirm_project_signing()
        elif command == "signing":
            open_project()
        elif command == "doctor":
            doctor()
        elif command == "runtime":
            runtime()
        elif command == "runtime-rf":
            service_uuid = os.environ.get("BLEHUB_APPLE_RF_SERVICE_UUID")
            if not service_uuid:
                raise RuntimeError(
                    "runtime-rf requires BLEHUB_APPLE_RF_SERVICE_UUID; "
                    "use runtime-rf-android for the managed Android fixture"
                )
            runtime(rf_service_uuid=service_uuid)
        elif command == "runtime-rf-external":
            runtime(
                scenario=args.scenario or "io",
                fixture=fixture_from_arguments(args),
                coordination_path=args.coordination_path,
            )
        elif command == "runtime-rf-android":
            scenario = args.scenario or os.environ.get("BLEHUB_APPLE_RF_SCENARIO", "io")
            if scenario not in RF_SCENARIOS:
                raise RuntimeError(f"unsupported iOS RF scenario: {scenario}")
            runtime(rf_service_uuid=_android_fixture_service_uuid(), scenario=scenario)
        else:
            unlock_login_keychain()
    except subprocess.CalledProcessError:
        return 1
    except (RuntimeError, TimeoutError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
