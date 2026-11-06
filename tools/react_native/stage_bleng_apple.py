"""Stage the formal Apple BLeng packages outside the React Native source tree."""

from __future__ import annotations

import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PACKAGE = PROJECT_ROOT / ".work" / "staged" / "apple-bleng"
DESTINATION = PROJECT_ROOT / ".work" / "staged" / "apple-bleng-react-native"
APPLE_BACKEND_SOURCE = PROJECT_ROOT / "platforms" / "apple" / "Sources" / "BleHubApple"
APPLE_PACKAGING_ROOT = (
    PROJECT_ROOT / "integrations" / "react-native-bleng" / "packaging" / "apple"
)
APPLE_BLENG_PACKAGE = APPLE_PACKAGING_ROOT / "Package.swift"
APPLE_PODSPEC = (
    PROJECT_ROOT / "platforms" / "apple" / "packaging" / "BleHubApple.podspec"
)

BLEHUB_UNIFFI_PODSPEC = """Pod::Spec.new do |spec|
  spec.name         = "BleHubUniFfi"
  spec.version      = "0.0.0"
  spec.summary      = "Staged BLeng Stable UniFFI Swift bindings"
  spec.description  = "Generated React Native BLeng dependency for the combined UniFFI image."
  spec.homepage     = "https://github.com/VIDLG/blehub"
  spec.authors      = "BleHub contributors"
  spec.license      = { :type => "UNLICENSED" }
  spec.source       = { :path => "." }

  spec.platforms    = { :ios => "16.0" }
  spec.module_name  = "BleHubUniFfi"
  spec.source_files = "Sources/BleHubUniFfi/*.swift"
  spec.vendored_frameworks = "BleHubUniFfiNative.xcframework"
  spec.prefix_header_contents = <<-HEADER
#include "blehubFFI.h"
#include "blengFFI.h"
#include "bleng_supervisorFFI.h"
HEADER
  spec.pod_target_xcconfig = {
    "DEFINES_MODULE" => "YES",
    "SWIFT_INSTALL_OBJC_HEADER" => "NO",
  }
  spec.user_target_xcconfig = {
    "OTHER_LDFLAGS[sdk=iphonesimulator*]" => "$(inherited) -force_load \\\"#{File.join(__dir__, "BleHubUniFfiNative.xcframework", "ios-arm64_x86_64-simulator", "libbleng_uniffi.a")}\\\"",
    "OTHER_LDFLAGS[sdk=iphoneos*]" => "$(inherited) -force_load \\\"#{File.join(__dir__, "BleHubUniFfiNative.xcframework", "ios-arm64", "libbleng_uniffi.a")}\\\"",
  }
end
"""


def stage() -> None:
    if not SOURCE_PACKAGE.is_dir():
        raise RuntimeError(
            f"missing Apple BLeng UniFFI package: {SOURCE_PACKAGE}; "
            "run `pixi run just ffi apple-bleng-package` first"
        )
    for required in (APPLE_BACKEND_SOURCE, APPLE_BLENG_PACKAGE, APPLE_PODSPEC):
        if not required.exists():
            raise RuntimeError(f"missing canonical Apple package input: {required}")

    shutil.rmtree(DESTINATION, ignore_errors=True)
    destination_uniffi = DESTINATION / "BleHubUniFfi"
    destination_apple = DESTINATION / "BleHubApple"
    shutil.copytree(SOURCE_PACKAGE, destination_uniffi)
    shutil.copytree(
        APPLE_BACKEND_SOURCE,
        destination_apple / "Sources" / "BleHubApple",
    )
    shutil.copy2(APPLE_BLENG_PACKAGE, destination_apple / "Package.swift")
    shutil.copy2(APPLE_PODSPEC, destination_apple / "BleHubApple.podspec")
    (destination_uniffi / "BleHubUniFfi.podspec").write_text(
        BLEHUB_UNIFFI_PODSPEC,
        encoding="utf-8",
        newline="\n",
    )
    print(f"bleng_apple_react_native_staging={DESTINATION}", flush=True)


if __name__ == "__main__":
    stage()
