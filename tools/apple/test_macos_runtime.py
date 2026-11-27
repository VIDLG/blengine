from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from tools import host_command
from tools.apple.rf_fixture import (
    EXTERNAL_MACOS_SCENARIOS,
    RfFixtureConfig,
    add_profile_arguments,
    fixture_from_arguments,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HIL_ROOT = PROJECT_ROOT / "tests" / "hil" / "apple" / "macos"
HIL_SUPPORT_ROOT = PROJECT_ROOT / "tests" / "hil" / "apple" / "support"
# Historical compatibility metadata, never a live RF default or authorization.
HISTORICAL_NRF_PROFILE_PATH = (
    PROJECT_ROOT
    / "tests"
    / "conformance"
    / "scenarios"
    / "blehub-nrf-indication-v2.json"
)
APPLE_SOURCE_ROOT = PROJECT_ROOT / "platforms" / "apple" / "Sources" / "BleHubApple"
PRODUCTION_PACKAGE_ROOT = PROJECT_ROOT / ".work" / "staged" / "apple-production"
MACOS_RF_BLOCKED = (
    "macOS RF blocked: an immutable v2 --fixture-request with matching contract/profile "
    "snapshots and a live receipt under the same physical lease is required. "
    "The native Mac host does not yet verify that session/owner, bridge subscription "
    "and passive-disconnect markers, or perform identity-bound cleanup; RF admission "
    "is disabled even when a request is supplied. No build, signing, fixture start/stop "
    "or BLE action was attempted. Use test-rf-host for software checks or "
    "build-macos-hil for an explicitly requested build/sign without RF."
)
WORK_ROOT = PROJECT_ROOT / ".work" / "apple-macos-runtime"
PACKAGE_ROOT = WORK_ROOT / "package"
BUNDLE_ROOT = WORK_ROOT / "BleHubAppleMacOSProbe.app"
BUNDLE_EXECUTABLE = BUNDLE_ROOT / "Contents" / "MacOS" / "BleHubAppleMacOSProbe"
BUNDLE_SOURCE_HASH = WORK_ROOT / "bundle-source.sha256"
CONNECTION_COMMANDS = {
    "connect-android",
    "notifications-external",
    "strict-indication-external",
    "rssi-android",
    "passive-disconnect-android",
    "passive-disconnect-external",
    "gatt-android",
    "io-android",
    "notifications-android",
    "strict-indication-android",
}
GATT_COMMANDS = {
    "gatt-android",
    "notifications-external",
    "strict-indication-external",
    "io-android",
    "notifications-android",
    "strict-indication-android",
}
IO_COMMANDS = {"io-android"}
NOTIFICATION_COMMANDS = {
    "notifications-android",
    "notifications-external",
    "strict-indication-external",
}
STRICT_INDICATION_COMMANDS = {"strict-indication-android", "strict-indication-external"}
PASSIVE_DISCONNECT_COMMANDS = {
    "passive-disconnect-android",
    "passive-disconnect-external",
}
RF_SCENARIO_COMMANDS = {
    "scan": "scan-android",
    "connection": "connect-android",
    "gatt": "gatt-android",
    "io": "io-android",
    "notification": "notifications-android",
    "indication": "strict-indication-android",
    "rssi": "rssi-android",
    "passive-disconnect": "passive-disconnect-android",
}
RF_ALL_SCENARIOS = tuple(RF_SCENARIO_COMMANDS)
EXTERNAL_RF_COMMANDS = {
    "scan": "scan-android",
    "connection": "connect-android",
    "gatt": "gatt-android",
    "io": "io-android",
    "notification": "notifications-external",
    "indication": "strict-indication-external",
    "rssi": "rssi-android",
    "passive-disconnect": "passive-disconnect-external",
}


def _require_macos_rf_session() -> None:
    """Reject admission until live session verification and owned cleanup are connected."""
    raise RuntimeError(MACOS_RF_BLOCKED)


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"missing required macOS executable: {name}")
    return executable


def _run(command: list[str], *, timeout: int | None = None) -> None:
    print("+", " ".join(command), flush=True)
    _ = subprocess.run(command, check=True, timeout=timeout)


def _capture(command: list[str]) -> str:
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _android_fixture_service_uuid() -> str:
    try:
        document = json.loads(HISTORICAL_NRF_PROFILE_PATH.read_text(encoding="utf-8"))
        service_uuids = document["advertising"]["service_uuids"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"cannot read historical Peripheral service UUID from {HISTORICAL_NRF_PROFILE_PATH}"
        ) from error
    if not isinstance(service_uuids, list) or len(service_uuids) != 1:
        raise RuntimeError(
            "Android Peripheral profile must advertise exactly one service UUID"
        )
    service_uuid = service_uuids[0]
    if not isinstance(service_uuid, str) or not service_uuid:
        raise RuntimeError("Android Peripheral service UUID must be a non-empty string")
    return service_uuid


def _bundle_is_current(codesign: str, source_hash: str) -> bool:
    if not BUNDLE_EXECUTABLE.is_file() or not BUNDLE_SOURCE_HASH.is_file():
        return False
    if BUNDLE_SOURCE_HASH.read_text(encoding="ascii").strip() != source_hash:
        return False
    result = subprocess.run(
        [codesign, "--verify", "--strict", str(BUNDLE_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _stage_source_package() -> None:
    shutil.rmtree(PACKAGE_ROOT, ignore_errors=True)
    apple_destination = PACKAGE_ROOT / "Sources" / "BleHubApple"
    probe_destination = PACKAGE_ROOT / "Sources" / "BleHubAppleMacOSProbe"
    _ = shutil.copytree(APPLE_SOURCE_ROOT, apple_destination)
    _ = shutil.copytree(
        HIL_ROOT / "Sources" / "BleHubAppleMacOSProbe", probe_destination
    )
    _ = shutil.copy2(
        HIL_SUPPORT_ROOT / "SwiftProbeHarness.swift",
        probe_destination / "SwiftProbeHarness.swift",
    )
    _ = shutil.copy2(
        HIL_SUPPORT_ROOT / "RfFixtureConfiguration.swift",
        probe_destination / "RfFixtureConfiguration.swift",
    )
    production_package_path = json.dumps(str(PRODUCTION_PACKAGE_ROOT))
    manifest = f"""// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "BleHubAppleMacOSProbe",
    platforms: [.macOS(.v13)],
    dependencies: [
        .package(name: "BleHubUniFfi", path: {production_package_path}),
    ],
    targets: [
        .target(
            name: "BleHubApple",
            dependencies: [
                .product(name: "BleHubUniFfi", package: "BleHubUniFfi"),
            ]
        ),
        .executableTarget(
            name: "BleHubAppleMacOSProbe",
            dependencies: [
                "BleHubApple",
                .product(name: "BleHubUniFfi", package: "BleHubUniFfi"),
            ]
        ),
    ]
)
"""
    _ = (PACKAGE_ROOT / "Package.swift").write_text(
        manifest, encoding="utf-8", newline="\n"
    )


def _build_bundle(swift: str, codesign: str, plutil: str) -> None:
    _stage_source_package()
    _run(
        [
            swift,
            "build",
            "--package-path",
            str(PACKAGE_ROOT),
            "--product",
            "BleHubAppleMacOSProbe",
        ]
    )
    binary_root = Path(
        _capture(
            [swift, "build", "--package-path", str(PACKAGE_ROOT), "--show-bin-path"]
        )
    )
    source_executable = binary_root / "BleHubAppleMacOSProbe"
    if not source_executable.is_file():
        raise RuntimeError(
            f"SwiftPM did not produce the runtime probe: {source_executable}"
        )
    source_hash = _file_sha256(source_executable)
    if _bundle_is_current(codesign, source_hash):
        print(f"reusing authorized macOS app bundle: {BUNDLE_ROOT}")
        return

    shutil.rmtree(BUNDLE_ROOT, ignore_errors=True)
    BUNDLE_EXECUTABLE.parent.mkdir(parents=True, exist_ok=True)
    _ = shutil.copy2(source_executable, BUNDLE_EXECUTABLE)
    _ = shutil.copy2(HIL_ROOT / "Info.plist", BUNDLE_ROOT / "Contents" / "Info.plist")

    _run([plutil, "-lint", str(BUNDLE_ROOT / "Contents" / "Info.plist")])
    _run([codesign, "--force", "--sign", "-", str(BUNDLE_ROOT)])
    _ = BUNDLE_SOURCE_HASH.write_text(source_hash + "\n", encoding="ascii")


def _wait_for_marker(
    marker: Path, process: subprocess.Popen[str], *, timeout: int
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.is_file():
            return
        if process.poll() is not None:
            raise RuntimeError(
                f"the CoreBluetooth probe exited before creating {marker.name}"
            )
        time.sleep(0.05)
    raise TimeoutError(
        f"timed out waiting for CoreBluetooth probe marker: {marker.name}"
    )


def _trigger_android_notification(
    value: str, *, expect_success: bool, recipe: str = "android-notify"
) -> None:
    command = [
        _required_executable("pixi"),
        "run",
        "just",
        "peripheral",
        recipe,
        value,
    ]
    print("+", " ".join(command), flush=True)
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if (result.returncode == 0) != expect_success:
        expectation = "succeed" if expect_success else "fail after native disable"
        raise RuntimeError(f"Android notification trigger did not {expectation}")
    if not expect_success and "no connected device subscribed" not in (
        result.stdout + result.stderr
    ):
        raise RuntimeError(
            "Android post-disable trigger failed for an unexpected reason"
        )


def _terminate_probe_app(coordination_root: Path) -> None:
    pid_path = coordination_root / "probe-pid"
    if not pid_path.is_file():
        return
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except ValueError:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_notification_coordination(
    command: list[str],
    coordination_root: Path,
    *,
    recipe: str = "android-notify",
    provider_owned: bool = False,
) -> None:
    print("+", " ".join(command), flush=True)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_marker(coordination_root / "probe-pid", process, timeout=30)
        _wait_for_marker(
            coordination_root / "notification-enabled", process, timeout=240
        )
        if not provider_owned:
            _trigger_android_notification(
                "deadbeef", expect_success=True, recipe=recipe
            )
        _wait_for_marker(
            coordination_root / "notification-disabled", process, timeout=60
        )
        if not provider_owned:
            _trigger_android_notification(
                "cafebabe", expect_success=False, recipe=recipe
            )
        _ = (coordination_root / "post-disable-triggered").write_text(
            "ready\n", encoding="ascii"
        )
        stdout, stderr = process.communicate(timeout=30)
    except BaseException:
        _terminate_probe_app(coordination_root)
        host_command.stop(process)
        raise
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command)


def _run_provider_passive_disconnect_coordination(
    command: list[str], coordination_root: Path
) -> None:
    print("+", " ".join(command), flush=True)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_marker(coordination_root / "probe-pid", process, timeout=30)
        _wait_for_marker(coordination_root / "connected", process, timeout=180)
        _wait_for_marker(
            coordination_root / "passive-disconnected", process, timeout=60
        )
        stdout, stderr = process.communicate(timeout=30)
    except BaseException:
        _terminate_probe_app(coordination_root)
        host_command.stop(process)
        raise
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command)


def _run_android_recipe(recipe: str, *, timeout: int = 60) -> None:
    command = [_required_executable("pixi"), "run", "just", "peripheral", recipe]
    print("+", " ".join(command), flush=True)
    _ = subprocess.run(command, cwd=PROJECT_ROOT, check=True, timeout=timeout)


def _run_passive_disconnect_coordination(
    command: list[str], coordination_root: Path
) -> None:
    print("+", " ".join(command), flush=True)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    radio_recovered = False
    try:
        _wait_for_marker(coordination_root / "probe-pid", process, timeout=30)
        _wait_for_marker(coordination_root / "connected", process, timeout=180)
        _run_android_recipe("android-radio-disable")
        _ = (coordination_root / "fault-triggered").write_text(
            "ready\n", encoding="ascii"
        )
        _wait_for_marker(
            coordination_root / "passive-disconnected", process, timeout=60
        )
        _run_android_recipe("android-radio-enable")
        _run_android_recipe("android-resume")
        radio_recovered = True
        stdout, stderr = process.communicate(timeout=30)
    except BaseException:
        _terminate_probe_app(coordination_root)
        host_command.stop(process)
        if not radio_recovered:
            try:
                _run_android_recipe("android-radio-enable")
                _run_android_recipe("android-resume")
            except (
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as recovery_error:
                print(
                    f"android_fixture_recovery_failed={recovery_error}",
                    file=sys.stderr,
                )
        raise
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command)


def _launch(
    open_executable: str,
    command_name: str,
    fixture: RfFixtureConfig | None = None,
    coordination_path: Path | None = None,
) -> None:
    # Keep this boundary closed for direct callers as well as CLI/Just entry points.
    if fixture is not None or command_name != "authorize":
        _require_macos_rf_session()
    if fixture is not None and command_name not in set(EXTERNAL_RF_COMMANDS.values()):
        raise RuntimeError(
            "external fixture scenario must not invoke Android coordination"
        )
    run_id = uuid.uuid4().hex
    result_name = (
        command_name.removesuffix("-android") + "-external" if fixture else command_name
    )
    result_path = WORK_ROOT / f"{result_name}-{run_id}-result.txt"
    print(f"apple_macos_result_path={result_path}", flush=True)
    result_path.unlink(missing_ok=True)
    command = [
        open_executable,
        "-n",
        "-W",
        str(BUNDLE_ROOT),
        "--args",
        "--wait-for-authorization",
    ]
    service_uuid = (
        fixture.serviceUuid
        if fixture
        else (
            _android_fixture_service_uuid()
            if command_name.endswith("-android")
            else None
        )
    )
    if fixture:
        command.extend(fixture.app_arguments())
    if service_uuid is not None:
        command.extend(["--scan-service-uuid", service_uuid])
        if os.environ.get("BLEHUB_APPLE_RF_DUMP_ADVERTISEMENTS") == "1":
            command.append("--rf-dump-advertisements")
    if command_name in CONNECTION_COMMANDS:
        command.append("--connect-after-scan")
    if command_name == "rssi-android":
        command.append("--run-rssi")
    if command_name in PASSIVE_DISCONNECT_COMMANDS:
        command.append("--run-passive-disconnect")
    if command_name in STRICT_INDICATION_COMMANDS:
        command.append("--strict-indication")
    if command_name in GATT_COMMANDS:
        command.append("--discover-gatt-after-connect")
    if command_name in IO_COMMANDS:
        command.append("--run-gatt-io")
    coordination_root: Path | None = None
    if (
        coordination_path is not None
        or command_name in NOTIFICATION_COMMANDS
        or command_name in STRICT_INDICATION_COMMANDS
        or command_name in PASSIVE_DISCONNECT_COMMANDS
    ):
        coordination_root = (
            coordination_path or WORK_ROOT / f"{command_name}-{run_id}-coordination"
        )
        shutil.rmtree(coordination_root, ignore_errors=True)
        coordination_root.mkdir(parents=True)
    if (
        command_name in NOTIFICATION_COMMANDS
        or command_name in STRICT_INDICATION_COMMANDS
        or command_name in PASSIVE_DISCONNECT_COMMANDS
    ):
        assert coordination_root is not None
        if (
            command_name in NOTIFICATION_COMMANDS
            or command_name in STRICT_INDICATION_COMMANDS
        ):
            command.append("--run-notifications")
        command.extend(["--fixture-coordination-path", str(coordination_root)])
    command.extend(["--result-path", str(result_path)])
    if command_name in NOTIFICATION_COMMANDS:
        assert coordination_root is not None
        _run_notification_coordination(
            command, coordination_root, provider_owned=fixture is not None
        )
    elif command_name in STRICT_INDICATION_COMMANDS:
        _run_notification_coordination(
            command, coordination_root, recipe="android-indicate"
        )
    elif command_name in PASSIVE_DISCONNECT_COMMANDS:
        assert coordination_root is not None
        if fixture is not None:
            _run_provider_passive_disconnect_coordination(command, coordination_root)
        else:
            _run_passive_disconnect_coordination(command, coordination_root)
    else:
        _run(command, timeout=180)
    if not result_path.is_file():
        raise RuntimeError("the launched CoreBluetooth probe produced no result file")
    result = result_path.read_text(encoding="utf-8")
    print(result, end="")
    if (
        command_name in NOTIFICATION_COMMANDS
        or command_name in STRICT_INDICATION_COMMANDS
    ):
        expected = "apple_macos_corebluetooth_notification=passed"
    elif command_name in PASSIVE_DISCONNECT_COMMANDS:
        expected = "apple_macos_corebluetooth_passive_disconnect=passed"
    elif command_name in IO_COMMANDS:
        expected = "apple_macos_corebluetooth_gatt_io=passed"
    elif command_name in GATT_COMMANDS:
        expected = "apple_macos_corebluetooth_gatt=passed"
    elif command_name in CONNECTION_COMMANDS:
        expected = (
            "apple_macos_corebluetooth_rssi=passed"
            if command_name == "rssi-android"
            else "apple_macos_corebluetooth_connection=passed"
        )
    elif service_uuid is not None:
        expected = "apple_macos_corebluetooth_scan=passed"
    else:
        expected = "apple_macos_corebluetooth_authorization=passed"
    required = [expected, *(fixture.evidence() if fixture else [])]
    if not set(required).issubset(result.splitlines()):
        raise RuntimeError(
            "the macOS CoreBluetooth probe did not pass the requested fixture contract"
        )


def _run_rf_scenario(open_executable: str, scenario: str) -> None:
    if scenario == "all":
        commands = [RF_SCENARIO_COMMANDS[name] for name in RF_ALL_SCENARIOS]
    else:
        try:
            commands = [RF_SCENARIO_COMMANDS[scenario]]
        except KeyError as error:
            allowed = ", ".join(("all", *RF_ALL_SCENARIOS))
            raise RuntimeError(
                f"unknown macOS Android RF scenario {scenario!r}; expected {allowed}"
            ) from error
    for command_name in commands:
        print(f"apple_macos_rf_scenario={command_name}", flush=True)
        _launch(open_executable, command_name)


def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("the Apple macOS runtime probe requires macOS")

    parser = argparse.ArgumentParser(description="Run the bundled Apple macOS probe")
    _ = parser.add_argument(
        "command",
        choices=(
            "runtime",
            "authorize",
            "rf-android",
            "rf-external",
            "build",
        ),
    )
    _ = parser.add_argument("scenario", nargs="?", default="all")
    add_profile_arguments(parser)
    _ = parser.add_argument(
        "--coordination-path",
        type=Path,
        help="provider-owned coordination directory for external RF scenarios",
    )
    _ = parser.add_argument(
        "--fixture-request",
        type=Path,
        help="immutable v2 session request; RF remains blocked until the live receipt/owner bridge is implemented",
    )
    args = parser.parse_args()
    if args.command in {"rf-external", "rf-android"}:
        try:
            _require_macos_rf_session()
        except RuntimeError as error:
            parser.error(str(error))
    if args.fixture_request is not None:
        parser.error(
            "--fixture-request is an RF input, not a build/runtime authorization"
        )
    fixture = None
    if args.command == "rf-external":
        if args.scenario not in EXTERNAL_MACOS_SCENARIOS:
            parser.error(
                f"external RF scenario must be one of {EXTERNAL_MACOS_SCENARIOS}; no provider control is available"
            )
        try:
            fixture = fixture_from_arguments(args)
        except ValueError as error:
            parser.error(str(error))
    elif (
        args.profile is not None
        or args.write_with_response != "102030"
        or args.write_without_response != "abcd"
    ):
        parser.error("profile and payload overrides require rf-external")
    command_name = getattr(args, "command", None)
    if not isinstance(command_name, str):
        raise TypeError("missing macOS probe command")

    swift = _required_executable("swift")
    codesign = _required_executable("codesign")
    plutil = _required_executable("plutil")
    _build_bundle(swift, codesign, plutil)
    if command_name == "build":
        print(f"apple_macos_probe_build=passed: {BUNDLE_ROOT}")
    elif command_name == "rf-external":
        _launch(
            _required_executable("open"),
            EXTERNAL_RF_COMMANDS[args.scenario],
            fixture,
            args.coordination_path,
        )
    elif command_name == "runtime":
        _run([str(BUNDLE_EXECUTABLE)], timeout=45)
    elif command_name == "authorize":
        _launch(_required_executable("open"), "authorize")
    else:
        _run_rf_scenario(_required_executable("open"), args.scenario)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
