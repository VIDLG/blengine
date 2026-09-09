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
from tools.local_config import resolve_local_value

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "tests" / "hil" / "apple" / "ios"
ANDROID_PROFILE_PATH = (
    PROJECT_ROOT / "tests" / "conformance" / "scenarios" / "android-peripheral.json"
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
                if "error:" in line or line.startswith("** BUILD")
            ]
            for line in important[-40:]:
                print(line, file=sys.stderr)
            print(f"full failure log: {failure_log}", file=sys.stderr)
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
        document = json.loads(ANDROID_PROFILE_PATH.read_text(encoding="utf-8"))
        service_uuids = document["advertising"]["service_uuids"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"cannot read Android Peripheral service UUID from {ANDROID_PROFILE_PATH}"
        ) from error
    if not isinstance(service_uuids, list) or len(service_uuids) != 1:
        raise RuntimeError(
            "Android Peripheral profile must advertise exactly one service UUID"
        )
    service_uuid = service_uuids[0]
    if not isinstance(service_uuid, str) or not service_uuid:
        raise RuntimeError("Android Peripheral service UUID must be a non-empty string")
    return service_uuid


def _run_xcodegen(xcodegen: str) -> None:
    _ = _run_captured(
        [
            xcodegen,
            "generate",
            "--quiet",
            "--spec",
            str(PROJECT_SPEC),
            "--project",
            str(PACKAGE_ROOT),
        ],
        timeout=60,
        success_marker="apple_ios_xcode_project=generated",
    )
    if not PROJECT_PATH.is_dir():
        raise RuntimeError(f"XcodeGen did not create {PROJECT_PATH}")


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
    if rf_service_uuid is not None:
        launch_arguments.extend(["--rf-service-uuid", rf_service_uuid])
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


def _try_pull_result(xcrun: str, device: IOSDevice, bundle_id: str) -> str | None:
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
            RESULT_SOURCE,
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
) -> str:
    deadline = time.monotonic() + 150
    expected_run_id = f"apple_ios_probe_run_id={run_id}"
    while time.monotonic() < deadline:
        result = _try_pull_result(xcrun, device, bundle_id)
        if result is None or expected_run_id not in result.splitlines():
            time.sleep(1)
            continue
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


def _require_unlocked_login_keychain(security: str) -> set[str]:
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
            "the login keychain has no usable code-signing identity; "
            f"run `security unlock-keychain {default_keychain}` once in a local "
            "interactive Terminal, then retry"
        )
    print("apple_login_keychain=usable")
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
    _require_unlocked_login_keychain(security)


def _environment() -> tuple[str, str, str, IOSDevice, str, str]:
    if platform.system() != "Darwin":
        raise RuntimeError("the Apple iOS runtime probe requires macOS")
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    xcodegen = str(xcodegen_toolchain.verify())
    xcodebuild = _required_executable("xcodebuild")
    xcrun = _required_executable("xcrun")
    defaults = _required_executable("defaults")
    security = _required_executable("security")
    keychain_team_ids = _require_unlocked_login_keychain(security)
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


def runtime(rf_service_uuid: str | None = None) -> None:
    xcodegen, xcodebuild, xcrun, device, team, bundle_id = _environment()
    _print_environment(device, bundle_id)
    _generate_project(xcodegen)
    _build_app(xcodebuild, device, team, bundle_id)
    _install_app(xcrun, device)
    run_id = uuid.uuid4().hex
    _launch_app(xcrun, device, bundle_id, run_id, rf_service_uuid)
    result = _wait_for_runtime_result(xcrun, device, bundle_id, run_id)
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
        if expected_marker not in result.splitlines():
            raise RuntimeError("the iPhone CoreBluetooth RF probe did not pass")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the BleHub iPhone probe")
    _ = parser.add_argument(
        "command",
        choices=(
            "doctor",
            "runtime",
            "runtime-rf",
            "runtime-rf-android",
            "unlock-keychain",
            "project-regenerate",
            "project-confirm",
            "signing",
        ),
    )
    args = parser.parse_args()
    command = getattr(args, "command", None)
    try:
        if command == "project-regenerate":
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
        elif command == "runtime-rf-android":
            runtime(rf_service_uuid=_android_fixture_service_uuid())
        else:
            unlock_login_keychain()
    except subprocess.CalledProcessError:
        return 1
    except (RuntimeError, TimeoutError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
