from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "spikes" / "boltffi-token-ingress"
PACKAGE_ROOT = FIXTURE_ROOT / "generated" / "apple"
CONSUMER_ROOT = FIXTURE_ROOT / "platforms" / "swift-package-consumer"
DERIVED_DATA_ROOT = PROJECT_ROOT / ".work" / "boltffi-apple-derived-data"
PACKAGE_SCHEME = "BleHubPhase0FfiFixtureFFI"
XCFRAMEWORK = PACKAGE_ROOT / "BleHubPhase0FfiFixture.xcframework"


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
        XCFRAMEWORK / "Info.plist",
        CONSUMER_ROOT / "Package.swift",
        CONSUMER_ROOT / "Sources" / "Consumer" / "main.swift",
    ]
    missing = [path for path in required if not path.exists()]
    if missing:
        joined = "\n".join(str(path) for path in missing)
        raise RuntimeError(f"missing BoltFFI Apple package inputs:\n{joined}")


def _xcframework_slices(
    metadata_value: object,
) -> set[tuple[str, str | None, frozenset[str]]]:
    if not isinstance(metadata_value, dict):
        raise TypeError("BoltFFI XCFramework Info.plist is not a dictionary")
    metadata = cast(dict[str, object], metadata_value)
    libraries_value = metadata.get("AvailableLibraries")
    if not isinstance(libraries_value, list):
        raise TypeError("BoltFFI XCFramework has no AvailableLibraries array")

    slices: set[tuple[str, str | None, frozenset[str]]] = set()
    for library_value in cast(list[object], libraries_value):
        if not isinstance(library_value, dict):
            raise TypeError("BoltFFI XCFramework contains an invalid library entry")
        library = cast(dict[str, object], library_value)
        platform = library.get("SupportedPlatform")
        variant = library.get("SupportedPlatformVariant")
        architectures_value = library.get("SupportedArchitectures")
        if not isinstance(platform, str):
            raise TypeError("BoltFFI XCFramework library has no platform")
        if variant is not None and not isinstance(variant, str):
            raise RuntimeError("BoltFFI XCFramework library has an invalid variant")
        if not isinstance(architectures_value, list):
            raise TypeError("BoltFFI XCFramework library has invalid architectures")
        architectures_list = cast(list[object], architectures_value)
        if not all(
            isinstance(architecture, str) for architecture in architectures_list
        ):
            raise TypeError("BoltFFI XCFramework library has invalid architectures")
        architectures = frozenset(
            architecture
            for architecture in architectures_list
            if isinstance(architecture, str)
        )
        slices.add((platform, variant, architectures))
    return slices


def _validate_xcframework(plutil: str) -> None:
    info_plist = XCFRAMEWORK / "Info.plist"
    _run([plutil, "-lint", str(info_plist)])
    with info_plist.open("rb") as stream:
        actual = _xcframework_slices(cast(object, plistlib.load(stream)))

    expected = {
        ("ios", None, frozenset({"arm64"})),
        ("ios", "simulator", frozenset({"arm64", "x86_64"})),
        ("macos", None, frozenset({"arm64", "x86_64"})),
    }
    if actual != expected:
        raise RuntimeError(
            f"unexpected BoltFFI XCFramework slices: expected {expected}, got {actual}"
        )

    modulemaps = sorted(XCFRAMEWORK.rglob("module.modulemap"))
    if len(modulemaps) != 3:
        raise RuntimeError(
            f"expected one module.modulemap per BoltFFI XCFramework slice, found {len(modulemaps)}"
        )
    for platform, variant, architectures in sorted(
        actual, key=lambda item: (item[0], item[1] or "")
    ):
        label = platform if variant is None else f"{platform}-{variant}"
        joined_architectures = " ".join(sorted(architectures))
        print(f"boltffi_apple_xcframework_{label}_architectures={joined_architectures}")
    for modulemap in modulemaps:
        print(f"boltffi_apple_xcframework_modulemap={modulemap}")


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
        raise SystemExit("the BoltFFI Apple package test must run on Darwin")

    _require_package()
    swift = _required_executable("swift")
    plutil = _required_executable("plutil")
    xcodebuild = _required_executable("xcodebuild")

    _validate_xcframework(plutil)
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
    print("boltffi_apple_xcframework_spm_consumer=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
