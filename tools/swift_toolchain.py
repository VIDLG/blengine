from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError(
            "BleHub Swift validation runs only on the project macOS/Xcode host"
        )


def _required_executable(name: str) -> Path:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"required executable was not found on PATH: {name}")
    return Path(executable).resolve()


def _developer_root() -> Path:
    try:
        result = subprocess.run(
            ["xcode-select", "-p"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        message = (
            "a selected Xcode installation is required; install Xcode and run "
            + "`sudo xcode-select --switch /Applications/Xcode.app`"
        )
        raise RuntimeError(message) from error
    root = Path(result.stdout.strip())
    if not root.is_dir():
        raise RuntimeError(f"the selected Xcode developer directory is missing: {root}")
    return root.resolve()


def verify() -> tuple[Path, Path, str]:
    _require_macos()
    _ = _developer_root()
    swiftc = _required_executable("swiftc")
    swift = _required_executable("swift")
    _ = _required_executable("xcodebuild")
    version_output = subprocess.run(
        [str(swiftc), "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if "Swift version" not in version_output:
        raise RuntimeError(f"unexpected Swift version output: {version_output!r}")
    _ = subprocess.run(
        [str(swiftc), "-typecheck", "-"],
        check=True,
        input="let blehubSwiftToolchainProbe: UInt32 = 1\n",
        text=True,
    )
    return swiftc, swift, version_output


def report() -> None:
    _require_macos()
    print("host=Darwin")
    print("toolchain_source=selected Xcode installation")
    try:
        root = _developer_root()
        swiftc, swift, version_output = verify()
        xcode_output = subprocess.run(
            ["xcodebuild", "-version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        print("installed=true")
        print(f"developer_root={root}")
        print(f"swiftc={swiftc}")
        print(f"swift={swift}")
        print(f"detected_version={version_output}")
        print(f"xcode_version={xcode_output}")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print("installed=false")
        print(f"install_error={error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the selected macOS Xcode Swift toolchain."
    )
    _ = parser.add_argument("command", choices=("path", "verify", "report"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = cast(str, args.command)
    if command == "path":
        swiftc, _, _ = verify()
        print(swiftc.parent)
    elif command == "verify":
        swiftc, _, version_output = verify()
        print(swiftc)
        print(version_output)
    else:
        report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
