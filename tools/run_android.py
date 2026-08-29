from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from tools.android_toolchain import select_test_device, toolchain_environment


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a command with BleHub's verified Android toolchain environment."
    )
    parser.add_argument(
        "--device",
        choices=("auto", "physical", "emulator"),
        help="select a test device and start the managed emulator only when needed",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args()


def _with_gradle_device(command: list[str], serial: str) -> list[str]:
    gradle_names = {"gradle", "gradle.exe", "gradlew", "gradlew.bat"}
    if not any(Path(part).name.lower() in gradle_names for part in command):
        return command
    property_value = f"-Pandroid.injected.device.serial={serial}"
    if property_value in command:
        return command
    return [*command, property_value]


def main() -> int:
    args = parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("an Android toolchain command is required after `--`")
    try:
        environment = toolchain_environment()
        if args.device:
            selected = select_test_device(args.device, environment=environment)
            environment["ANDROID_SERIAL"] = selected.serial
            command = _with_gradle_device(command, selected.serial)
            print(f"android_test_device={selected.serial}", flush=True)
            print(f"android_test_device_kind={selected.kind}", flush=True)
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    return subprocess.run(command, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
