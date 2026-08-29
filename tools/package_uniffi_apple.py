from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "spikes" / "uniffi-lifecycle"
MANIFEST = FIXTURE_ROOT / "Cargo.toml"
TARGET_DIR = Path(
    os.environ.get("CARGO_TARGET_DIR", PROJECT_ROOT / ".work" / "cargo-target")
)
WORK_ROOT = PROJECT_ROOT / ".work" / "uniffi-apple"
BINDINGS_ROOT = WORK_ROOT / "bindings"
LIBRARIES_ROOT = WORK_ROOT / "libraries"
HEADERS_ROOT = WORK_ROOT / "headers"
PACKAGE_ROOT = FIXTURE_ROOT / "generated" / "apple"
XCFRAMEWORK_NAME = "blehub_uniffi_lifecycleFFI.xcframework"
STATIC_LIBRARY_NAME = "libblehub_uniffi_lifecycle.a"
FFI_HEADER_NAME = "blehub_uniffi_lifecycleFFI.h"
SWIFT_SOURCE_NAME = "blehub_uniffi_lifecycle.swift"
FFI_MODULE_NAME = "blehub_uniffi_lifecycleFFI"

RUST_TARGETS = (
    ("aarch64-apple-ios", "16.0", "IPHONEOS_DEPLOYMENT_TARGET"),
    ("aarch64-apple-ios-sim", "16.0", "IPHONEOS_DEPLOYMENT_TARGET"),
    ("x86_64-apple-ios", "16.0", "IPHONEOS_DEPLOYMENT_TARGET"),
    ("aarch64-apple-darwin", "13.0", "MACOSX_DEPLOYMENT_TARGET"),
    ("x86_64-apple-darwin", "13.0", "MACOSX_DEPLOYMENT_TARGET"),
)

PACKAGE_MANIFEST = """// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "BleHubUniFfiLifecycle",
    platforms: [
        .iOS(.v16),
        .macOS(.v13),
    ],
    products: [
        .library(
            name: "BleHubUniFfiLifecycle",
            targets: ["BleHubUniFfiLifecycle"]
        ),
    ],
    targets: [
        .binaryTarget(
            name: "blehub_uniffi_lifecycleFFI",
            path: "blehub_uniffi_lifecycleFFI.xcframework"
        ),
        .target(
            name: "BleHubUniFfiLifecycle",
            dependencies: ["blehub_uniffi_lifecycleFFI"],
            path: "Sources/BleHubUniFfiLifecycle"
        ),
    ]
)
"""


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"missing required macOS executable: {name}")
    return executable


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    _ = subprocess.run(command, check=True, env=env)


def _build_rust_slices(cargo: str) -> None:
    for target, deployment_target, environment_key in RUST_TARGETS:
        environment = os.environ.copy()
        environment[environment_key] = deployment_target
        _run(
            [
                cargo,
                "build",
                "--locked",
                "--manifest-path",
                str(MANIFEST),
                "--lib",
                "--release",
                "--target",
                target,
            ],
            env=environment,
        )
        library = TARGET_DIR / target / "release" / STATIC_LIBRARY_NAME
        if not library.is_file():
            raise RuntimeError(
                f"Cargo did not produce expected Apple archive: {library}"
            )


def _create_universal_archives(lipo: str) -> dict[str, Path]:
    slice_sources = {
        "ios-device": [
            TARGET_DIR / "aarch64-apple-ios" / "release" / STATIC_LIBRARY_NAME
        ],
        "ios-simulator": [
            TARGET_DIR / "aarch64-apple-ios-sim" / "release" / STATIC_LIBRARY_NAME,
            TARGET_DIR / "x86_64-apple-ios" / "release" / STATIC_LIBRARY_NAME,
        ],
        "macos": [
            TARGET_DIR / "aarch64-apple-darwin" / "release" / STATIC_LIBRARY_NAME,
            TARGET_DIR / "x86_64-apple-darwin" / "release" / STATIC_LIBRARY_NAME,
        ],
    }
    archives: dict[str, Path] = {}
    for slice_name, sources in slice_sources.items():
        destination = LIBRARIES_ROOT / slice_name / STATIC_LIBRARY_NAME
        destination.parent.mkdir(parents=True, exist_ok=True)
        if len(sources) == 1:
            _ = shutil.copy2(sources[0], destination)
        else:
            _run(
                [
                    lipo,
                    "-create",
                    *(str(source) for source in sources),
                    "-output",
                    str(destination),
                ]
            )
        archives[slice_name] = destination
    return archives


def _generate_bindings(cargo: str) -> None:
    source = TARGET_DIR / "aarch64-apple-ios" / "release" / STATIC_LIBRARY_NAME
    _run(
        [
            cargo,
            "run",
            "--locked",
            "--manifest-path",
            str(MANIFEST),
            "--features",
            "bindgen",
            "--bin",
            "uniffi-bindgen-swift",
            "--",
            str(source),
            str(BINDINGS_ROOT),
            "--swift-sources",
            "--headers",
            "--modulemap",
            "--module-name",
            FFI_MODULE_NAME,
            "--modulemap-filename",
            "module.modulemap",
        ]
    )
    required = (
        BINDINGS_ROOT / FFI_HEADER_NAME,
        BINDINGS_ROOT / SWIFT_SOURCE_NAME,
        BINDINGS_ROOT / "module.modulemap",
    )
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(
            f"UniFFI did not generate expected Apple bindings: {missing}"
        )


def _stage_headers() -> dict[str, Path]:
    header_roots: dict[str, Path] = {}
    for slice_name in ("ios-device", "ios-simulator", "macos"):
        destination = HEADERS_ROOT / slice_name
        destination.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(BINDINGS_ROOT / FFI_HEADER_NAME, destination / FFI_HEADER_NAME)
        _ = shutil.copy2(
            BINDINGS_ROOT / "module.modulemap", destination / "module.modulemap"
        )
        header_roots[slice_name] = destination
    return header_roots


def _create_xcframework(
    xcodebuild: str,
    archives: dict[str, Path],
    header_roots: dict[str, Path],
) -> Path:
    PACKAGE_ROOT.mkdir(parents=True, exist_ok=True)
    destination = PACKAGE_ROOT / XCFRAMEWORK_NAME
    command = [xcodebuild, "-create-xcframework"]
    for slice_name in ("ios-device", "ios-simulator", "macos"):
        command.extend(
            [
                "-library",
                str(archives[slice_name]),
                "-headers",
                str(header_roots[slice_name]),
            ]
        )
    command.extend(["-output", str(destination)])
    _run(command)
    return destination


def _validate_architectures(lipo: str, archives: dict[str, Path]) -> None:
    expected = {
        "ios-device": {"arm64"},
        "ios-simulator": {"arm64", "x86_64"},
        "macos": {"arm64", "x86_64"},
    }
    for slice_name, archive in archives.items():
        result = subprocess.run(
            [lipo, "-archs", str(archive)],
            check=True,
            capture_output=True,
            text=True,
        )
        actual = set(result.stdout.strip().split())
        if actual != expected[slice_name]:
            expected_architectures = sorted(expected[slice_name])
            actual_architectures = sorted(actual)
            raise RuntimeError(
                f"unexpected {slice_name} architectures: expected {expected_architectures}, got {actual_architectures}"
            )
        print(f"apple_archive_{slice_name}_architectures={' '.join(sorted(actual))}")


def _write_swift_package(xcframework: Path, plutil: str) -> None:
    source_root = PACKAGE_ROOT / "Sources" / "BleHubUniFfiLifecycle"
    source_root.mkdir(parents=True, exist_ok=True)
    _ = shutil.copy2(BINDINGS_ROOT / SWIFT_SOURCE_NAME, source_root / SWIFT_SOURCE_NAME)
    _ = (PACKAGE_ROOT / "Package.swift").write_text(
        PACKAGE_MANIFEST,
        encoding="utf-8",
        newline="\n",
    )
    _run([plutil, "-lint", str(xcframework / "Info.plist")])
    modulemaps = sorted(xcframework.rglob("module.modulemap"))
    if len(modulemaps) != 3:
        raise RuntimeError(
            f"expected one module.modulemap per XCFramework slice, found {len(modulemaps)}"
        )
    for modulemap in modulemaps:
        print(f"apple_xcframework_modulemap={modulemap}")


def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("UniFFI Apple packaging must run on Darwin with Xcode")

    cargo = _required_executable("cargo")
    lipo = _required_executable("lipo")
    plutil = _required_executable("plutil")
    xcodebuild = _required_executable("xcodebuild")

    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    shutil.rmtree(PACKAGE_ROOT, ignore_errors=True)
    BINDINGS_ROOT.mkdir(parents=True, exist_ok=True)

    _build_rust_slices(cargo)
    archives = _create_universal_archives(lipo)
    _generate_bindings(cargo)
    header_roots = _stage_headers()
    xcframework = _create_xcframework(xcodebuild, archives, header_roots)
    _validate_architectures(lipo, archives)
    _write_swift_package(xcframework, plutil)
    print(f"uniffi_apple_package={PACKAGE_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
