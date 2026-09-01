from __future__ import annotations

import argparse
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tools.android_toolchain import (
    android_executable,
    sdk_root,
    select_test_device,
    toolchain_environment,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
APK = REPO_ROOT / "tests/peripherals/android/app/build/outputs/apk/debug/app-debug.apk"
PACKAGE = "dev.blehub.fixture.peripheral"
ACTIVITY_CLASS = f"{PACKAGE}.MainActivity"
ADB_COMPONENT = f"{PACKAGE}/{ACTIVITY_CLASS}"
LOG_TAG = "BleHubPeripheral"
NOTIFICATION_BURST_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class Args:
    command: str
    value: str | None
    burst_id: str | None
    count: int | None
    payload_bytes: int | None


def _device() -> tuple[str, dict[str, str]]:
    environment = toolchain_environment()
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
    adb = sdk_root() / "platform-tools/adb.exe"
    _run([str(adb), "-s", serial, *args], environment, timeout=timeout)


def _adb_output(serial: str, environment: dict[str, str], *args: str) -> str:
    adb = sdk_root() / "platform-tools/adb.exe"
    result = subprocess.run(
        [str(adb), "-s", serial, *args],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.returncode)
    return result.stdout


def install(serial: str, environment: dict[str, str]) -> None:
    _require_apk()
    _run(
        [
            str(android_executable()),
            f"--sdk={sdk_root()}",
            "install",
            f"--device={serial}",
            f"--apks={APK}",
            "--install-options=-r,-g",
        ],
        environment,
    )


def start(serial: str, environment: dict[str, str]) -> None:
    _require_apk()
    _run(
        [
            str(android_executable()),
            f"--sdk={sdk_root()}",
            "run",
            f"--device={serial}",
            f"--apks={APK}",
            f"--activity={ACTIVITY_CLASS}",
            "--type=ACTIVITY",
            "--install-options=-r,-g",
        ],
        environment,
    )
    deadline = time.monotonic() + 5.0
    while True:
        outcome = command(serial, environment, "ready")
        if "result=ok" in outcome:
            return
        if "fixture is not advertising: STARTING" not in outcome:
            raise SystemExit(1)
        if time.monotonic() >= deadline:
            raise SystemExit(
                "timed out waiting for Android fixture advertising readiness"
            )
        time.sleep(0.1)


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


def notification_burst(
    serial: str,
    environment: dict[str, str],
    burst_id: str,
    count: int,
    payload_bytes: int,
) -> str:
    outcome = command(
        serial,
        environment,
        "notification-burst",
        burst_id=burst_id,
        count=count,
        payload_bytes=payload_bytes,
        timeout=NOTIFICATION_BURST_TIMEOUT_SECONDS,
    )
    fields = dict(token.split("=", 1) for token in outcome.split() if "=" in token)
    if fields.get("operation") != "notification-burst":
        raise SystemExit("Android notification burst result has no matching operation")
    if fields.get("count") != str(count):
        raise SystemExit("Android notification burst result has the wrong count")
    if fields.get("payload_bytes") != str(payload_bytes):
        raise SystemExit("Android notification burst result has the wrong payload size")
    if fields.get("result") == "ok":
        elapsed = fields.get("local_elapsed_ns")
        try:
            elapsed_nanos = int(elapsed) if elapsed is not None else -1
        except ValueError as error:
            raise SystemExit(
                "Android notification burst result has invalid local_elapsed_ns"
            ) from error
        if elapsed_nanos < 0:
            raise SystemExit(
                "Android notification burst result has invalid local_elapsed_ns"
            )
    return outcome


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
    expected_state = "STATE_ON" if enabled else "STATE_OFF"
    _adb(
        serial,
        environment,
        "shell",
        "svc",
        "bluetooth",
        action,
        timeout=30.0,
    )
    _adb(
        serial,
        environment,
        "shell",
        "cmd",
        "bluetooth_manager",
        f"wait-for-state:{expected_state}",
        timeout=30.0,
    )
    print(f"android_bluetooth_radio={expected_state}")


def parse_args() -> Args:
    parser = argparse.ArgumentParser(
        description="Control the Android BLE test peripheral"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in (
        "install",
        "start",
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
    burst = subparsers.add_parser("notification-burst")
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
    elif args.command == "log":
        logs(serial, environment)
    elif args.command == "radio-enable":
        set_bluetooth_radio(serial, environment, enabled=True)
    elif args.command == "radio-disable":
        set_bluetooth_radio(serial, environment, enabled=False)
    elif args.command in {"notify", "indicate"}:
        outcome = command(serial, environment, args.command, args.value)
    elif args.command == "notification-burst":
        outcome = notification_burst(
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
