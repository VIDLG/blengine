"""Stage the production Apple package for the RN internal artifact consumer."""

from __future__ import annotations

import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PACKAGE = PROJECT_ROOT / ".work" / "staged" / "apple-production"
DESTINATION = PROJECT_ROOT / ".work" / "staged" / "apple-production-react-native"
APPLE_BACKEND_SOURCE = PROJECT_ROOT / "platforms" / "apple" / "Sources" / "BleHubApple"
APPLE_PODSPEC = (
    PROJECT_ROOT / "platforms" / "apple" / "packaging" / "BleHubApple.podspec"
)
PACKAGING_ROOT = (
    PROJECT_ROOT / "integrations" / "react-native-internal" / "packaging" / "apple"
)


def stage() -> None:
    if not SOURCE_PACKAGE.is_dir():
        raise RuntimeError(
            f"missing Apple production package: {SOURCE_PACKAGE}; "
            "run `pixi run just ffi apple-package` first"
        )
    required_metadata = (
        PACKAGING_ROOT / "Package.swift",
        APPLE_PODSPEC,
        PACKAGING_ROOT / "BleHubUniFfi.podspec",
    )
    for path in required_metadata:
        if not path.is_file():
            raise RuntimeError(f"missing canonical RN Apple package metadata: {path}")
    if not APPLE_BACKEND_SOURCE.is_dir():
        raise RuntimeError(
            f"missing canonical Apple backend source: {APPLE_BACKEND_SOURCE}"
        )

    shutil.rmtree(DESTINATION, ignore_errors=True)
    destination_uniffi = DESTINATION / "BleHubUniFfi"
    destination_apple = DESTINATION / "BleHubApple"
    shutil.copytree(SOURCE_PACKAGE, destination_uniffi)
    shutil.copytree(
        APPLE_BACKEND_SOURCE,
        destination_apple / "Sources" / "BleHubApple",
    )
    shutil.copy2(PACKAGING_ROOT / "Package.swift", destination_apple / "Package.swift")
    shutil.copy2(
        APPLE_PODSPEC,
        destination_apple / "BleHubApple.podspec",
    )
    shutil.copy2(
        PACKAGING_ROOT / "BleHubUniFfi.podspec",
        destination_uniffi / "BleHubUniFfi.podspec",
    )
    print(f"apple_production_react_native_staging={DESTINATION}", flush=True)


if __name__ == "__main__":
    stage()
