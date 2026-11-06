"""Independent, artifact-only iOS package consumer (no CoreBluetooth or RF)."""

from __future__ import annotations

import argparse
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
from pathlib import Path
from typing import Any

from tools.apple import xcodegen_toolchain
from tools.local_config import resolve_local_value

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "tests/consumers/apple/blehub/ios/project.yml"
PACKAGE = ROOT / ".work/staged/apple-production"
BACKEND_PACKAGE = ROOT / ".work/staged/apple-backend-production"
WORK = ROOT / ".work/apple-ios-artifact-consumer"
PROJECT = WORK / "BleHubIOSArtifactConsumer.xcodeproj"
SCHEME = "BleHubIOSArtifactConsumer"
RESULT_SOURCE = "Documents/blehub-ios-artifact-result.txt"


def executable(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise RuntimeError(f"missing executable: {name}")
    return found


def run(command: list[str], *, timeout: int, log: Path | None = None) -> None:
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=False, timeout=timeout
    )
    if result.returncode:
        if log is not None:
            log.write_text(result.stdout + result.stderr, encoding="utf-8")
            for line in (result.stdout + result.stderr).splitlines():
                if "error:" in line or "errSec" in line or "** BUILD" in line:
                    print(line, file=sys.stderr)
            print(f"full failure log: {log}", file=sys.stderr)
        else:
            print(result.stdout + result.stderr, file=sys.stderr)
        raise RuntimeError(f"command failed (exit {result.returncode}): {command[0]}")


def device_json(
    xcrun: str, args: list[str], name: str, timeout: int = 60
) -> dict[str, Any]:
    output = WORK / name
    output.unlink(missing_ok=True)
    run(
        [xcrun, "devicectl", *args, "--quiet", "--json-output", str(output)],
        timeout=timeout,
    )
    data: dict[str, Any] = json.loads(output.read_text(encoding="utf-8"))
    return data


def generate() -> None:
    if platform.system() != "Darwin":
        raise RuntimeError("iOS consumer requires the project Mac with Xcode")
    if (
        not (PACKAGE / "BleHubUniFfiNative.xcframework").is_dir()
        or not (PACKAGE / "Package.swift").is_file()
    ):
        raise RuntimeError(
            "missing staged production package; run `pixi run just apple prepare`"
        )
    if (
        not (BACKEND_PACKAGE / "Package.swift").is_file()
        or not (BACKEND_PACKAGE / "Sources/BleHubApple").is_dir()
    ):
        raise RuntimeError(
            "missing staged backend package; run `pixi run just apple prepare`"
        )
    WORK.mkdir(parents=True, exist_ok=True)
    run(
        [
            str(xcodegen_toolchain.verify()),
            "generate",
            "--quiet",
            "--spec",
            str(SPEC),
            "--project",
            str(WORK),
        ],
        timeout=60,
    )
    if not (PROJECT / "project.pbxproj").is_file():
        raise RuntimeError(f"XcodeGen did not create {PROJECT}")
    print(f"apple_ios_artifact_project={PROJECT}", flush=True)


def build(destination: str, *, signed: bool, team: str | None = None) -> Path:
    derived = WORK / ("DeviceDerivedData" if signed else "UnsignedDerivedData")
    bundle = f"dev.blehub.{team.lower()}.ios-artifact-consumer" if team else None
    command = [
        executable("xcodebuild"),
        "-quiet",
        "-project",
        str(PROJECT),
        "-scheme",
        SCHEME,
        "-destination",
        destination,
        "-derivedDataPath",
        str(derived),
        f"INFOPLIST_FILE={SPEC.parent / 'Info.plist'}",
    ]
    if signed:
        assert team is not None and bundle is not None
        command.extend(
            [
                "-allowProvisioningUpdates",
                "-allowProvisioningDeviceRegistration",
                f"DEVELOPMENT_TEAM={team}",
                f"PRODUCT_BUNDLE_IDENTIFIER={bundle}",
                "CODE_SIGN_STYLE=Automatic",
                "ONLY_ACTIVE_ARCH=YES",
            ]
        )
    else:
        command.append("CODE_SIGNING_ALLOWED=NO")
    command.append("build")
    run(command, timeout=600, log=WORK / "xcodebuild.log")
    configuration = (
        "Debug-iphoneos"
        if destination != "generic/platform=iOS Simulator"
        else "Debug-iphonesimulator"
    )
    app = derived / "Build/Products" / configuration / f"{SCHEME}.app"
    if not app.is_dir():
        raise RuntimeError(f"build did not produce {app}")
    if signed and not (app / "embedded.mobileprovision").is_file():
        raise RuntimeError("signed app has no embedded provisioning profile")
    print(
        f"apple_ios_artifact_{'signed' if signed else 'unsigned'}_build=passed destination={destination}"
    )
    return app


def device_and_team(xcrun: str) -> tuple[str, str, str]:
    devices = (
        device_json(xcrun, ["list", "devices"], "devices.json")
        .get("result", {})
        .get("devices", [])
    )
    selector = os.environ.get("BLEHUB_APPLE_DEVICE")
    connected = [
        device
        for device in devices
        if device.get("connectionProperties", {}).get("tunnelState") == "connected"
        and device.get("hardwareProperties", {}).get("platform") == "iOS"
        and device.get("hardwareProperties", {}).get("reality") == "physical"
        and (
            not selector
            or selector
            in {
                device.get("identifier"),
                device.get("hardwareProperties", {}).get("udid"),
                device.get("deviceProperties", {}).get("name"),
            }
        )
    ]
    if len(connected) != 1:
        raise RuntimeError(
            "expected one connected physical iPhone; set BLEHUB_APPLE_DEVICE if needed"
        )
    device = connected[0]
    if device.get("deviceProperties", {}).get("developerModeStatus") != "enabled":
        raise RuntimeError("iPhone Developer Mode is not enabled")
    configured = resolve_local_value(
        "apple", "development_team", "BLEHUB_APPLE_DEVELOPMENT_TEAM", None
    ).value
    if configured is None:
        defaults = subprocess.run(
            [executable("defaults"), "export", "com.apple.dt.Xcode", "-"],
            check=True,
            capture_output=True,
        )
        teams = plistlib.loads(defaults.stdout).get(
            "IDEProvisioningTeamByIdentifier", {}
        )
        paid = {
            entry.get("teamID")
            for entries in teams.values()
            for entry in entries
            if not entry.get("isFreeProvisioningTeam", True)
            and isinstance(entry.get("teamID"), str)
        }
        if len(paid) != 1:
            raise RuntimeError(
                "select a non-Personal team via BLEHUB_APPLE_DEVELOPMENT_TEAM"
            )
        configured = paid.pop()
    if re.fullmatch(r"[A-Z0-9]{10}", configured) is None:
        raise RuntimeError("invalid development Team ID")
    identity = subprocess.run(
        [executable("security"), "find-identity", "-v", "-p", "codesigning"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if identity.returncode or not re.search(
        r"\b[1-9]\d* valid identities found", identity.stdout
    ):
        raise RuntimeError(
            "no visible signing identity; unlock the login keychain manually in a local Terminal"
        )
    return (
        str(device["identifier"]),
        str(device["hardwareProperties"]["udid"]),
        configured,
    )


def classify_result(contents: str, run_id: str) -> bool | None:
    """Ignore stale receipts; accept only this run's complete, unique evidence."""
    lines = contents.splitlines()
    identity = f"apple_ios_artifact_run_id={run_id}"
    if identity not in lines:
        return None
    identities = [
        line for line in lines if line.startswith("apple_ios_artifact_run_id=")
    ]
    if identities != [identity]:
        raise RuntimeError("iOS artifact result has ambiguous run identity")
    if "apple_ios_artifact_consumer=failed" in lines:
        raise RuntimeError(
            f"iOS artifact consumer reported failure: {contents.strip()}"
        )
    terminals = [
        line for line in lines if line.startswith("apple_ios_artifact_consumer=")
    ]
    if terminals != ["apple_ios_artifact_consumer=passed"]:
        raise RuntimeError("iOS artifact result has no terminal status or is ambiguous")
    resources = [line for line in lines if line.startswith("backend_resources=")]
    if resources != ["backend_resources=passed"]:
        raise RuntimeError("iOS artifact result lacks successful backend resources")
    return True


def device_run() -> None:
    xcrun = executable("xcrun")
    device_id, udid, team = device_and_team(xcrun)
    bundle = f"dev.blehub.{team.lower()}.ios-artifact-consumer"
    # No installation or signing is attempted by the ordinary unsigned consumer recipe.
    print("apple_ios_artifact_device=explicit_signed_build", flush=True)
    app = build(f"id={udid}", signed=True, team=team)
    print(f"apple_ios_artifact_device=installing {app}", flush=True)
    device_json(
        xcrun,
        ["device", "install", "app", "--device", device_id, str(app)],
        "install.json",
        120,
    )
    run_id = str(uuid.uuid4())
    print(f"apple_ios_artifact_device=launching run_id={run_id}", flush=True)
    device_json(
        xcrun,
        [
            "device",
            "process",
            "launch",
            "--device",
            device_id,
            "--terminate-existing",
            "--activate",
            bundle,
            "--run-id",
            run_id,
        ],
        "launch.json",
    )
    destination = WORK / "result.txt"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        destination.unlink(missing_ok=True)
        output = WORK / "copy-result.json"
        output.unlink(missing_ok=True)
        result = subprocess.run(
            [
                xcrun,
                "devicectl",
                "device",
                "copy",
                "from",
                "--device",
                device_id,
                "--source",
                RESULT_SOURCE,
                "--destination",
                str(destination),
                "--domain-type",
                "appDataContainer",
                "--domain-identifier",
                bundle,
                "--quiet",
                "--json-output",
                str(output),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if (
            result.returncode == 0
            and destination.is_file()
            and classify_result(destination.read_text(encoding="utf-8"), run_id)
        ):
            print(f"apple_ios_artifact_result={destination}")
            print("apple_ios_artifact_consumer=passed")
            return
        time.sleep(1)
    raise TimeoutError(f"timed out waiting for iPhone result; see {WORK}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build-unsigned", "device"))
    args = parser.parse_args()
    try:
        generate()
        if args.command == "device":
            device_run()
        else:
            build("generic/platform=iOS", signed=False)
            build("generic/platform=iOS Simulator", signed=False)
    except (RuntimeError, TimeoutError, subprocess.TimeoutExpired, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
