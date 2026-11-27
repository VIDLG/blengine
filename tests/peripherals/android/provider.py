from __future__ import annotations

import argparse
import hashlib
import re
import secrets
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tools.android_toolchain import adb_environment, adb_executable, select_test_device

REPO_ROOT = Path(__file__).resolve().parents[3]
APK = REPO_ROOT / "tests/peripherals/android/app/build/outputs/apk/debug/app-debug.apk"
PACKAGE = "dev.blehub.fixture.peripheral"
ACTIVITY_CLASS = f"{PACKAGE}.MainActivity"
ADB_COMPONENT = f"{PACKAGE}/{ACTIVITY_CLASS}"
LOG_TAG = "BleHubPeripheral"
REQUIRED_PERMISSIONS = (
    "android.permission.BLUETOOTH_ADVERTISE",
    "android.permission.BLUETOOTH_CONNECT",
)
INSTALL_TIMEOUT_SECONDS = 30.0
# OEM install protection (for example ColorOS `com.oplus.appdetail`) blocks non-interactive
# `adb install`, so a rooted device falls back to `pm install` through the platform service.
ROOT_INSTALL_TIMEOUT_SECONDS = 120.0
ROOT_INSTALL_REMOTE = "/data/local/tmp/blehub-android-fixture.apk"
INSTALLER_PACKAGE = "com.android.packageinstaller"
INSTALLER_CONFIRM_BUTTON = "com.android.packageinstaller:id/button1"
INSTALLER_CONFIRM_QUESTION = "com.android.packageinstaller:id/install_confirm_question"
INSTALLER_SUCCESS = "com.android.packageinstaller:id/install_success"
INSTALL_UI_DUMP = "/sdcard/blehub-install-ui.xml"
INSTALL_SUCCESS_TIMEOUT_SECONDS = 5.0
SUBSCRIPTION_BURST_TIMEOUT_FLOOR_SECONDS = 180.0
SUBSCRIPTION_BURST_OVERHEAD_SECONDS = 20.0
SUBSCRIPTION_MIN_OPERATIONS_PER_SECOND = {
    "notification": 50.0,
    "indication": 25.0,
}


@dataclass(frozen=True)
class Args:
    command: str
    value: str | None
    burst_id: str | None
    count: int | None
    payload_bytes: int | None


def _device() -> tuple[str, dict[str, str]]:
    environment = adb_environment()
    selected = select_test_device("physical", environment=environment)
    print(f"android_test_device={selected.serial}", flush=True)
    return selected.serial, environment


def _run(
    command: list[str],
    environment: dict[str, str],
    *,
    timeout: float | None = None,
) -> None:
    try:
        result = subprocess.run(
            command,
            env=environment,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise SystemExit(
            f"timed out after {timeout} seconds: {' '.join(command)}"
        ) from error
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def _require_apk() -> None:
    if not APK.is_file():
        raise SystemExit(
            f"missing {APK}; run `pixi run just peripheral android-build` first"
        )


def _adb(
    serial: str,
    environment: dict[str, str],
    *args: str,
    timeout: float | None = None,
) -> None:
    _run([str(adb_executable()), "-s", serial, *args], environment, timeout=timeout)


def _adb_output(
    serial: str,
    environment: dict[str, str],
    *args: str,
    timeout: float | None = None,
) -> str:
    result = subprocess.run(
        [str(adb_executable()), "-s", serial, *args],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.returncode)
    return result.stdout


def _installer_nodes(
    serial: str, environment: dict[str, str]
) -> list[ET.Element] | None:
    try:
        dump = subprocess.run(
            [
                str(adb_executable()),
                "-s",
                serial,
                "shell",
                "uiautomator",
                "dump",
                INSTALL_UI_DUMP,
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
        )
        if dump.returncode != 0:
            return None
        ui = subprocess.run(
            [str(adb_executable()), "-s", serial, "shell", "cat", INSTALL_UI_DUMP],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
        )
        if ui.returncode != 0:
            return None
        return list(ET.fromstring(ui.stdout).iter("node"))
    except (ET.ParseError, OSError, subprocess.TimeoutExpired):
        return None


def _button_center(nodes: list[ET.Element], resource_id: str) -> tuple[int, int] | None:
    for node in nodes:
        if (
            node.get("package") != INSTALLER_PACKAGE
            or node.get("resource-id") != resource_id
            or node.get("class") != "android.widget.Button"
            or node.get("clickable") != "true"
            or node.get("enabled") != "true"
        ):
            continue
        bounds = node.get("bounds", "")
        try:
            left_top, right_bottom = bounds.split("][")
            left, top = (int(value) for value in left_top.removeprefix("[").split(","))
            right, bottom = (
                int(value) for value in right_bottom.removesuffix("]").split(",")
            )
        except (ValueError, IndexError):
            return None
        return ((left + right) // 2, (top + bottom) // 2)
    return None


def _install_prompt_button(
    serial: str, environment: dict[str, str]
) -> tuple[int, int] | None:
    nodes = _installer_nodes(serial, environment)
    if nodes is None:
        return None
    has_target_title = any(
        node.get("package") == INSTALLER_PACKAGE
        and node.get("resource-id") == f"{INSTALLER_PACKAGE}:id/alertTitle"
        and node.get("text") == "BleHub Peripheral"
        for node in nodes
    )
    has_confirmation_question = any(
        node.get("package") == INSTALLER_PACKAGE
        and node.get("resource-id") == INSTALLER_CONFIRM_QUESTION
        for node in nodes
    )
    if not has_target_title or not has_confirmation_question:
        return None
    return _button_center(nodes, INSTALLER_CONFIRM_BUTTON)


def _install_success_button(
    serial: str, environment: dict[str, str]
) -> tuple[int, int] | None:
    nodes = _installer_nodes(serial, environment)
    if nodes is None:
        return None
    has_target_title = any(
        node.get("package") == INSTALLER_PACKAGE
        and node.get("resource-id") == f"{INSTALLER_PACKAGE}:id/alertTitle"
        and node.get("text") == "BleHub Peripheral"
        for node in nodes
    )
    has_success_message = any(
        node.get("package") == INSTALLER_PACKAGE
        and node.get("resource-id") == INSTALLER_SUCCESS
        for node in nodes
    )
    if not has_target_title or not has_success_message:
        return None
    return _button_center(nodes, INSTALLER_CONFIRM_BUTTON)


def _install_apk(serial: str, environment: dict[str, str]) -> bool:
    """Install through `adb install`; returns whether the rooted fallback ran."""
    command = [
        str(adb_executable()),
        "-s",
        serial,
        "install",
        "-r",
        "-g",
        str(APK),
    ]
    process = subprocess.Popen(command, env=environment)
    deadline = time.monotonic() + INSTALL_TIMEOUT_SECONDS
    prompt_clicked = False
    while process.poll() is None:
        if not prompt_clicked:
            try:
                button = _install_prompt_button(serial, environment)
            except (OSError, subprocess.TimeoutExpired):
                button = None
            if button is not None:
                x, y = button
                print(
                    f"android_install_confirmation=click x={x} y={y}",
                    flush=True,
                )
                _adb(
                    serial,
                    environment,
                    "shell",
                    "input",
                    "tap",
                    str(x),
                    str(y),
                    timeout=5.0,
                )
                prompt_clicked = True
        if time.monotonic() >= deadline:
            process.kill()
            _ = process.wait()
            if _root_install_available(serial, environment):
                print("android_install_fallback=root", flush=True)
                _install_apk_via_root(serial, environment)
                return True
            detail = f"timed out after {INSTALL_TIMEOUT_SECONDS} seconds: {' '.join(command)}"
            raise SystemExit(detail)
        time.sleep(0.5)
    if process.returncode != 0:
        raise SystemExit(process.returncode)
    return False


def _root_install_available(serial: str, environment: dict[str, str]) -> bool:
    """Return whether `adb shell su` exposes uid 0 without changing device state."""
    try:
        result = subprocess.run(
            [str(adb_executable()), "-s", serial, "shell", "su", "-c", "id"],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return (
        result.returncode == 0
        and re.search(r"uid=0(?:\(|\s|$)", result.stdout or "") is not None
    )


def _install_apk_via_root(serial: str, environment: dict[str, str]) -> None:
    """Install through `pm install` so an OEM protection screen cannot block the upload.

    Cleanup removes only this invocation's temporary APK. A failed upload is never reported as
    success, and the PIN/credential-free fixture needs no on-device approval.
    """
    _adb(serial, environment, "push", str(APK), ROOT_INSTALL_REMOTE, timeout=60.0)
    try:
        output = _adb_output(
            serial,
            environment,
            "shell",
            "su",
            "-c",
            f"pm install -r -g {ROOT_INSTALL_REMOTE}",
            timeout=ROOT_INSTALL_TIMEOUT_SECONDS,
        )
    finally:
        _adb(
            serial, environment, "shell", "rm", "-f", ROOT_INSTALL_REMOTE, timeout=15.0
        )
    if "Success" not in output:
        raise SystemExit(f"root APK install was not confirmed: {output.strip()}")


def _open_installed_app(serial: str, environment: dict[str, str]) -> None:
    success_deadline = time.monotonic() + INSTALL_SUCCESS_TIMEOUT_SECONDS
    while time.monotonic() < success_deadline:
        button = _install_success_button(serial, environment)
        if button is not None:
            x, y = button
            print(f"android_install_success=click x={x} y={y}", flush=True)
            _adb(
                serial,
                environment,
                "shell",
                "input",
                "tap",
                str(x),
                str(y),
                timeout=5.0,
            )
            return
        time.sleep(0.5)


def _apk_fingerprint() -> str:
    with APK.open("rb") as apk:
        return hashlib.file_digest(apk, "sha256").hexdigest()


def _installed_apk_fingerprint(serial: str, environment: dict[str, str]) -> str | None:
    installed = subprocess.run(
        [str(adb_executable()), "-s", serial, "shell", "pm", "path", PACKAGE],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if installed.returncode != 0:
        return None
    paths = [
        line.removeprefix("package:").strip()
        for line in installed.stdout.splitlines()
        if line.startswith("package:")
    ]
    base_apk = next((path for path in paths if path.endswith("/base.apk")), None)
    if base_apk is None:
        return None
    apk = subprocess.run(
        [str(adb_executable()), "-s", serial, "exec-out", "cat", base_apk],
        env=environment,
        check=False,
        capture_output=True,
    )
    if apk.returncode != 0:
        return None
    return hashlib.sha256(apk.stdout).hexdigest()


def install(serial: str, environment: dict[str, str]) -> None:
    _require_apk()
    used_root = _install_apk(serial, environment)
    _grant_permissions(serial, environment)
    if not used_root:
        _open_installed_app(serial, environment)


def _grant_permissions(serial: str, environment: dict[str, str]) -> None:
    """`-g` normally covers this; some OEM ROMs reject the plain shell grant.

    The retry stays fail-closed: when neither the shell nor root can grant a declared runtime
    permission, the install is reported as failed instead of leaving a silently under-permissioned
    fixture behind.
    """
    for permission in REQUIRED_PERMISSIONS:
        try:
            _ = _adb_output(
                serial,
                environment,
                "shell",
                "pm",
                "grant",
                PACKAGE,
                permission,
                timeout=30.0,
            )
            continue
        except SystemExit:
            if not _root_install_available(serial, environment):
                raise
        _ = _adb_output(
            serial,
            environment,
            "shell",
            "su",
            "-c",
            f"pm grant {PACKAGE} {permission}",
            timeout=30.0,
        )


def _remove_existing_install(serial: str, environment: dict[str, str]) -> None:
    installed = subprocess.run(
        [str(adb_executable()), "-s", serial, "shell", "pm", "path", PACKAGE],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if installed.returncode != 0 or not installed.stdout.strip():
        return
    result = subprocess.run(
        [str(adb_executable()), "-s", serial, "uninstall", PACKAGE],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = result.stdout + result.stderr
    if result.returncode != 0 and "Unknown package" not in output:
        if "DELETE_FAILED_INTERNAL_ERROR" in output:
            detail = "Android package manager rejected fixture uninstall"
            detail += " (DELETE_FAILED_INTERNAL_ERROR); unlock the device, restart"
            detail += " ADB, or reboot the test device before retrying android-start"
            raise SystemExit(detail)
        raise SystemExit(output.strip() or result.returncode)


def _require_unlocked(serial: str, environment: dict[str, str]) -> None:
    output = _adb_output(serial, environment, "shell", "dumpsys", "window")
    if "mInputRestricted=true" in output or "mDreamingLockscreen=true" in output:
        raise SystemExit(
            "Android test device is locked; unlock it before deploying the BLE fixture"
        )


def start(serial: str, environment: dict[str, str]) -> None:
    _require_unlocked(serial, environment)
    _require_apk()
    _remove_existing_install(serial, environment)
    install(serial, environment)
    _reactivate(serial, environment)
    wait_ready(serial, environment)


def _reactivate(serial: str, environment: dict[str, str]) -> None:
    _adb(
        serial,
        environment,
        "shell",
        "am",
        "force-stop",
        PACKAGE,
        timeout=30.0,
    )
    _adb(
        serial,
        environment,
        "shell",
        "am",
        "start",
        "-n",
        ADB_COMPONENT,
        timeout=30.0,
    )


def resume(serial: str, environment: dict[str, str]) -> None:
    _reactivate(serial, environment)
    wait_ready(serial, environment)


def ensure(serial: str, environment: dict[str, str]) -> None:
    _require_apk()
    fingerprint = _apk_fingerprint()
    if _installed_apk_fingerprint(serial, environment) == fingerprint:
        print(f"android_fixture_apk={fingerprint} action=resume", flush=True)
        resume(serial, environment)
        return
    print(f"android_fixture_apk={fingerprint} action=install", flush=True)
    start(serial, environment)


def wait_ready(serial: str, environment: dict[str, str]) -> None:
    deadline = time.monotonic() + 10.0
    recovery_attempted = False
    while True:
        try:
            outcome = command(serial, environment, "ready")
        except SystemExit as error:
            if recovery_attempted or "timed out" not in str(error):
                raise
            outcome = ""
        if "result=ok" in outcome:
            return
        if "fixture is not advertising: STARTING" in outcome:
            if time.monotonic() >= deadline:
                raise SystemExit(
                    "timed out waiting for Android fixture advertising readiness"
                )
            time.sleep(0.1)
            continue
        if "fixture is not advertising: STOPPED" in outcome and not recovery_attempted:
            recovery_attempted = True
            print("android_fixture_start_recovery=resume", flush=True)
            _reactivate(serial, environment)
            continue
        raise SystemExit(1)


def command(
    serial: str,
    environment: dict[str, str],
    name: str,
    value: str | None = None,
    *,
    burst_id: str | None = None,
    count: int | None = None,
    payload_bytes: int | None = None,
    timeout: float = 5.0,
) -> str:
    control_id = secrets.token_hex(8)
    arguments = [
        "shell",
        "am",
        "start",
        "-n",
        ADB_COMPONENT,
        "--es",
        "fixture_command",
        name,
        "--es",
        "control_id",
        control_id,
    ]
    if value is not None:
        arguments.extend(("--es", "value", value))
    if burst_id is not None:
        arguments.extend(("--es", "burst_id", burst_id))
    if count is not None:
        arguments.extend(("--ei", "count", str(count)))
    if payload_bytes is not None:
        arguments.extend(("--ei", "payload_bytes", str(payload_bytes)))
    _adb(serial, environment, *arguments, timeout=30.0)

    deadline = time.monotonic() + timeout
    marker = f"control id={control_id} result="
    while time.monotonic() < deadline:
        output = _adb_output(
            serial,
            environment,
            "logcat",
            "-d",
            "-v",
            "raw",
            "-s",
            f"{LOG_TAG}:V",
            "*:S",
        )
        matching = next((line for line in output.splitlines() if marker in line), None)
        if matching is not None:
            print(matching)
            return matching
        time.sleep(0.1)
    raise SystemExit(
        f"timed out after {timeout:g}s waiting for Android fixture command {control_id}"
    )


def _subscription_burst_timeout(mode: str, count: int) -> float:
    delivery_seconds = count / SUBSCRIPTION_MIN_OPERATIONS_PER_SECOND[mode]
    return max(
        SUBSCRIPTION_BURST_TIMEOUT_FLOOR_SECONDS,
        delivery_seconds + SUBSCRIPTION_BURST_OVERHEAD_SECONDS,
    )


def _subscription_burst(
    serial: str,
    environment: dict[str, str],
    mode: str,
    burst_id: str,
    count: int,
    payload_bytes: int,
) -> str:
    operation = f"{mode}-burst"
    outcome = command(
        serial,
        environment,
        operation,
        burst_id=burst_id,
        count=count,
        payload_bytes=payload_bytes,
        timeout=_subscription_burst_timeout(mode, count),
    )
    fields = dict(token.split("=", 1) for token in outcome.split() if "=" in token)
    if fields.get("operation") != operation:
        raise SystemExit(f"Android {mode} burst result has no matching operation")
    if fields.get("mode") not in (None, mode):
        raise SystemExit(f"Android {mode} burst result has the wrong mode")
    if fields.get("count") != str(count):
        raise SystemExit(f"Android {mode} burst result has the wrong count")
    if fields.get("payload_bytes") != str(payload_bytes):
        raise SystemExit(f"Android {mode} burst result has the wrong payload size")
    if fields.get("result") == "ok":
        elapsed = fields.get("local_elapsed_ns")
        try:
            elapsed_nanos = int(elapsed) if elapsed is not None else -1
        except ValueError as error:
            raise SystemExit(
                f"Android {mode} burst result has invalid local_elapsed_ns"
            ) from error
        if elapsed_nanos < 0:
            raise SystemExit(
                f"Android {mode} burst result has invalid local_elapsed_ns"
            )
    return outcome


def notification_burst(
    serial: str,
    environment: dict[str, str],
    burst_id: str,
    count: int,
    payload_bytes: int,
) -> str:
    return _subscription_burst(
        serial,
        environment,
        "notification",
        burst_id,
        count,
        payload_bytes,
    )


def indication_burst(
    serial: str,
    environment: dict[str, str],
    burst_id: str,
    count: int,
    payload_bytes: int,
) -> str:
    return _subscription_burst(
        serial,
        environment,
        "indication",
        burst_id,
        count,
        payload_bytes,
    )


def write_batch_start(
    serial: str,
    environment: dict[str, str],
    batch_id: str,
    count: int,
) -> str:
    outcome = command(
        serial,
        environment,
        "write-batch-start",
        burst_id=batch_id,
        count=count,
    )
    fields = dict(token.split("=", 1) for token in outcome.split() if "=" in token)
    if (
        fields.get("result") != "ok"
        or fields.get("operation") != "write-batch-start"
        or fields.get("batch_id") != batch_id.upper()
        or fields.get("count") != str(count)
    ):
        raise SystemExit("Android write batch start result is invalid")
    return outcome


def write_batch_status(
    serial: str,
    environment: dict[str, str],
    batch_id: str,
) -> str:
    outcome = command(
        serial,
        environment,
        "write-batch-status",
        burst_id=batch_id,
    )
    fields = dict(token.split("=", 1) for token in outcome.split() if "=" in token)
    required_counts = (
        "expected_count",
        "received_count",
        "unique_count",
        "missing_count",
        "duplicate_count",
        "out_of_order_count",
        "out_of_range_count",
        "foreign_batch_count",
        "malformed_count",
        "callback_span_ns",
    )
    if (
        fields.get("result") != "ok"
        or fields.get("operation") != "write-batch-status"
        or fields.get("batch_id") != batch_id.upper()
    ):
        raise SystemExit("Android write batch status result is invalid")
    try:
        if any(int(fields[name]) < 0 for name in required_counts):
            raise ValueError
    except (KeyError, ValueError) as error:
        raise SystemExit("Android write batch status counters are invalid") from error
    print(
        "fixture_write_batch_status "
        + " ".join(f"{name}={fields[name]}" for name in ("batch_id", *required_counts))
    )
    return outcome


def burst_id_argument(value: str) -> str:
    if len(value) != 16 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise argparse.ArgumentTypeError(
            "burst_id must be exactly 16 lowercase hexadecimal characters"
        )
    return value


def payload_bytes_argument(value: str) -> int:
    try:
        payload_bytes = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("payload_bytes must be an integer") from error
    if not 20 <= payload_bytes <= 512:
        raise argparse.ArgumentTypeError("payload_bytes must be between 20 and 512")
    return payload_bytes


def burst_count_argument(value: str) -> int:
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("count must be an integer") from error
    if not 1 <= count <= 10_000:
        raise argparse.ArgumentTypeError("count must be between 1 and 10000")
    return count


def logs(serial: str, environment: dict[str, str]) -> None:
    _adb(serial, environment, "logcat", "-d", "-s", f"{LOG_TAG}:V", "*:S")


def set_bluetooth_radio(
    serial: str, environment: dict[str, str], *, enabled: bool
) -> None:
    action = "enable" if enabled else "disable"
    expected_state = "ON" if enabled else "OFF"
    _adb(
        serial,
        environment,
        "shell",
        "svc",
        "bluetooth",
        action,
        timeout=30.0,
    )
    deadline = time.monotonic() + 30.0
    observed_state = "unknown"
    while True:
        output = _adb_output(
            serial,
            environment,
            "shell",
            "dumpsys",
            "bluetooth_manager",
        )
        observed_state = next(
            (
                line.strip().removeprefix("state:").strip()
                for line in output.splitlines()
                if line.strip().startswith("state:")
            ),
            "unknown",
        )
        if observed_state == expected_state:
            print(f"android_bluetooth_radio=STATE_{expected_state}")
            return
        if time.monotonic() >= deadline:
            state_details = f"STATE_{expected_state}; observed state={observed_state}"
            raise SystemExit(
                f"timed out waiting for Android Bluetooth radio {state_details}"
            )
        time.sleep(0.1)


def parse_args() -> Args:
    parser = argparse.ArgumentParser(
        description="Control the Android BLE test peripheral"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in (
        "install",
        "start",
        "ensure",
        "resume",
        "stop",
        "disconnect",
        "status",
        "log",
        "radio-enable",
        "radio-disable",
    ):
        _ = subparsers.add_parser(name)
    for name in ("notify", "indicate"):
        child = subparsers.add_parser(name)
        _ = child.add_argument("value", help="hex payload, for example deadbeef")
    for name in ("notification-burst", "indication-burst"):
        burst = subparsers.add_parser(name)
        _ = burst.add_argument("burst_id", type=burst_id_argument)
        _ = burst.add_argument("count", type=burst_count_argument)
        _ = burst.add_argument(
            "payload_bytes", type=payload_bytes_argument, nargs="?", default=20
        )
    write_batch_start_parser = subparsers.add_parser("write-batch-start")
    _ = write_batch_start_parser.add_argument("burst_id", type=burst_id_argument)
    _ = write_batch_start_parser.add_argument("count", type=burst_count_argument)
    write_batch_status_parser = subparsers.add_parser("write-batch-status")
    _ = write_batch_status_parser.add_argument("burst_id", type=burst_id_argument)
    parsed = parser.parse_args()
    return Args(
        command=cast(str, parsed.command),
        value=cast(str | None, getattr(parsed, "value", None)),
        burst_id=cast(str | None, getattr(parsed, "burst_id", None)),
        count=cast(int | None, getattr(parsed, "count", None)),
        payload_bytes=cast(int | None, getattr(parsed, "payload_bytes", None)),
    )


def main() -> int:
    args = parse_args()
    serial, environment = _device()
    outcome: str | None = None
    if args.command == "install":
        install(serial, environment)
    elif args.command == "start":
        start(serial, environment)
    elif args.command == "ensure":
        ensure(serial, environment)
    elif args.command == "resume":
        resume(serial, environment)
    elif args.command == "log":
        logs(serial, environment)
    elif args.command == "radio-enable":
        set_bluetooth_radio(serial, environment, enabled=True)
    elif args.command == "radio-disable":
        set_bluetooth_radio(serial, environment, enabled=False)
    elif args.command in {"notify", "indicate"}:
        outcome = command(serial, environment, args.command, args.value)
    elif args.command in {"notification-burst", "indication-burst"}:
        burst = (
            notification_burst
            if args.command == "notification-burst"
            else indication_burst
        )
        outcome = burst(
            serial,
            environment,
            cast(str, args.burst_id),
            cast(int, args.count),
            cast(int, args.payload_bytes),
        )
    elif args.command == "write-batch-start":
        outcome = write_batch_start(
            serial,
            environment,
            cast(str, args.burst_id),
            cast(int, args.count),
        )
    elif args.command == "write-batch-status":
        outcome = write_batch_status(
            serial,
            environment,
            cast(str, args.burst_id),
        )
    else:
        outcome = command(serial, environment, args.command)
    return 1 if outcome is not None and "result=error" in outcome else 0


if __name__ == "__main__":
    raise SystemExit(main())
