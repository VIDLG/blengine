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


@dataclass(frozen=True)
class Args:
    command: str
    value: str | None


def _device() -> tuple[str, dict[str, str]]:
    environment = toolchain_environment()
    selected = select_test_device("physical", environment=environment)
    print(f"android_test_device={selected.serial}", flush=True)
    return selected.serial, environment


def _run(command: list[str], environment: dict[str, str]) -> None:
    result = subprocess.run(command, env=environment, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def _require_apk() -> None:
    if not APK.is_file():
        raise SystemExit(
            f"missing {APK}; run `pixi run just peripheral-android-build` first"
        )


def _adb(serial: str, environment: dict[str, str], *args: str) -> None:
    adb = sdk_root() / "platform-tools/adb.exe"
    _run([str(adb), "-s", serial, *args], environment)


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
    _adb(serial, environment, *arguments)

    deadline = time.monotonic() + 5.0
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
    raise SystemExit(f"timed out waiting for Android fixture command {control_id}")


def logs(serial: str, environment: dict[str, str]) -> None:
    _adb(serial, environment, "logcat", "-d", "-s", f"{LOG_TAG}:V", "*:S")


def parse_args() -> Args:
    parser = argparse.ArgumentParser(
        description="Control the Android BLE test peripheral"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "start", "stop", "disconnect", "status", "log"):
        _ = subparsers.add_parser(name)
    for name in ("notify", "indicate"):
        child = subparsers.add_parser(name)
        _ = child.add_argument("value", help="hex payload, for example deadbeef")
    parsed = parser.parse_args()
    return Args(
        command=cast(str, parsed.command),
        value=cast(str | None, getattr(parsed, "value", None)),
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
    elif args.command in {"notify", "indicate"}:
        outcome = command(serial, environment, args.command, args.value)
    else:
        outcome = command(serial, environment, args.command)
    return 1 if outcome is not None and "result=error" in outcome else 0


if __name__ == "__main__":
    raise SystemExit(main())
