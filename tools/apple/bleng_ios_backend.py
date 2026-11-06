"""iOS BLeng + production CoreBluetooth initialization/RF HIL."""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from tests.peripherals.nrf52840 import profile as nrf_profile
from tests.peripherals.nrf52840.fixture_session import prepare_session
from tools.apple import consumer_bleng_ios as consumer
from tools.apple import consumer_ios, rf_fixture, test_ios_runtime, xcodegen_toolchain

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "tests/hil/apple/bleng-ios/project.yml"
WORK = ROOT / ".work/apple-bleng-ios-backend"
PROJECT = WORK / "BlengAppleIOSProbe.xcodeproj"
SCHEME = "BlengAppleIOSProbe"
RESULT_SOURCE = "Documents/bleng-corebluetooth-init-result.txt"


def generate() -> None:
    if platform.system() != "Darwin":
        raise RuntimeError("Apple BLeng HIL requires the project Mac")
    consumer.verify_package()
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
    print("apple_bleng_backend_project=ready", flush=True)


def test_recovery_host() -> None:
    """Require a fresh, nonempty, entirely passing standalone macOS XCTest receipt."""
    run_id = str(uuid.uuid4())
    results = WORK / "recovery-host/Results"
    bundle = results / f"{run_id}.xcresult"
    attempt = results / run_id
    if any(path.exists() or path.is_symlink() for path in (bundle, attempt)):
        raise RuntimeError(
            f"refusing existing recovery host artifacts: {bundle}; run directory: {attempt}"
        )
    attempt.mkdir(parents=True)
    log = attempt / "xcodebuild.log"
    summary_path = attempt / "summary.json"
    summary_log = attempt / "summary.log"
    print(f"apple_bleng_recovery_host_run_id={run_id}", flush=True)
    print(f"apple_bleng_recovery_host_result_bundle={bundle}", flush=True)
    print(f"apple_bleng_recovery_host_log={log}", flush=True)
    print(f"apple_bleng_recovery_host_summary={summary_path}", flush=True)
    print(f"apple_bleng_recovery_host_summary_log={summary_log}", flush=True)
    try:
        command = [
            consumer_ios.executable("xcodebuild"),
            "-quiet",
            "-project",
            str(PROJECT),
            "-scheme",
            "BlengAppleRecoveryHostTests",
            "-destination",
            "platform=macOS",
            "-derivedDataPath",
            str(WORK / "recovery-host/DerivedData"),
            "-resultBundlePath",
            str(bundle),
            "CODE_SIGNING_ALLOWED=NO",
            "test",
        ]
        with log.open("x", encoding="utf-8") as output:
            result = subprocess.run(
                command,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=180,
            )
        if result.returncode:
            raise RuntimeError(f"xcodebuild failed (exit {result.returncode})")
        if not bundle.is_dir() or bundle.is_symlink():
            raise RuntimeError(f"missing fresh recovery host result bundle: {bundle}")
        with summary_path.open("xb") as output, summary_log.open("xb") as diagnostics:
            result = subprocess.run(
                [
                    consumer_ios.executable("xcrun"),
                    "xcresulttool",
                    "get",
                    "test-results",
                    "summary",
                    "--path",
                    str(bundle),
                    "--compact",
                ],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=diagnostics,
                check=False,
                timeout=30,
            )
        if result.returncode:
            raise RuntimeError(
                f"xcresult summary export failed (exit {result.returncode})"
            )
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid recovery host summary JSON: {error}") from error
        counts = (
            "totalTestCount",
            "passedTests",
            "failedTests",
            "skippedTests",
            "expectedFailures",
        )
        if not (
            isinstance(summary, dict)
            and summary.get("result") == "Passed"
            and all(type(summary.get(key)) is int for key in counts)
            and summary["totalTestCount"] > 0
            and summary["passedTests"] == summary["totalTestCount"]
            and summary["failedTests"]
            == summary["skippedTests"]
            == summary["expectedFailures"]
            == 0
            and summary.get("testFailures") == []
        ):
            raise RuntimeError(
                "recovery host summary does not prove nonempty, all-passed execution"
            )
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(
            f"recovery host tests failed: {error}; result bundle: {bundle}; "
            f"full log: {log}; summary: {summary_path}; summary log: {summary_log}"
        ) from error
    print(
        f"apple_bleng_recovery_host_test_count={summary['totalTestCount']}", flush=True
    )
    print("apple_bleng_recovery_host=passed", flush=True)


def build(destination: str, variant: str, *, team: str | None = None) -> Path:
    print(f"apple_bleng_backend_build={variant}", flush=True)
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
                f"PRODUCT_BUNDLE_IDENTIFIER=dev.blehub.{team.lower()}.bleng-apple-init",
            ]
        )
    command.append("build")
    log = WORK / f"{variant}-xcodebuild.log"
    if team is None:
        consumer_ios.run(command, timeout=600, log=log)
    else:
        consumer.run_signed_build(command, log)
    configuration = (
        "Debug-iphonesimulator" if variant == "ios-simulator" else "Debug-iphoneos"
    )
    app = derived / "Build/Products" / configuration / f"{SCHEME}.app"
    executable = app / SCHEME
    if not executable.is_file():
        raise RuntimeError(f"missing BLeng Apple HIL executable: {executable}")
    nm = consumer_ios.executable("nm")
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
    consumer.validate_linked_images(images)
    if team is not None and not (app / "embedded.mobileprovision").is_file():
        raise RuntimeError(f"Apple HIL app lacks provisioning: {app}")
    print(f"apple_bleng_backend_{variant}_image=passed", flush=True)
    return app


def build_ui_tests(team: str | None = None) -> None:
    """Compile the existing HIL App/UI test target, optionally signed for a device."""
    variant = "ios-ui-tests-signed" if team is not None else "ios-ui-tests"
    derived = WORK / variant / "DerivedData"
    # This DerivedData tree is owned by this build variant. Older preflight
    # manifests must not make the post-build artifact identity ambiguous.
    for stale in (derived / "Build/Products").glob("*.xctestrun"):
        stale.unlink()
    command = [
        consumer_ios.executable("xcodebuild"),
        "-quiet",
        "-project",
        str(PROJECT),
        "-scheme",
        SCHEME,
        "-destination",
        "generic/platform=iOS",
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
                f"PRODUCT_BUNDLE_IDENTIFIER=dev.blehub.{team.lower()}.bleng-apple-init",
            ]
        )
    command.append("build-for-testing")
    consumer_ios.run(command, timeout=600, log=WORK / "ios-ui-tests-xcodebuild.log")
    xctestruns = sorted((derived / "Build/Products").glob("*.xctestrun"))
    if len(xctestruns) != 1:
        raise RuntimeError(f"expected one UI test bundle manifest, found {xctestruns}")
    print(
        f"apple_bleng_backend_system_loss_ui_build=passed xctestrun={xctestruns[0]}",
        flush=True,
    )


def classify_result(contents: str, run_id: str) -> dict[str, str] | None:
    lines = contents.splitlines()
    if f"apple_bleng_corebluetooth_run_id={run_id}" not in lines:
        return None
    if "apple_bleng_corebluetooth_initialize=failed" in lines:
        raise RuntimeError(
            f"BLeng CoreBluetooth app reported failure: {contents.strip()}"
        )
    if "apple_bleng_corebluetooth_initialize=passed" not in lines:
        raise RuntimeError("BLeng CoreBluetooth app result lacks terminal status")
    values = dict(line.split("=", 1) for line in lines if "=" in line)
    for name in ("adapter_state", "scan_authorization", "connection_authorization"):
        if not values.get(name):
            raise RuntimeError(f"BLeng CoreBluetooth result missing {name}")
    return values


def classify_scan_result(
    contents: str, run_id: str, service_uuid: str
) -> dict[str, str] | None:
    lines = contents.splitlines()
    if f"apple_bleng_corebluetooth_run_id={run_id}" not in lines:
        return None
    if "apple_bleng_corebluetooth_scan=failed" in lines:
        raise RuntimeError(f"BLeng CoreBluetooth RF scan failed: {contents.strip()}")
    if "apple_bleng_corebluetooth_scan=passed" not in lines:
        raise RuntimeError("BLeng CoreBluetooth RF scan result lacks terminal status")
    values = dict(line.split("=", 1) for line in lines if "=" in line)
    if values.get("service_uuid") != service_uuid:
        raise RuntimeError("BLeng RF scan result has the wrong fixture service UUID")
    if values.get("adapter_state", "").lower() != "poweredon":
        raise RuntimeError("BLeng RF scan result lacks a powered-on adapter")
    if values.get("scan_authorization", "").lower() != "authorized":
        raise RuntimeError("BLeng RF scan result lacks scan authorization")
    for name in ("scan_session_id", "peripheral_id"):
        if name not in values or not values[name].isdigit():
            raise RuntimeError(f"BLeng RF scan result lacks {name}")
    return values


def classify_central_recovery_spike_result(
    contents: str, run_id: str
) -> dict[str, str] | None:
    """Validate the diagnostic-only Central recreation spike result."""
    lines = contents.splitlines()
    if f"apple_bleng_corebluetooth_run_id={run_id}" not in lines:
        return None
    if "apple_bleng_corebluetooth_central_recovery_spike=failed" in lines:
        raise RuntimeError(f"BLeng Central recovery spike failed: {contents.strip()}")
    if "apple_bleng_corebluetooth_central_recovery_spike=attempted" not in lines:
        raise RuntimeError("BLeng Central recovery spike lacks terminal status")
    entries = [line.split("=", 1) for line in lines if "=" in line]
    if len({entry[0] for entry in entries}) != len(entries):
        raise RuntimeError(
            "BLeng Central recovery spike contains duplicate evidence keys"
        )
    values = dict(entries)
    if values.get("production_gate_claim") != "none":
        raise RuntimeError("Central recovery spike must not claim a production gate")
    outcome = values.get("central_recovery_spike_outcome")
    if outcome not in ("advertisement-observed", "platform-timeout", "refused"):
        raise RuntimeError("Central recovery spike has an invalid outcome")
    for name in ("first_scan_session_id", "first_matching_advertisements"):
        if re.fullmatch(r"[0-9]+", values.get(name, "")) is None:
            raise RuntimeError(f"Central recovery spike lacks numeric {name}")
    if int(values["first_matching_advertisements"]) < 1:
        raise RuntimeError(
            "Central recovery spike lacks first-scan advertisement evidence"
        )
    second_session = values.get("second_scan_session_id", "")
    second_count = values.get("second_matching_advertisements", "")
    if (
        second_session != "not-started"
        and re.fullmatch(r"[0-9]+", second_session) is None
    ):
        raise RuntimeError("Central recovery spike has an invalid second scan session")
    if re.fullmatch(r"[0-9]+", second_count) is None:
        raise RuntimeError("Central recovery spike lacks numeric second scan count")
    if outcome == "advertisement-observed" and (
        second_session == "not-started" or int(second_count) < 1
    ):
        raise RuntimeError(
            "Central recovery spike claims observation without second-scan evidence"
        )
    return values


def classify_fresh_owner_lifecycle_result(
    contents: str, run_id: str
) -> dict[str, str] | None:
    """Validate the no-RF old-owner retirement/fresh-owner diagnostic."""
    lines = contents.splitlines()
    if f"apple_bleng_corebluetooth_run_id={run_id}" not in lines:
        return None
    if "apple_bleng_fresh_owner_lifecycle=failed" in lines:
        raise RuntimeError(f"BLeng fresh-owner lifecycle failed: {contents.strip()}")
    if "apple_bleng_fresh_owner_lifecycle=attempted" not in lines:
        raise RuntimeError("fresh-owner lifecycle lacks terminal status")
    entries = [line.split("=", 1) for line in lines if "=" in line]
    if len({entry[0] for entry in entries}) != len(entries):
        raise RuntimeError("fresh-owner lifecycle contains duplicate evidence keys")
    values = dict(entries)
    if values.get("production_gate_claim") != "none":
        raise RuntimeError("fresh-owner lifecycle must not claim a production gate")
    if values.get("fresh_owner_lifecycle_outcome") != "fresh-owner-created-and-retired":
        raise RuntimeError("fresh-owner lifecycle has an invalid outcome")
    for name in ("old_owner_id", "new_owner_id", "old_backend_id", "new_backend_id"):
        try:
            uuid.UUID(values.get(name, ""))
        except (ValueError, AttributeError):
            raise RuntimeError(f"fresh-owner lifecycle lacks valid {name}") from None
    if values["old_owner_id"] == values["new_owner_id"]:
        raise RuntimeError("fresh-owner lifecycle reused owner identity")
    if values["old_backend_id"] == values["new_backend_id"]:
        raise RuntimeError("fresh-owner lifecycle reused backend identity")
    return values


def classify_connection_result(
    contents: str, run_id: str, service_uuid: str
) -> dict[str, str] | None:
    lines = contents.splitlines()
    if f"apple_bleng_corebluetooth_run_id={run_id}" not in lines:
        return None
    if "apple_bleng_corebluetooth_connection=failed" in lines:
        raise RuntimeError(
            f"BLeng CoreBluetooth RF connection failed: {contents.strip()}"
        )
    if "apple_bleng_corebluetooth_connection=passed" not in lines:
        raise RuntimeError(
            "BLeng CoreBluetooth RF connection result lacks terminal status"
        )
    values = classify_scan_result(contents, run_id, service_uuid)
    assert values is not None
    if not values.get("connection_id", "").isdigit():
        raise RuntimeError("BLeng RF connection result lacks connection_id")
    if values.get("connection_state_sequence") != "connecting,connected,disconnected":
        raise RuntimeError(
            "BLeng RF connection result lacks ordered lifecycle evidence"
        )
    return values


def classify_gatt_result(
    contents: str, run_id: str, service_uuid: str, *, indication_only: bool
) -> dict[str, str] | None:
    lines = contents.splitlines()
    if f"apple_bleng_corebluetooth_run_id={run_id}" not in lines:
        return None
    if "apple_bleng_corebluetooth_gatt=failed" in lines:
        raise RuntimeError(f"BLeng CoreBluetooth RF GATT failed: {contents.strip()}")
    if "apple_bleng_corebluetooth_gatt=passed" not in lines:
        raise RuntimeError("BLeng CoreBluetooth RF GATT result lacks terminal status")
    values = classify_connection_result(contents, run_id, service_uuid)
    assert values is not None
    if not values.get("gatt_session_id", "").isdigit():
        raise RuntimeError("BLeng RF GATT result lacks gatt_session_id")
    expected = 5 if indication_only else 4
    if values.get("gatt_characteristic_count") != str(expected):
        raise RuntimeError("BLeng RF GATT result lacks fixture characteristic evidence")
    if values.get("gatt_cccd_count") != str(2 if indication_only else 1):
        raise RuntimeError("BLeng RF GATT result lacks fixture CCCD evidence")
    return values


def classify_io_result(
    contents: str, run_id: str, fixture: rf_fixture.RfFixtureConfig
) -> dict[str, str] | None:
    lines = contents.splitlines()
    if f"apple_bleng_corebluetooth_run_id={run_id}" not in lines:
        return None
    if "apple_bleng_corebluetooth_io=failed" in lines:
        raise RuntimeError(f"BLeng CoreBluetooth RF I/O failed: {contents.strip()}")
    if "apple_bleng_corebluetooth_io=passed" not in lines:
        raise RuntimeError("BLeng CoreBluetooth RF I/O result lacks terminal status")
    values = classify_gatt_result(
        contents,
        run_id,
        fixture.serviceUuid,
        indication_only=fixture.hasIndicationOnly,
    )
    assert values is not None
    expected = {
        "initial_read_hex": fixture.initialValueHex,
        "with_response_readback_hex": fixture.writeWithResponseHex,
        "without_response_delayed_readback_hex": fixture.writeWithoutResponseHex,
    }
    for key, value in expected.items():
        if values.get(key) != value:
            raise RuntimeError(f"BLeng RF I/O result lacks exact {key} evidence")
    return values


def classify_rssi_result(
    contents: str, run_id: str, service_uuid: str
) -> dict[str, str] | None:
    lines = contents.splitlines()
    if f"apple_bleng_corebluetooth_run_id={run_id}" not in lines:
        return None
    if "apple_bleng_corebluetooth_rssi=failed" in lines:
        raise RuntimeError(f"BLeng CoreBluetooth RF RSSI failed: {contents.strip()}")
    if "apple_bleng_corebluetooth_rssi=passed" not in lines:
        raise RuntimeError("BLeng CoreBluetooth RF RSSI result lacks terminal status")
    values = classify_connection_result(contents, run_id, service_uuid)
    assert values is not None
    if re.fullmatch(r"-?[0-9]+", values.get("rssi_dbm", "")) is None:
        raise RuntimeError("BLeng RF RSSI result lacks integer RSSI evidence")
    if not -32768 <= int(values["rssi_dbm"]) <= 32767:
        raise RuntimeError("BLeng RF RSSI result exceeds the contract Int16 range")
    return values


def classify_subscription_result(
    contents: str,
    run_id: str,
    fixture: rf_fixture.RfFixtureConfig,
    *,
    mode: str,
) -> dict[str, str] | None:
    if mode not in ("notification", "indication"):
        raise ValueError(f"invalid subscription RF mode: {mode}")
    lines = contents.splitlines()
    if f"apple_bleng_corebluetooth_run_id={run_id}" not in lines:
        return None
    if f"apple_bleng_corebluetooth_{mode}=failed" in lines:
        raise RuntimeError(f"BLeng CoreBluetooth RF {mode} failed: {contents.strip()}")
    if f"apple_bleng_corebluetooth_{mode}=passed" not in lines:
        raise RuntimeError(
            f"BLeng CoreBluetooth RF {mode} result lacks terminal status"
        )
    values = classify_gatt_result(
        contents,
        run_id,
        fixture.serviceUuid,
        indication_only=fixture.hasIndicationOnly,
    )
    assert values is not None
    if values.get(f"{mode}_value_hex") != fixture.notificationValueHex:
        raise RuntimeError(f"BLeng RF {mode} result lacks the exact fixture value")
    if values.get("actual_mode") != mode:
        raise RuntimeError(f"BLeng RF {mode} result did not use {mode} mode")
    if re.fullmatch(r"[0-9]+", values.get("subscription_id", "")) is None:
        raise RuntimeError(f"BLeng RF {mode} result lacks subscription_id")
    for name in (f"{mode}_enable_disable_order", f"{mode}_post_disable_silence"):
        if values.get(name) != "passed":
            raise RuntimeError(f"BLeng RF {mode} result lacks {name} evidence")
    if mode == "indication":
        if not fixture.hasIndicationOnly or fixture.notificationValueHex != "ef":
            raise RuntimeError(
                "BLeng RF indication requires the indication-only EF profile"
            )
        for name in ("strict_dual_indication_rejected", "strict_dual_no_active_route"):
            if values.get(name) != "passed":
                raise RuntimeError(f"BLeng RF indication result lacks {name} evidence")
    return values


def classify_notification_result(
    contents: str, run_id: str, fixture: rf_fixture.RfFixtureConfig
) -> dict[str, str] | None:
    return classify_subscription_result(contents, run_id, fixture, mode="notification")


def classify_indication_result(
    contents: str, run_id: str, fixture: rf_fixture.RfFixtureConfig
) -> dict[str, str] | None:
    return classify_subscription_result(contents, run_id, fixture, mode="indication")


def classify_recovery_result(
    contents: str,
    run_id: str,
    fixture: rf_fixture.RfFixtureConfig,
    *,
    mode: str = "recovery",
) -> dict[str, str] | None:
    """Require per-cycle native event/handle evidence, not a UI success string."""
    if mode not in ("passive-disconnect", "recovery"):
        raise ValueError(f"invalid recovery RF mode: {mode}")
    lines = contents.splitlines()
    if f"apple_bleng_corebluetooth_run_id={run_id}" not in lines:
        return None
    if f"apple_bleng_corebluetooth_{mode}=failed" in lines:
        raise RuntimeError(f"BLeng CoreBluetooth RF {mode} failed: {contents.strip()}")
    if f"apple_bleng_corebluetooth_{mode}=passed" not in lines:
        raise RuntimeError(f"BLeng RF {mode} result lacks terminal status")
    entries = [line.split("=", 1) for line in lines if "=" in line]
    if len({entry[0] for entry in entries}) != len(entries):
        raise RuntimeError("BLeng RF recovery result contains duplicate evidence keys")
    values = classify_scan_result(contents, run_id, fixture.serviceUuid)
    assert values is not None
    cycles = 3 if mode == "recovery" else 1
    if (
        fixture.notificationValueHex != "ef"
        or fixture.initialValueHex != "00"
        or values.get("cycles") != str(cycles)
        or values.get("owner_count") != "1"
        or values.get("connection_authorization", "").lower() != "authorized"
        or values.get("final_disconnect") != "passed"
    ):
        raise RuntimeError(
            "BLeng RF recovery lacks fixed cycles/single-owner/fixture/cleanup evidence"
        )
    observed_cycles = {
        int(match.group(1))
        for key in values
        if (match := re.fullmatch(r"cycle_([0-9]+)_status", key))
    }
    if observed_cycles != set(range(1, cycles + 1)):
        raise RuntimeError("BLeng RF recovery has missing or foreign cycles")
    ids: dict[str, set[int]] = {
        "connection_id": set(),
        "gatt_session_id": set(),
        "subscription_id": set(),
    }
    previous_recovered: dict[str, int] = {}
    previous_disconnect_index = -1
    for cycle in range(1, cycles + 1):
        prefix = f"cycle_{cycle}_"
        passive_result = values.get(prefix + "passive_result")
        category_key = prefix + "passive_error_category"
        if passive_result == "connection-error":
            if values.get(category_key) != "connection":
                raise RuntimeError(
                    f"BLeng RF recovery lacks {category_key}=connection evidence"
                )
        elif passive_result == "disconnected":
            if category_key in values:
                raise RuntimeError(
                    "BLeng RF recovery relabeled an error as disconnected"
                )
        else:
            raise RuntimeError(
                f"BLeng RF recovery has missing or unsupported {prefix}passive_result"
            )
        required = {
            "status": "passed",
            "actual_mode": "notification",
            "notification_value_hex": "ef",
            "passive_command": "absent",
            "active_subscription_at_disconnect": "passed",
            "connection_state_sequence": f"connecting,connected,{passive_result}",
            "stale_connection": "ConnectionNotActive",
            "stale_gatt": "ConnectionNotActive",
            "stale_subscription": "ConnectionNotActive",
            "stale_gatt_on_reconnect": "GattNotReady",
            "stale_subscription_on_reconnect": "SubscriptionNotActive",
            "post_disconnect_silence": "passed",
            "initial_read_hex": "00",
            "recovered_read_hex": "00",
            "gatt_characteristic_count": str(5 if fixture.hasIndicationOnly else 4),
            "gatt_cccd_count": str(2 if fixture.hasIndicationOnly else 1),
        }
        for name, expected in required.items():
            if values.get(prefix + name) != expected:
                raise RuntimeError(f"BLeng RF recovery lacks {prefix + name} evidence")
        numeric: dict[str, int] = {}
        for name in (
            "connection_id",
            "gatt_session_id",
            "subscription_id",
            "characteristic_id",
            "recovered_connection_id",
            "recovered_gatt_session_id",
            "enable_index",
            "enable_terminal_index",
            "value_index",
            "disconnect_index",
        ):
            raw = values.get(prefix + name, "")
            if re.fullmatch(r"[0-9]+", raw) is None:
                raise RuntimeError(f"BLeng RF recovery lacks numeric {prefix + name}")
            numeric[name] = int(raw)
        if not (
            previous_disconnect_index
            < numeric["enable_index"]
            < numeric["enable_terminal_index"]
            < numeric["value_index"]
            < numeric["disconnect_index"]
        ):
            raise RuntimeError("BLeng RF recovery lacks enable/value/passive ordering")
        previous_disconnect_index = numeric["disconnect_index"]
        for name, seen in ids.items():
            current = numeric[name]
            if current in seen:
                raise RuntimeError(f"BLeng RF recovery reused {name}")
            if (
                cycle > 1
                and name != "subscription_id"
                and current != previous_recovered[name]
            ):
                raise RuntimeError(f"BLeng RF recovery did not retain recovered {name}")
            seen.add(current)
            if name != "subscription_id":
                recovered = numeric["recovered_" + name]
                if recovered in seen:
                    raise RuntimeError(f"BLeng RF recovery reused recovered {name}")
                previous_recovered[name] = recovered
    return values


def _provider_command() -> list[str]:
    return [sys.executable, "-m", "tests.peripherals.nrf52840.provider"]


def _provider_status() -> str:
    result = subprocess.run(
        [*_provider_command(), "status"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(
            f"nRF provider status failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def _provider_pid(status: str) -> str | None:
    match = re.search(r"^nrf52840_fixture_pid=([0-9]+)$", status, re.MULTILINE)
    return None if match is None else match.group(1)


def _provider_pgid(status: str) -> str | None:
    match = re.search(r"^nrf52840_fixture_pgid=([0-9]+)$", status, re.MULTILINE)
    return None if match is None else match.group(1)


def _stop_spawned_provider(pid: str, pgid: str) -> None:
    result = subprocess.run(
        [
            *_provider_command(),
            "stop-owned",
            "--pid",
            pid,
            "--pgid",
            pgid,
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"nRF provider exact-identity cleanup failed (exit {result.returncode}): "
            f"{detail}"
        )


def _start_owned_provider(
    coordination_path: Path,
    *,
    mode: str = "notification",
    port: str | None = None,
    fixture_request: Path | None = None,
) -> tuple[str, str]:
    status = _provider_status()
    if "nrf52840_fixture=running" in status:
        raise RuntimeError("refusing to take over an existing nRF52840 provider")
    result = subprocess.run(
        [
            *_provider_command(),
            "direct-start",
            *(["--port", port] if port is not None else []),
            "--coordination-path",
            str(coordination_path),
            "--coordination-mode",
            mode,
            *(
                ["--fixture-request", str(fixture_request)]
                if fixture_request is not None
                else []
            ),
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    if result.returncode:
        raise RuntimeError(
            f"nRF provider direct-start failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    spawned_pid = _provider_pid(result.stdout)
    spawned_pgid = _provider_pgid(result.stdout)
    if spawned_pid is None or spawned_pgid is None:
        raise RuntimeError(
            "nRF provider direct-start did not publish its spawned fixture identity"
        )
    try:
        status = _provider_status()
    except Exception as error:
        try:
            _stop_spawned_provider(spawned_pid, spawned_pgid)
        except RuntimeError as cleanup_error:
            raise RuntimeError(
                f"nRF provider status failed after startup and exact-identity cleanup "
                f"failed: {cleanup_error}"
            ) from error
        raise
    owned_pid = _provider_pid(status)
    owned_pgid = _provider_pgid(status)
    if owned_pid != spawned_pid or owned_pgid != spawned_pgid:
        try:
            _stop_spawned_provider(spawned_pid, spawned_pgid)
        except RuntimeError as cleanup_error:
            raise RuntimeError(
                f"nRF provider identity changed after startup and exact-identity "
                f"cleanup failed: {cleanup_error}"
            ) from cleanup_error
        raise RuntimeError(
            "nRF provider direct-start did not retain its spawned fixture identity"
        )
    return spawned_pid, spawned_pgid


def _stop_owned_provider(owned_identity: tuple[str, str]) -> None:
    _stop_spawned_provider(*owned_identity)


def _pull_device_file(
    xcrun: str,
    device_id: str,
    bundle: str,
    source: str,
    destination_name: str,
) -> str | None:
    destination = WORK / destination_name
    destination.unlink(missing_ok=True)
    try:
        consumer.device_json(
            xcrun,
            [
                "device",
                "copy",
                "from",
                "--device",
                device_id,
                "--source",
                source,
                "--destination",
                str(destination),
                "--domain-type",
                "appDataContainer",
                "--domain-identifier",
                bundle,
            ],
            f"{destination_name}.json",
            timeout=30,
            work=WORK,
        )
    except RuntimeError:
        return None
    if not destination.is_file():
        return None
    return destination.read_text(encoding="utf-8")


def _wait_for_device_marker(
    xcrun: str,
    device_id: str,
    bundle: str,
    run_id: str,
    marker: str,
    *,
    timeout: int,
    mode: str = "notification",
    progress: Callable[[], None] | None = None,
) -> None:
    marker_source = f"Documents/bleng-rf-{run_id}/{marker}"
    result_source = f"Documents/bleng-rf-{run_id}/result.txt"
    deadline = time.monotonic() + timeout
    next_progress = time.monotonic() + 15
    while time.monotonic() < deadline:
        if progress is not None:
            progress()
        if time.monotonic() >= next_progress:
            print(f"apple_bleng_backend_rf_{mode}=waiting_for_{marker}", flush=True)
            next_progress = time.monotonic() + 15
        if (
            _pull_device_file(
                xcrun,
                device_id,
                bundle,
                marker_source,
                f"{run_id}-{marker.replace('/', '-')}.marker",
            )
            is not None
        ):
            return
        result = _pull_device_file(
            xcrun,
            device_id,
            bundle,
            result_source,
            f"{run_id}-{mode}-result.txt",
        )
        if (
            result is not None
            and f"apple_bleng_corebluetooth_run_id={run_id}" in result.splitlines()
            and f"apple_bleng_corebluetooth_{mode}=failed" in result.splitlines()
        ):
            raise RuntimeError(
                f"BLeng CoreBluetooth RF {mode} failed while waiting for {marker}: "
                f"{result.strip()}"
            )
        time.sleep(1)
    raise TimeoutError(f"timed out waiting for iOS RF marker: {marker}")


def _write_host_marker(coordination_path: Path, marker: str) -> None:
    (coordination_path / marker).write_text("ready\n", encoding="ascii")


def _wait_for_host_marker(
    coordination_path: Path,
    marker: str,
    *,
    timeout: int,
    progress: Callable[[], None] | None = None,
) -> None:
    path = coordination_path / marker
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if progress is not None:
            progress()
        if path.is_file():
            return
        time.sleep(0.2)
    raise TimeoutError(f"timed out waiting for nRF host marker: {path}")


def _copy_marker_to_device(
    xcrun: str, device_id: str, bundle: str, run_id: str, marker: str
) -> None:
    local_marker = WORK / f"{run_id}-{marker.replace('/', '-')}.host-marker"
    local_marker.write_text("ready\n", encoding="ascii")
    consumer.device_json(
        xcrun,
        [
            "device",
            "copy",
            "to",
            "--device",
            device_id,
            "--source",
            str(local_marker),
            "--destination",
            f"Documents/bleng-rf-{run_id}/{marker}",
            "--domain-type",
            "appDataContainer",
            "--domain-identifier",
            bundle,
        ],
        f"{run_id}-{marker.replace('/', '-')}-copy.json",
        timeout=30,
        work=WORK,
    )


def _wait_for_subscription_result(
    xcrun: str,
    device_id: str,
    bundle: str,
    run_id: str,
    fixture: rf_fixture.RfFixtureConfig,
    *,
    mode: str = "notification",
    progress: Callable[[], None] | None = None,
) -> dict[str, str]:
    source = f"Documents/bleng-rf-{run_id}/result.txt"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if progress is not None:
            progress()
        contents = _pull_device_file(
            xcrun,
            device_id,
            bundle,
            source,
            f"{run_id}-{mode}-result.txt",
        )
        if contents is not None:
            classifier = (
                classify_recovery_result
                if mode in ("passive-disconnect", "recovery")
                else classify_subscription_result
            )
            values = classifier(contents, run_id, fixture, mode=mode)
            if values is not None:
                archive = WORK / f"{run_id}-{mode}-result.txt"
                archive.write_text(contents, encoding="utf-8")
                return values
        time.sleep(1)
    raise TimeoutError(f"timed out waiting for the iOS RF {mode} result")


def _subscription_device_run(
    xcrun: str,
    device_id: str,
    bundle: str,
    fixture: rf_fixture.RfFixtureConfig,
    *,
    mode: str = "notification",
) -> None:
    run_id = str(uuid.uuid4())
    if mode not in ("notification", "indication"):
        raise ValueError(f"invalid subscription RF mode: {mode}")
    if mode == "indication" and (
        not fixture.hasIndicationOnly or fixture.notificationValueHex != "ef"
    ):
        raise RuntimeError(
            "BLeng RF indication requires the indication-only EF profile"
        )
    coordination_path = WORK / f"rf-{mode}-{run_id}"
    if coordination_path.exists():
        raise RuntimeError(
            f"refusing to reuse RF coordination directory: {coordination_path}"
        )
    coordination_path.mkdir(parents=True)
    owned_identity: tuple[str, str] | None = None
    try:
        print(f"apple_bleng_backend_rf_{mode}=starting_fixture", flush=True)
        owned_identity = _start_owned_provider(coordination_path, mode=mode)
        print(
            f"apple_bleng_backend_rf_{mode}=fixture_started pid={owned_identity[0]}",
            flush=True,
        )
        launch = [
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
            f"--rf-{mode}",
            "--rf-notification-value-hex",
            fixture.notificationValueHex,
            "--rf-scan-service-uuid",
            fixture.serviceUuid,
        ]
        if fixture.hasIndicationOnly:
            launch.append("--rf-indication-only")
        consumer.device_json(
            xcrun,
            launch,
            "device-launch.json",
            timeout=60,
            work=WORK,
        )
        _wait_for_device_marker(
            xcrun, device_id, bundle, run_id, f"{mode}-enabled", timeout=240, mode=mode
        )
        _write_host_marker(coordination_path, "subscription-enabled")
        _wait_for_host_marker(coordination_path, "value-applied", timeout=60)
        _wait_for_device_marker(
            xcrun, device_id, bundle, run_id, f"{mode}-disabled", timeout=60, mode=mode
        )
        _write_host_marker(coordination_path, "subscription-disabled")
        _wait_for_host_marker(coordination_path, "post-disable-triggered", timeout=60)
        _copy_marker_to_device(
            xcrun, device_id, bundle, run_id, "post-disable-triggered"
        )
        values = _wait_for_subscription_result(
            xcrun, device_id, bundle, run_id, fixture, mode=mode
        )
        print(
            f"apple_bleng_backend_rf_{mode}=passed "
            f"run_id={run_id} profile={fixture.profileId} "
            f"service_uuid={fixture.serviceUuid} subscription_id={values['subscription_id']} "
            f"result={WORK / f'{run_id}-{mode}-result.txt'}",
            flush=True,
        )
    finally:
        if owned_identity is not None:
            _stop_owned_provider(owned_identity)


def _recovery_device_run(
    xcrun: str,
    device_id: str,
    bundle: str,
    fixture: rf_fixture.RfFixtureConfig,
    *,
    mode: str,
) -> None:
    if mode not in ("passive-disconnect", "recovery"):
        raise ValueError(f"invalid recovery RF mode: {mode}")
    if fixture.notificationValueHex != "ef" or fixture.initialValueHex != "00":
        raise ValueError(
            "recovery RF requires the provider's EF notification and 00 read"
        )
    run_id = str(uuid.uuid4())
    coordination_path = WORK / f"rf-{mode}-{run_id}"
    coordination_path.mkdir(parents=True, exist_ok=False)
    owned_identity: tuple[str, str] | None = None
    last_progress = 0.0

    def progress() -> None:
        nonlocal last_progress
        if time.monotonic() - last_progress < 5:
            return
        last_progress = time.monotonic()
        status = _provider_status()
        if (
            _provider_pid(status),
            _provider_pgid(status),
        ) != owned_identity:
            raise RuntimeError("owned nRF recovery provider exited or changed identity")
        contents = _pull_device_file(
            xcrun,
            device_id,
            bundle,
            f"Documents/bleng-rf-{run_id}/result.txt",
            f"{run_id}-{mode}-result.txt",
        )
        if (
            contents is not None
            and f"apple_bleng_corebluetooth_run_id={run_id}" in contents.splitlines()
            and f"apple_bleng_corebluetooth_{mode}=failed" in contents.splitlines()
        ):
            raise RuntimeError(
                f"BLeng RF {mode} failed during coordination: {contents.strip()}"
            )

    def device_marker(marker: str, timeout: int = 60) -> None:
        _wait_for_device_marker(
            xcrun,
            device_id,
            bundle,
            run_id,
            marker,
            timeout=timeout,
            mode=mode,
            progress=progress,
        )

    def host_marker(marker: str) -> None:
        _wait_for_host_marker(coordination_path, marker, timeout=60, progress=progress)
        _copy_marker_to_device(xcrun, device_id, bundle, run_id, marker)

    try:
        provider_mode = "recovery-ios" if mode == "recovery" else mode
        owned_identity = _start_owned_provider(coordination_path, mode=provider_mode)
        launch = [
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
            f"--rf-{mode}",
            "--rf-scan-service-uuid",
            fixture.serviceUuid,
            "--rf-notification-value-hex",
            fixture.notificationValueHex,
            "--rf-initial-hex",
            fixture.initialValueHex,
        ]
        if fixture.hasIndicationOnly:
            launch.append("--rf-indication-only")
        consumer.device_json(xcrun, launch, "device-launch.json", timeout=60, work=WORK)
        if mode == "recovery":
            device_marker("coordination-ready")
        for cycle in range(1, 4 if mode == "recovery" else 2):
            prefix = f"cycle-{cycle}/" if mode == "recovery" else ""
            if mode == "recovery":
                host_marker(prefix + "fixture-ready")
            device_marker(prefix + "subscription-enabled", timeout=90)
            _write_host_marker(coordination_path, prefix + "subscription-enabled")
            _wait_for_host_marker(
                coordination_path,
                prefix + "value-applied",
                timeout=60,
                progress=progress,
            )
            device_marker(prefix + "connected")
            _write_host_marker(coordination_path, prefix + "connected")
            host_marker(prefix + "fault-triggered")
            device_marker(prefix + "passive-disconnected")
            if mode == "recovery":
                # recovery-ios cannot resume advertising until the device ACK is forwarded.
                _write_host_marker(coordination_path, prefix + "passive-disconnected")
            host_marker(prefix + "recovery-ready")
            device_marker(prefix + "cycle-passed", timeout=90)
            if mode == "recovery":
                _write_host_marker(coordination_path, prefix + "cycle-passed")
        values = _wait_for_subscription_result(
            xcrun, device_id, bundle, run_id, fixture, mode=mode, progress=progress
        )
        print(
            f"apple_bleng_backend_rf_{mode}=passed run_id={run_id} "
            f"profile={fixture.profileId} cycles={values['cycles']} "
            f"result={WORK / f'{run_id}-{mode}-result.txt'}",
            flush=True,
        )
    finally:
        if owned_identity is not None:
            _stop_owned_provider(owned_identity)


def _central_recovery_spike_device_run(
    xcrun: str,
    device_id: str,
    bundle: str,
    *,
    contract: Path,
    fixture_profile: Path,
    port: str,
) -> None:
    """Run the non-destructive Central recreation spike in the existing HIL App."""
    run_id = str(uuid.uuid4())
    coordination_path = WORK / f"rf-central-recovery-spike-{run_id}"
    coordination_path.mkdir(parents=True, exist_ok=False)
    fixture_session = coordination_path / "fixture-session"
    profile_data = nrf_profile.load_profile(fixture_profile)
    plan = prepare_session(
        contract,
        fixture_profile,
        fixture_session,
        port=port,
        service_uuid=profile_data.service_uuid,
        session_id=run_id,
    )
    owned_identity: tuple[str, str] | None = None
    try:
        owned_identity = _start_owned_provider(
            coordination_path,
            mode="notification",
            port=port,
            fixture_request=plan.request_path,
        )
        launch = [
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
            "--rf-central-recovery-spike",
            "--rf-scan-service-uuid",
            profile_data.service_uuid,
        ]
        consumer.device_json(
            xcrun,
            launch,
            "central-recovery-spike-device-launch.json",
            timeout=60,
            work=WORK,
        )
        result_file = WORK / f"{run_id}-central-recovery-spike-result.txt"
        diagnostic_file = WORK / f"{run_id}-central-recovery-spike.json"
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = _pull_device_file(
                xcrun,
                device_id,
                bundle,
                f"Documents/bleng-rf-{run_id}/result.txt",
                result_file.name,
            )
            if result is not None and (
                f"apple_bleng_corebluetooth_run_id={run_id}" in result.splitlines()
            ):
                diagnostic = _pull_device_file(
                    xcrun,
                    device_id,
                    bundle,
                    f"Documents/bleng-rf-{run_id}/central-recovery-spike.json",
                    diagnostic_file.name,
                )
                values = classify_central_recovery_spike_result(result, run_id)
                if values is None:
                    raise RuntimeError(
                        "Central recovery spike result lost its run identity"
                    )
                outcome = values["central_recovery_spike_outcome"]
                print(
                    "apple_bleng_backend_rf-central-recovery-spike=completed "
                    f"run_id={run_id} outcome={outcome} "
                    f"result={result_file} diagnostic={diagnostic_file if diagnostic else 'missing'}",
                    flush=True,
                )
                return
            time.sleep(2)
        raise TimeoutError(
            "BLeng Central recovery spike result not observed within 120 seconds; "
            f"see {WORK}"
        )
    finally:
        if owned_identity is not None:
            _stop_owned_provider(owned_identity)


def _fresh_owner_lifecycle_device_run(
    xcrun: str,
    device_id: str,
    bundle: str,
) -> None:
    """Run the no-RF lifecycle probe in the existing HIL App."""
    run_id = str(uuid.uuid4())
    launch = [
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
        "--rf-fresh-owner-lifecycle-spike",
    ]
    consumer.device_json(
        xcrun,
        launch,
        "fresh-owner-lifecycle-device-launch.json",
        timeout=60,
        work=WORK,
    )
    result_file = WORK / f"{run_id}-fresh-owner-lifecycle-result.txt"
    diagnostic_file = WORK / f"{run_id}-fresh-owner-lifecycle.json"
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = _pull_device_file(
            xcrun,
            device_id,
            bundle,
            f"Documents/bleng-rf-{run_id}/result.txt",
            result_file.name,
        )
        if (
            result is not None
            and f"apple_bleng_corebluetooth_run_id={run_id}" in result.splitlines()
        ):
            diagnostic = _pull_device_file(
                xcrun,
                device_id,
                bundle,
                f"Documents/bleng-rf-{run_id}/fresh-owner-lifecycle.json",
                diagnostic_file.name,
            )
            values = classify_fresh_owner_lifecycle_result(result, run_id)
            if values is None:
                raise RuntimeError("fresh-owner lifecycle result lost its run identity")
            print(
                "apple_bleng_backend_fresh_owner_lifecycle=completed "
                f"run_id={run_id} old_owner={values['old_owner_id']} "
                f"new_owner={values['new_owner_id']} "
                f"diagnostic={diagnostic_file if diagnostic else 'missing'}",
                flush=True,
            )
            return
        time.sleep(2)
    raise TimeoutError(
        "BLeng fresh-owner lifecycle result not observed within 120 seconds; "
        f"see {WORK}"
    )


def device_run(
    *,
    rf_scenario: str | None = None,
    profile: Path = test_ios_runtime.NRF_PROFILE_PATH,
    notification_value_hex: str | None = None,
    initial_hex: str | None = None,
    contract: Path | None = None,
    fixture_profile: Path | None = None,
    port: str | None = None,
) -> None:
    fixture = (
        rf_fixture.load_profile(
            profile,
            write_with_response="10" if rf_scenario == "io" else "102030",
            write_without_response="ab" if rf_scenario == "io" else "abcd",
        )
        if rf_scenario is not None
        else None
    )
    if rf_scenario in ("passive-disconnect", "recovery"):
        assert fixture is not None
        # The owned provider has a fixed profile; flags are assertions, not overrides.
        if (
            profile.resolve() != test_ios_runtime.NRF_PROFILE_PATH.resolve()
            or fixture.notificationValueHex != "ef"
            or fixture.initialValueHex != "00"
            or (
                notification_value_hex is not None
                and notification_value_hex.lower() != "ef"
            )
            or (initial_hex is not None and initial_hex.lower() != "00")
        ):
            raise ValueError(
                "recovery requires the managed nRF profile, EF notification and 00 read"
            )
    elif (
        notification_value_hex is not None
        or initial_hex is not None
        or profile != test_ios_runtime.NRF_PROFILE_PATH
    ):
        raise ValueError(
            "profile/value assertion flags are only supported by the recovery gates"
        )
    xcrun = consumer_ios.executable("xcrun")
    device_id, udid, team = consumer.connected_device(xcrun, work=WORK)
    bundle = f"dev.blehub.{team.lower()}.bleng-apple-init"
    app = build(f"id={udid}", "ios-signed-device", team=team)
    print("apple_bleng_backend_device=installing", flush=True)
    consumer.device_json(
        xcrun,
        ["device", "install", "app", "--device", device_id, str(app)],
        "device-install.json",
        timeout=120,
        work=WORK,
    )
    device_id, _, _ = consumer.wait_for_connected_device(xcrun, work=WORK)
    consumer.wait_for_unlocked_device(xcrun, device_id, work=WORK)
    if rf_scenario == "fresh-owner-lifecycle":
        _fresh_owner_lifecycle_device_run(xcrun, device_id, bundle)
        return
    if rf_scenario in ("passive-disconnect", "recovery"):
        assert fixture is not None
        _recovery_device_run(xcrun, device_id, bundle, fixture, mode=rf_scenario)
        return
    if rf_scenario == "central-recovery-spike":
        if contract is None or fixture_profile is None or port is None:
            raise ValueError(
                "central recovery spike requires --contract, --fixture-profile and --port"
            )
        _central_recovery_spike_device_run(
            xcrun,
            device_id,
            bundle,
            contract=contract,
            fixture_profile=fixture_profile,
            port=port,
        )
        return
    if rf_scenario in ("notification", "indication"):
        assert fixture is not None
        _subscription_device_run(xcrun, device_id, bundle, fixture, mode=rf_scenario)
        return
    run_id = str(uuid.uuid4())
    print(f"apple_bleng_backend_device=launching run_id={run_id}", flush=True)
    launch = [
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
    ]
    if fixture is not None:
        launch.extend(["--rf-scan-service-uuid", fixture.serviceUuid])
        if rf_scenario == "connection":
            launch.append("--rf-connect")
        elif rf_scenario == "rssi":
            launch.append("--rf-rssi")
        elif rf_scenario in ("gatt", "io"):
            launch.append("--rf-io" if rf_scenario == "io" else "--rf-gatt")
            if fixture.hasIndicationOnly:
                launch.append("--rf-indication-only")
            if rf_scenario == "io":
                launch.extend(
                    [
                        "--rf-initial-hex",
                        fixture.initialValueHex,
                        "--rf-write-response-hex",
                        fixture.writeWithResponseHex,
                        "--rf-write-no-response-hex",
                        fixture.writeWithoutResponseHex,
                    ]
                )
    consumer.device_json(
        xcrun,
        launch,
        "device-launch.json",
        timeout=60,
        work=WORK,
    )
    result_file = WORK / "device-result.txt"
    deadline = time.monotonic() + (90 if fixture is None else 75)
    attempt = 0
    while time.monotonic() < deadline:
        result_file.unlink(missing_ok=True)
        try:
            consumer.device_json(
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
                work=WORK,
            )
        except RuntimeError:
            pass  # The app may not have written its result immediately.
        if result_file.is_file():
            contents = result_file.read_text(encoding="utf-8")
            if fixture is None:
                values = classify_result(contents, run_id)
            elif rf_scenario == "connection":
                values = classify_connection_result(
                    contents, run_id, fixture.serviceUuid
                )
            elif rf_scenario == "gatt":
                values = classify_gatt_result(
                    contents,
                    run_id,
                    fixture.serviceUuid,
                    indication_only=fixture.hasIndicationOnly,
                )
            elif rf_scenario == "io":
                values = classify_io_result(contents, run_id, fixture)
            elif rf_scenario == "rssi":
                values = classify_rssi_result(contents, run_id, fixture.serviceUuid)
            else:
                values = classify_scan_result(contents, run_id, fixture.serviceUuid)
            if values is not None:
                archive = WORK / f"{run_id}-result.txt"
                archive.write_text(contents, encoding="utf-8")
                if fixture is None:
                    print(
                        "apple_bleng_backend_device=passed "
                        f"adapter_state={values['adapter_state']} "
                        f"scan_authorization={values['scan_authorization']}",
                        flush=True,
                    )
                else:
                    print(
                        f"apple_bleng_backend_rf_{rf_scenario}=passed "
                        f"run_id={run_id} profile={fixture.profileId} "
                        f"service_uuid={fixture.serviceUuid} "
                        f"result={archive}",
                        flush=True,
                    )
                return
        attempt += 1
        if attempt % 5 == 0:
            print("apple_bleng_backend_device=awaiting_result", flush=True)
        time.sleep(2)
    raise TimeoutError(
        f"BLeng CoreBluetooth result not observed within {90 if fixture is None else 75} seconds; see {WORK}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "command",
        choices=(
            "build-unsigned",
            "build-ui-tests",
            "test-recovery-host",
            "device",
            "rf-scan",
            "rf-connection",
            "rf-gatt",
            "rf-io",
            "rf-notification",
            "rf-indication",
            "rf-rssi",
            "rf-passive-disconnect",
            "rf-recovery",
            "rf-central-recovery-spike",
            "rf-fresh-owner-lifecycle",
        ),
    )
    parser.add_argument(
        "--profile", type=Path, default=test_ios_runtime.NRF_PROFILE_PATH
    )
    parser.add_argument(
        "--team", help="sign the UI-test candidate for a physical iOS device"
    )
    parser.add_argument(
        "--notification-value-hex", help="recovery assertion: provider value must be EF"
    )
    parser.add_argument(
        "--initial-hex", help="recovery assertion: provider read must be 00"
    )
    parser.add_argument(
        "--contract", type=Path, help="attested nRF v2 fixture contract for the spike"
    )
    parser.add_argument(
        "--fixture-profile",
        type=Path,
        help="attested nRF v2 fixture profile for the spike",
    )
    parser.add_argument("--port", help="explicit nRF fixture serial port for the spike")
    args = parser.parse_args()
    try:
        generate()
        if args.command == "test-recovery-host":
            test_recovery_host()
        elif args.command == "build-ui-tests":
            build_ui_tests(args.team)
        elif args.command in (
            "device",
            "rf-scan",
            "rf-connection",
            "rf-gatt",
            "rf-io",
            "rf-notification",
            "rf-indication",
            "rf-rssi",
            "rf-passive-disconnect",
            "rf-recovery",
            "rf-central-recovery-spike",
            "rf-fresh-owner-lifecycle",
        ):
            device_run(
                rf_scenario=args.command.removeprefix("rf-")
                if args.command.startswith("rf-")
                else None,
                profile=args.profile,
                notification_value_hex=args.notification_value_hex,
                initial_hex=args.initial_hex,
                contract=args.contract,
                fixture_profile=args.fixture_profile,
                port=args.port,
            )
        else:
            build("generic/platform=iOS", "ios-device")
            build("generic/platform=iOS Simulator", "ios-simulator")
    except (
        RuntimeError,
        OSError,
        ValueError,
        TimeoutError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
