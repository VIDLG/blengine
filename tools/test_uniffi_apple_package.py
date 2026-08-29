from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "spikes" / "uniffi-lifecycle"
PACKAGE_ROOT = FIXTURE_ROOT / "generated" / "apple"
CONSUMER_ROOT = FIXTURE_ROOT / "platforms" / "swift-package-consumer"
DERIVED_DATA_ROOT = PROJECT_ROOT / ".work" / "uniffi-apple-derived-data"
PACKAGE_SCHEME = "BleHubUniFfiLifecycle"


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"missing required macOS executable: {name}")
    return executable


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(command), flush=True)
    _ = subprocess.run(command, cwd=cwd, check=True)


def _require_package() -> None:
    required = [
        PACKAGE_ROOT / "Package.swift",
        PACKAGE_ROOT / "blehub_uniffi_lifecycleFFI.xcframework" / "Info.plist",
        PACKAGE_ROOT
        / "Sources"
        / "BleHubUniFfiLifecycle"
        / "blehub_uniffi_lifecycle.swift",
        CONSUMER_ROOT / "Package.swift",
        CONSUMER_ROOT / "Sources" / "Consumer" / "main.swift",
    ]
    missing = [path for path in required if not path.exists()]
    if missing:
        joined = "\n".join(str(path) for path in missing)
        raise RuntimeError(f"missing UniFFI Apple package inputs:\n{joined}")


def _build_ios_destination(xcodebuild: str, destination: str, output_name: str) -> None:
    derived_data = DERIVED_DATA_ROOT / output_name
    shutil.rmtree(derived_data, ignore_errors=True)
    _run(
        [
            xcodebuild,
            "-scheme",
            PACKAGE_SCHEME,
            "-destination",
            destination,
            "-derivedDataPath",
            str(derived_data),
            "CODE_SIGNING_ALLOWED=NO",
            "build",
        ],
        cwd=PACKAGE_ROOT,
    )


def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("the UniFFI Apple package test must run on Darwin")

    _require_package()
    swift = _required_executable("swift")
    xcodebuild = _required_executable("xcodebuild")

    _run([swift, "package", "--package-path", str(PACKAGE_ROOT), "reset"])
    _run([swift, "package", "--package-path", str(PACKAGE_ROOT), "describe"])
    _run([swift, "build", "--package-path", str(PACKAGE_ROOT), "-c", "release"])

    _run([swift, "package", "--package-path", str(CONSUMER_ROOT), "reset"])
    _run([swift, "run", "--package-path", str(CONSUMER_ROOT), "-c", "release"])

    _build_ios_destination(
        xcodebuild,
        "generic/platform=iOS Simulator",
        "ios-simulator",
    )
    _build_ios_destination(xcodebuild, "generic/platform=iOS", "ios-device")
    print("uniffi_apple_xcframework_spm_consumer=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
