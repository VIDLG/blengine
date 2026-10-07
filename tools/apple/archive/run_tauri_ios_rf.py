"""Run the signed iOS Tauri UI/RF gate against the nRF52840 fixture."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from tools.local_config import resolve_local_value

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DEVICE = "00008110-0016655002D9401E"
BUNDLE_ID = "dev.blehub.tauri.blehubinternal"
TEST_PROJECT = (
    PROJECT_ROOT
    / "tests/hil/apple/ios/archive/tauri-ui/BleHubTauriInternalUITests.xcodeproj"
)
TEST_SCHEME = "BleHubTauriInternalUITests"
DERIVED = PROJECT_ROOT / ".work/tauri-ios-ui"
ARCHIVE_APP = (
    PROJECT_ROOT
    / "examples/archive/tauri-blehub-internal/src-tauri/gen/apple/build/blehub-tauri-internal_iOS.xcarchive/Products/Applications/BleHub Tauri Internal.app"
)
COORDINATION = PROJECT_ROOT / ".work/peripheral/nrf52840/ios-tauri-rf-coordination"
RESULT = PROJECT_ROOT / ".work/tauri-ios-nrf-rf.xcresult"


def run(
    command: list[str], *, check: bool = True, env: dict[str, str] | None = None
) -> int:
    return subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=check).returncode


def device_ready(device: str) -> None:
    result = subprocess.run(
        ["xcrun", "devicectl", "list", "devices"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    device_lines = [line for line in result.stdout.splitlines() if device in line]
    if not any("connected" in line for line in device_lines):
        raise RuntimeError(
            f"iOS device {device} is not connected; unlock it and pair it before RF"
        )


def build_runner(team: str, device: str) -> Path:
    run(
        [
            "xcodebuild",
            "-quiet",
            "-project",
            str(TEST_PROJECT),
            "-scheme",
            TEST_SCHEME,
            "-destination",
            f"id={device}",
            "-derivedDataPath",
            str(DERIVED),
            "build-for-testing",
            "CODE_SIGN_STYLE=Automatic",
            f"DEVELOPMENT_TEAM={team}",
            "-allowProvisioningUpdates",
            "-allowProvisioningDeviceRegistration",
        ]
    )
    candidates = sorted((DERIVED / "Build/Products").glob("*.xctestrun"))
    if not candidates:
        raise RuntimeError("xcodebuild did not produce an xctestrun file")
    return candidates[-1]


def install_app(device: str) -> None:
    run(
        [
            "xcrun",
            "devicectl",
            "device",
            "install",
            "app",
            "--device",
            device,
            str(ARCHIVE_APP),
        ]
    )


def pair_runner(source: Path) -> Path:
    paired = source.with_name("paired-rf.xctestrun")
    document = plistlib.loads(source.read_bytes())
    target = document[TEST_SCHEME]
    target["UITargetAppPath"] = str(ARCHIVE_APP.resolve())
    target["UITargetAppBundleIdentifier"] = BUNDLE_ID
    target["UseUITargetAppProvidedByTests"] = False
    target.setdefault("EnvironmentVariables", {})["BLEHUB_IOS_TAURI_RF"] = "1"
    target.setdefault("UITargetAppEnvironmentVariables", {})["BLEHUB_IOS_TAURI_RF"] = (
        "1"
    )
    paired.write_bytes(plistlib.dumps(document))
    return paired


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    configured_device = resolve_local_value(
        "apple", "device", "BLEHUB_APPLE_DEVICE", None
    ).value
    parser.add_argument(
        "--device",
        default=configured_device or DEFAULT_DEVICE,
        help="connected iOS device UDID, name, or CoreDevice identifier",
    )
    arguments = parser.parse_args()
    team = resolve_local_value(
        "apple", "development_team", "BLEHUB_APPLE_DEVELOPMENT_TEAM", None
    ).value
    if team is None:
        raise RuntimeError("apple.development_team is required for signed iOS RF")
    device_ready(arguments.device)
    if not ARCHIVE_APP.is_dir():
        raise RuntimeError(
            "signed Tauri iOS archive is missing; use the archived Tauri recipe to build it"
        )
    install_app(arguments.device)
    runner = pair_runner(build_runner(team, arguments.device))
    COORDINATION.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(COORDINATION, ignore_errors=True)
    if RESULT.exists():
        shutil.rmtree(RESULT)
    provider_command = [sys.executable, "-m", "tests.peripherals.nrf52840.provider"]
    run([*provider_command, "stop"])
    try:
        run(
            [
                *provider_command,
                "direct-start",
                "--coordination-path",
                str(COORDINATION),
                "--coordination-mode",
                "ios-tauri",
            ]
        )
        env = os.environ.copy()
        env["BLEHUB_IOS_TAURI_RF"] = "1"
        command = [
            "caffeinate",
            "-dimsu",
            "-t",
            "360",
            "xcodebuild",
            "test-without-building",
            "-xctestrun",
            str(runner),
            "-destination",
            f"id={arguments.device}",
            "-parallel-testing-enabled",
            "NO",
            "-collect-test-diagnostics",
            "never",
            "-resultBundlePath",
            str(RESULT),
        ]
        return run(command, env=env)
    finally:
        run([*provider_command, "stop"])
        status = subprocess.run(
            [*provider_command, "status"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        if "nrf52840_fixture=running" in status.stdout:
            raise RuntimeError(
                "nRF provider remained running after iOS Tauri RF cleanup"
            )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(f"iOS Tauri RF error: {error}", file=sys.stderr)
        raise SystemExit(2)
