from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "platforms" / "apple"
DERIVED_DATA_ROOT = PROJECT_ROOT / ".work" / "apple-backend-derived-data"
# `platforms/apple/Package.swift` treats the UniFFI package as a build input, so every
# `swift`/`xcodebuild` invocation has to name the staged package explicitly.
UNIFFI_PACKAGE_ROOT = PROJECT_ROOT / ".work" / "staged" / "apple-production"
UNIFFI_PACKAGE_ENV = "BLEHUB_APPLE_UNIFFI_PACKAGE_PATH"


def _environment() -> dict[str, str]:
    """Require the staged UniFFI package instead of letting the manifest fail opaquely."""
    if not (UNIFFI_PACKAGE_ROOT / "Package.swift").is_file():
        raise RuntimeError(
            f"missing {UNIFFI_PACKAGE_ROOT}; run `pixi run just apple prepare` first"
        )
    return {**os.environ, UNIFFI_PACKAGE_ENV: str(UNIFFI_PACKAGE_ROOT)}


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"missing required macOS executable: {name}")
    return executable


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(command), flush=True)
    _ = subprocess.run(command, cwd=cwd, env=_environment(), check=True)


def _run_success_quiet(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(command), flush=True)
    result = subprocess.run(
        command,
        cwd=cwd,
        env=_environment(),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        result.check_returncode()


def test_macos(swift: str) -> None:
    _run_success_quiet([swift, "test", "--quiet", "--package-path", str(PACKAGE_ROOT)])
    print("apple_backend_macos_tests=passed")


def _build_ios_destination(xcodebuild: str, destination: str, output_name: str) -> None:
    derived_data = DERIVED_DATA_ROOT / output_name
    shutil.rmtree(derived_data, ignore_errors=True)
    _run(
        [
            xcodebuild,
            "-quiet",
            "-scheme",
            "BleHubApple",
            "-destination",
            destination,
            "-derivedDataPath",
            str(derived_data),
            "CODE_SIGNING_ALLOWED=NO",
            "build",
        ],
        cwd=PACKAGE_ROOT,
    )


def build_ios(xcodebuild: str) -> None:
    _build_ios_destination(
        xcodebuild,
        "generic/platform=iOS Simulator",
        "ios-simulator",
    )
    _build_ios_destination(xcodebuild, "generic/platform=iOS", "ios-device")
    print("apple_backend_generic_ios_builds=passed")


def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("Apple backend tests require macOS and Xcode")

    parser = argparse.ArgumentParser(description="Build and test the Apple backend")
    parser.add_argument("command", choices=("test", "build-ios", "check"))
    args = parser.parse_args()

    swift = _required_executable("swift")
    xcodebuild = _required_executable("xcodebuild")
    if args.command in {"test", "check"}:
        test_macos(swift)
    if args.command in {"build-ios", "check"}:
        build_ios(xcodebuild)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
