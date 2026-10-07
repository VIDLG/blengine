"""Build or explicitly run the opt-in BLeng iOS artifact consumer."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from tools.apple import consumer_ios, xcodegen_toolchain

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "tests/consumers/apple/bleng-ios/project.yml"
PACKAGE = ROOT / ".work/staged/apple-bleng"
WORK = ROOT / ".work/apple-bleng-ios-consumer"
PROJECT = WORK / "BlengIOSArtifactConsumer.xcodeproj"
SCHEME = "BlengIOSArtifactConsumer"
RESULT_SOURCE = "Documents/bleng-ios-artifact-result.txt"


def verify_package() -> None:
    required = (
        PACKAGE / "Package.swift",
        PACKAGE / "BleHubUniFfiNative.xcframework/Info.plist",
        PACKAGE / "Sources/BleHubUniFfi/blehub.swift",
        PACKAGE / "Sources/BleHubUniFfi/bleng.swift",
    )
    flavor = PACKAGE / "BleHubUniFfi.flavor"
    if (
        not flavor.is_file()
        or flavor.read_text(encoding="utf-8").strip() != "bleng"
        or any(not path.is_file() for path in required)
    ):
        raise RuntimeError(
            "missing staged two-component BLeng package; run "
            "`pixi run just ffi apple-bleng-package`"
        )


def validate_linked_symbols(symbols: str) -> None:
    actual = set(re.findall(r"^_uniffi_([a-z][a-z0-9]*)_", symbols, re.MULTILINE))
    expected = {"blehub", "bleng"}
    if actual != expected:
        raise RuntimeError(
            f"unexpected iOS consumer UniFFI components: "
            f"expected {sorted(expected)}, got {sorted(actual)}"
        )
    for token in ("_uniffi_blehub_ffi_test_harness_", "_uniffi_bleng_foreignprobe_"):
        if token in symbols:
            raise RuntimeError("iOS consumer linked a test-only UniFFI symbol")


def validate_linked_images(images: dict[str, str]) -> None:
    owners = {
        name: symbols
        for name, symbols in images.items()
        if re.search(r"^_uniffi_", symbols, re.MULTILINE)
    }
    if len(owners) != 1:
        raise RuntimeError(
            f"expected exactly one linked UniFFI image in iOS app, got {sorted(owners)}"
        )
    validate_linked_symbols(next(iter(owners.values())))


def generate() -> None:
    if platform.system() != "Darwin":
        raise RuntimeError("iOS BLeng consumer requires the project Mac with Xcode")
    verify_package()
    WORK.mkdir(parents=True, exist_ok=True)
    consumer_ios.run(
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
    print(f"apple_bleng_ios_project={PROJECT}", flush=True)


def run_signed_build(command: list[str], log: Path) -> None:
    # Apple signing must never inherit a terminal or receive a password from
    # configuration. A human prepares the login keychain outside this recipe.
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 600
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"signed BLeng build timed out; log: {log}")
                try:
                    returncode = process.wait(timeout=min(30, remaining))
                    break
                except subprocess.TimeoutExpired:
                    print("apple_bleng_ios_signing=still_building", flush=True)
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
    if returncode != 0:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        print("\n".join(lines[-25:]), file=sys.stderr)
        raise RuntimeError(
            f"signed BLeng build failed (exit {returncode}); log: {log}. "
            "Unlock the login keychain locally if signing requires authentication"
        )


def build(destination: str, variant: str, *, team: str | None = None) -> Path:
    print(f"apple_bleng_ios_build={variant}", flush=True)
    derived = WORK / variant / "DerivedData"
    command = [
        consumer_ios.executable("xcodebuild"),
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
    if team is None:
        command.append("CODE_SIGNING_ALLOWED=NO")
    else:
        command.extend(
            [
                "-allowProvisioningUpdates",
                "-allowProvisioningDeviceRegistration",
                f"DEVELOPMENT_TEAM={team}",
                "CODE_SIGN_STYLE=Automatic",
                "CODE_SIGN_IDENTITY=Apple Development",
                "ONLY_ACTIVE_ARCH=YES",
                f"PRODUCT_BUNDLE_IDENTIFIER=dev.blehub.{team.lower()}.bleng-ios-artifact-consumer",
            ]
        )
    command.append("build")
    log = WORK / f"{variant}-xcodebuild.log"
    if team is None:
        consumer_ios.run(command, timeout=600, log=log)
    else:
        run_signed_build(command, log)
    configuration = (
        "Debug-iphonesimulator" if variant == "ios-simulator" else "Debug-iphoneos"
    )
    app = derived / "Build/Products" / configuration / f"{SCHEME}.app"
    executable = app / SCHEME
    if not executable.is_file():
        raise RuntimeError(
            f"unsigned build did not produce an iOS executable: {executable}"
        )
    nm = consumer_ios.executable("nm")
    # Debug Xcode builds put application code in <executable>.debug.dylib;
    # release builds may keep it in the executable. Inspect all app Mach-O
    # images and require the UniFFI symbols in exactly one of them.
    images: dict[str, str] = {}
    for binary in (executable, *sorted(app.rglob("*.dylib"))):
        result = subprocess.run(
            [nm, "-gj", str(binary)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode:
            raise RuntimeError(f"nm failed for {binary}: {result.stderr.strip()}")
        images[str(binary.relative_to(app))] = result.stdout
    validate_linked_images(images)
    if team is not None and not (app / "embedded.mobileprovision").is_file():
        raise RuntimeError(f"signed BLeng app lacks provisioning: {app}")
    flavor = "signed" if team is not None else "unsigned"
    print(f"apple_bleng_ios_{flavor}_{variant}=passed", flush=True)
    return app


def classify_result(contents: str, run_id: str) -> bool | None:
    lines = contents.splitlines()
    if f"apple_bleng_ios_run_id={run_id}" not in lines:
        return None
    if "apple_bleng_ios_artifact_consumer=failed" in lines:
        raise RuntimeError(f"BLeng iOS app reported failure: {contents.strip()}")
    if "apple_bleng_ios_artifact_consumer=passed" not in lines:
        raise RuntimeError("BLeng iOS app result has no terminal status")
    return True


def device_json(
    xcrun: str, args: list[str], name: str, *, timeout: int = 60, work: Path = WORK
) -> dict[str, Any]:
    output = work / name
    is_launch = args[:3] == ["device", "process", "launch"]
    device_id = None
    if "--device" in args:
        device_id = args[args.index("--device") + 1]
    for attempt in range(4):
        output.unlink(missing_ok=True)
        result = subprocess.run(
            [xcrun, "devicectl", *args, "--quiet", "--json-output", str(output)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if not result.returncode and output.is_file():
            value: dict[str, Any] = json.loads(output.read_text(encoding="utf-8"))
            return value
        locked = output.is_file() and "Locked" in output.read_text(
            encoding="utf-8", errors="replace"
        )
        if not (is_launch and locked and device_id and attempt < 3):
            raise RuntimeError(
                f"devicectl {' '.join(args[:3])} failed (exit {result.returncode})"
            )
        print(
            f"apple_bleng_device=launch_retry_locked attempt={attempt + 1}",
            flush=True,
        )
        wait_for_unlocked_device(xcrun, device_id, timeout=45, work=work)
        time.sleep(1)
    raise AssertionError("unreachable launch retry loop")


def wait_for_connected_device(
    xcrun: str,
    *,
    timeout: int = 180,
    work: Path = WORK,
) -> tuple[str, str, str]:
    """Wait for CoreDevice to restore the wired tunnel after install."""
    deadline = time.monotonic() + timeout
    announced = False
    while time.monotonic() < deadline:
        try:
            return connected_device(xcrun, work=work)
        except RuntimeError:
            if not announced:
                print(
                    "apple_bleng_device=waiting_for_wired_tunnel timeout=180s",
                    flush=True,
                )
                announced = True
            time.sleep(2)
    raise TimeoutError("iPhone wired CoreDevice tunnel did not reconnect before launch")


def wait_for_unlocked_device(
    xcrun: str,
    device_id: str,
    *,
    timeout: int = 180,
    work: Path = WORK,
) -> None:
    """Wait for the physical device to be launchable after build/install."""
    deadline = time.monotonic() + timeout
    announced = False
    while time.monotonic() < deadline:
        try:
            lock_state = (
                device_json(
                    xcrun,
                    ["device", "info", "lockState", "--device", device_id],
                    "device-lock-state.json",
                    timeout=30,
                    work=work,
                )
                or {}
            )
            state = lock_state.get("result") or {}
        except RuntimeError:
            state = {}
        if state.get("passcodeRequired") is False and state.get("unlockedSinceBoot"):
            print("apple_bleng_device=unlocked", flush=True)
            return
        if not announced:
            print(
                "apple_bleng_device=waiting_for_unlock timeout=180s",
                flush=True,
            )
            announced = True
        time.sleep(2)
    raise TimeoutError(
        "iPhone remained locked before launch; unlock it in iPhone Mirroring "
        "and rerun the bounded device gate"
    )


def connected_device(xcrun: str, *, work: Path = WORK) -> tuple[str, str, str]:
    try:
        return consumer_ios.device_and_team(xcrun)
    except RuntimeError as error:
        if "expected one connected physical iPhone" not in str(error):
            raise
    devices = (
        device_json(xcrun, ["list", "devices"], "device-list.json", work=work)
        .get("result", {})
        .get("devices", [])
    )
    selector = os.environ.get("BLEHUB_APPLE_DEVICE")
    paired = [
        device
        for device in devices
        if device.get("hardwareProperties", {}).get("reality") == "physical"
        and device.get("deviceProperties", {}).get("developerModeStatus") == "enabled"
        and device.get("connectionProperties", {}).get("pairingState") == "paired"
        and device.get("connectionProperties", {}).get("transportType") == "wired"
        and device.get("connectionProperties", {}).get("tunnelState") == "disconnected"
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
    if len(paired) != 1:
        raise RuntimeError(
            "expected one connected physical iPhone; pair and unlock a wired device first"
        )
    print("apple_bleng_device=checking_wired_tunnel", flush=True)
    device_json(
        xcrun,
        ["device", "info", "details", "--device", str(paired[0]["identifier"])],
        "device-details.json",
        timeout=30,
        work=work,
    )
    return consumer_ios.device_and_team(xcrun)


def device_run() -> None:
    xcrun = consumer_ios.executable("xcrun")
    device_id, udid, team = connected_device(xcrun)
    bundle = f"dev.blehub.{team.lower()}.bleng-ios-artifact-consumer"
    print("apple_bleng_ios_device=building_signed", flush=True)
    app = build(f"id={udid}", "ios-signed-device", team=team)
    print("apple_bleng_ios_device=installing", flush=True)
    device_json(
        xcrun,
        ["device", "install", "app", "--device", device_id, str(app)],
        "device-install.json",
        timeout=120,
    )
    device_id, _, _ = wait_for_connected_device(xcrun, work=WORK)
    wait_for_unlocked_device(xcrun, device_id)
    run_id = str(uuid.uuid4())
    print(f"apple_bleng_ios_device=launching run_id={run_id}", flush=True)
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
        "device-launch.json",
        timeout=60,
    )
    result_file = WORK / "device-result.txt"
    deadline = time.monotonic() + 90
    attempt = 0
    while time.monotonic() < deadline:
        result_file.unlink(missing_ok=True)
        try:
            device_json(
                xcrun,
                [
                    "device",
                    "copy",
                    "from",
                    "--device",
                    device_id,
                    "--source",
                    RESULT_SOURCE,
                    "--destination",
                    str(result_file),
                    "--domain-type",
                    "appDataContainer",
                    "--domain-identifier",
                    bundle,
                ],
                "device-copy-result.json",
                timeout=30,
            )
        except RuntimeError:
            pass  # The result file may not exist immediately after launch.
        if result_file.is_file() and classify_result(
            result_file.read_text(encoding="utf-8"), run_id
        ):
            print("apple_bleng_ios_device=passed", flush=True)
            return
        attempt += 1
        if attempt % 5 == 0:
            print("apple_bleng_ios_device=awaiting_result", flush=True)
        time.sleep(2)
    raise TimeoutError(f"BLeng iOS result not observed within 90 seconds; see {WORK}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("build-unsigned", "device"))
    args = parser.parse_args()
    try:
        generate()
        if args.command == "device":
            device_run()
        else:
            build("generic/platform=iOS", "ios-device")
            build("generic/platform=iOS Simulator", "ios-simulator")
    except (
        RuntimeError,
        OSError,
        TimeoutError,
        ValueError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
