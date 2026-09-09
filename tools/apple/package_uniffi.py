from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_ROOT = PROJECT_ROOT / "crates" / "blehub-uniffi"
BINDGEN_MANIFEST = BRIDGE_ROOT / "Cargo.toml"
TARGET_BASE = Path(
    os.environ.get("CARGO_TARGET_DIR", PROJECT_ROOT / ".work" / "cargo-target")
)
XCFRAMEWORK_NAME = "BleHubUniFfiNative.xcframework"
SWIFT_MODULE_NAME = "BleHubUniFfi"


@dataclass(frozen=True)
class PackageLayout:
    target_dir: Path
    work_root: Path
    package_root: Path

    @property
    def bindings_root(self) -> Path:
        return self.work_root / "bindings"

    @property
    def libraries_root(self) -> Path:
        return self.work_root / "libraries"

    @property
    def headers_root(self) -> Path:
        return self.work_root / "headers"


def _package_layout() -> PackageLayout:
    return PackageLayout(
        target_dir=TARGET_BASE / "blehub-uniffi-production",
        work_root=PROJECT_ROOT / ".work" / "blehub-uniffi-apple-production",
        package_root=PROJECT_ROOT / ".work" / "staged" / "apple-production",
    )


STATIC_LIBRARY_NAME = "libblehub_uniffi.a"
COMPONENT = "blehub"

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
    name: "BleHubUniFfi",
    platforms: [
        .iOS(.v16),
        .macOS(.v13),
    ],
    products: [
        .library(
            name: "BleHubUniFfi",
            targets: ["BleHubUniFfi"]
        ),
    ],
    targets: [
        .binaryTarget(
            name: "BleHubUniFfiNative",
            path: "BleHubUniFfiNative.xcframework"
        ),
        .target(
            name: "BleHubUniFfi",
            dependencies: ["BleHubUniFfiNative"],
            path: "Sources/BleHubUniFfi"
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
    result = subprocess.run(
        command,
        check=False,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        print("+", " ".join(command), file=sys.stderr, flush=True)
        _ = sys.stderr.write(result.stdout)
        _ = result.check_returncode()


def _build_rust_slices(cargo: str, layout: PackageLayout) -> None:
    for target, deployment_target, environment_key in RUST_TARGETS:
        environment = os.environ.copy()
        environment[environment_key] = deployment_target
        environment["CARGO_TARGET_DIR"] = str(layout.target_dir)
        _run(
            [
                cargo,
                "build",
                "--quiet",
                "--locked",
                "--manifest-path",
                str(BINDGEN_MANIFEST),
                "--no-default-features",
                "--lib",
                "--release",
                "--target",
                target,
            ],
            env=environment,
        )
        library = layout.target_dir / target / "release" / STATIC_LIBRARY_NAME
        if not library.is_file():
            raise RuntimeError(
                f"Cargo did not produce expected Apple archive: {library}"
            )


def _create_universal_archives(lipo: str, layout: PackageLayout) -> dict[str, Path]:
    library_name = STATIC_LIBRARY_NAME
    slice_sources = {
        "ios-device": [
            layout.target_dir / "aarch64-apple-ios" / "release" / library_name
        ],
        "ios-simulator": [
            layout.target_dir / "aarch64-apple-ios-sim" / "release" / library_name,
            layout.target_dir / "x86_64-apple-ios" / "release" / library_name,
        ],
        "macos": [
            layout.target_dir / "aarch64-apple-darwin" / "release" / library_name,
            layout.target_dir / "x86_64-apple-darwin" / "release" / library_name,
        ],
    }
    archives: dict[str, Path] = {}
    for slice_name, sources in slice_sources.items():
        destination = layout.libraries_root / slice_name / library_name
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


def _expected_binding_files() -> set[str]:
    return {
        f"{COMPONENT}.swift",
        f"{COMPONENT}FFI.h",
        f"{COMPONENT}FFI.modulemap",
    }


def _validate_generated_surface(layout: PackageLayout) -> None:
    expected = _expected_binding_files()
    actual = {path.name for path in layout.bindings_root.iterdir() if path.is_file()}
    if actual != expected:
        raise RuntimeError(
            "unexpected production UniFFI binding surface: expected "
            + f"{sorted(expected)}, got {sorted(actual)}"
        )

    forbidden = ("FfiTest", "ffi_test", "ffiTest", "test_harness")
    for path in layout.bindings_root.iterdir():
        if path.is_file():
            source = path.read_text(encoding="utf-8")
            if any(token in source for token in forbidden):
                raise RuntimeError(
                    f"production UniFFI binding contains a test-only symbol: {path}"
                )


def _generate_bindings(cargo: str, layout: PackageLayout) -> None:
    source = layout.target_dir / "aarch64-apple-ios" / "release" / STATIC_LIBRARY_NAME
    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = str(TARGET_BASE / "blehub-uniffi-bindgen")
    _run(
        [
            cargo,
            "run",
            "--quiet",
            "--locked",
            "--manifest-path",
            str(BINDGEN_MANIFEST),
            "--no-default-features",
            "--features",
            "bindgen",
            "--bin",
            "uniffi-bindgen",
            "--",
            "generate",
            str(source),
            "--language",
            "swift",
            "--out-dir",
            str(layout.bindings_root),
            "--no-format",
        ],
        env=environment,
    )
    required = [
        layout.bindings_root / f"{COMPONENT}FFI.h",
        layout.bindings_root / f"{COMPONENT}.swift",
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(
            f"UniFFI did not generate expected Apple bindings: {missing}"
        )
    _validate_generated_surface(layout)


def _module_map() -> str:
    return (
        f'module {COMPONENT}FFI {{\n    header "{COMPONENT}FFI.h"\n    export *\n}}\n'
    )


def _stage_headers(layout: PackageLayout) -> dict[str, Path]:
    header_roots: dict[str, Path] = {}
    for slice_name in ("ios-device", "ios-simulator", "macos"):
        destination = layout.headers_root / slice_name
        destination.mkdir(parents=True, exist_ok=True)
        header_name = f"{COMPONENT}FFI.h"
        _ = shutil.copy2(layout.bindings_root / header_name, destination / header_name)
        _ = (destination / "module.modulemap").write_text(
            _module_map(), encoding="utf-8", newline="\n"
        )
        header_roots[slice_name] = destination
    return header_roots


def _create_xcframework(
    xcodebuild: str,
    archives: dict[str, Path],
    header_roots: dict[str, Path],
    layout: PackageLayout,
) -> Path:
    layout.package_root.mkdir(parents=True, exist_ok=True)
    destination = layout.package_root / XCFRAMEWORK_NAME
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


def _validate_component_symbols(nm: str, archives: dict[str, Path]) -> None:
    for slice_name, archive in archives.items():
        result = subprocess.run(
            [nm, "-gj", str(archive)],
            check=False,
            capture_output=True,
            text=True,
        )
        symbols = result.stdout
        if "_uniffi_blehub_" not in symbols:
            detail = result.stderr.strip()
            raise RuntimeError(
                f"{slice_name} archive has no BleHub UniFFI symbols"
                + (f": {detail}" if detail else "")
            )
        if "_uniffi_blehub_ffi_test_harness_" in symbols:
            raise RuntimeError(
                f"{slice_name} production archive contains the obsolete "
                + "FFI test-harness component"
            )
    print("apple_production_uniffi_component_symbols=passed")


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
            message = (
                f"unexpected {slice_name} architectures: expected "
                f"{sorted(expected[slice_name])}, got {sorted(actual)}"
            )
            raise RuntimeError(message)
        print(f"apple_archive_{slice_name}_architectures={' '.join(sorted(actual))}")


def _validate_xcframework(xcframework: Path) -> None:
    info_path = xcframework / "Info.plist"
    with info_path.open("rb") as info_file:
        info_value = cast(object, plistlib.load(info_file))
    if not isinstance(info_value, dict):
        raise TypeError("XCFramework Info.plist is not a dictionary")
    info = cast(dict[str, object], info_value)
    libraries_value = info.get("AvailableLibraries")
    if not isinstance(libraries_value, list):
        raise TypeError("XCFramework Info.plist has no AvailableLibraries array")

    libraries = cast(list[object], libraries_value)
    actual: dict[tuple[str, str | None], set[str]] = {}
    for library_value in libraries:
        if not isinstance(library_value, dict):
            raise TypeError("XCFramework contains an invalid library entry")
        library = cast(dict[str, object], library_value)
        platform = library.get("SupportedPlatform")
        variant = library.get("SupportedPlatformVariant")
        architectures_value = library.get("SupportedArchitectures")
        if not isinstance(platform, str) or not isinstance(architectures_value, list):
            raise TypeError("XCFramework library entry has invalid platform metadata")
        if variant is not None and not isinstance(variant, str):
            raise TypeError("XCFramework library entry has an invalid variant")
        architecture_values = cast(list[object], architectures_value)
        if not all(
            isinstance(architecture, str) for architecture in architecture_values
        ):
            raise TypeError("XCFramework library entry has invalid architectures")
        architectures = cast(list[str], architecture_values)
        actual[(platform, variant)] = set(architectures)

    expected = {
        ("ios", None): {"arm64"},
        ("ios", "simulator"): {"arm64", "x86_64"},
        ("macos", None): {"arm64", "x86_64"},
    }
    if actual != expected:
        raise RuntimeError(
            f"unexpected XCFramework slices: expected {expected}, got {actual}"
        )

    modulemaps = sorted(xcframework.rglob("module.modulemap"))
    if len(modulemaps) != 3:
        raise RuntimeError(
            f"expected one module.modulemap per XCFramework slice, found {len(modulemaps)}"
        )
    print("apple_xcframework_slices=passed")


def _write_swift_package(xcframework: Path, layout: PackageLayout) -> None:
    source_root = layout.package_root / "Sources" / SWIFT_MODULE_NAME
    source_root.mkdir(parents=True, exist_ok=True)
    source_name = f"{COMPONENT}.swift"
    _ = shutil.copy2(layout.bindings_root / source_name, source_root / source_name)
    _ = (layout.package_root / "Package.swift").write_text(
        PACKAGE_MANIFEST, encoding="utf-8", newline="\n"
    )
    _ = (layout.package_root / "BleHubUniFfi.flavor").write_text(
        "production\n", encoding="utf-8", newline="\n"
    )
    _validate_xcframework(xcframework)


def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("BleHub UniFFI Apple packaging requires macOS and Xcode")

    parser = argparse.ArgumentParser(
        description="Package the production BleHub UniFFI library for Apple"
    )
    _ = parser.parse_args()
    layout = _package_layout()

    cargo = _required_executable("cargo")
    lipo = _required_executable("lipo")
    nm = _required_executable("nm")
    xcodebuild = _required_executable("xcodebuild")

    shutil.rmtree(layout.work_root, ignore_errors=True)
    shutil.rmtree(layout.package_root, ignore_errors=True)
    layout.bindings_root.mkdir(parents=True, exist_ok=True)

    _build_rust_slices(cargo, layout)
    archives = _create_universal_archives(lipo, layout)
    _generate_bindings(cargo, layout)
    header_roots = _stage_headers(layout)
    xcframework = _create_xcframework(xcodebuild, archives, header_roots, layout)
    _validate_architectures(lipo, archives)
    _validate_component_symbols(nm, archives)
    _write_swift_package(xcframework, layout)
    print(f"blehub_uniffi_apple_production_package={layout.package_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
