"""Check that each RN host contains one production UniFFI native image."""

from __future__ import annotations

import argparse
import glob
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import cast

IOS_SYMBOLS = (
    "_ffi_blehub_uniffi_contract_version",
    "_ffi_blehub_rustbuffer_free",
    "_uniffi_blehub_fn_constructor_blehub_new",
)


def check_android(apk: Path) -> None:
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()

    native_names = [
        name for name in names if name.startswith("lib/") and name.endswith(".so")
    ]
    image_names = [
        name for name in native_names if name.endswith("/libblehub_uniffi.so")
    ]
    nitro_names = [
        name for name in native_names if name.endswith("/libBleHubReactNative.so")
    ]
    unexpected_blehub_names = [
        name
        for name in native_names
        if "blehub" in Path(name).name.lower()
        and Path(name).name not in {"libblehub_uniffi.so", "libBleHubReactNative.so"}
    ]

    if not image_names:
        raise AssertionError(f"{apk} contains no libblehub_uniffi.so")
    if not nitro_names:
        raise AssertionError(f"{apk} contains no libBleHubReactNative.so")
    image_abis = [Path(name).parent.name for name in image_names]
    nitro_abis = [Path(name).parent.name for name in nitro_names]
    if len(image_names) != len(set(image_abis)):
        raise AssertionError(f"duplicate libblehub_uniffi.so in an ABI: {image_names}")
    if len(nitro_names) != len(set(nitro_abis)):
        raise AssertionError(f"duplicate Nitro native library in an ABI: {nitro_names}")
    if unexpected_blehub_names:
        raise AssertionError(
            f"unexpected BleHub native images: {unexpected_blehub_names}"
        )

    packaged_abis = {Path(name).parent.name for name in native_names}
    if set(image_abis) != packaged_abis or set(nitro_abis) != packaged_abis:
        raise AssertionError(
            f"missing native image for APK ABI: packaged={sorted(packaged_abis)} "
            f"uniffi={sorted(set(image_abis))} nitro={sorted(set(nitro_abis))}"
        )

    message = f"Android single-image gate passed: {len(image_names)} UniFFI ABI slices and {len(nitro_names)} Nitro slices"
    print(message)


def check_ios(binary: Path | None = None) -> None:
    product_path = (
        "Build/Products/Debug-iphonesimulator/"
        "BleHubNitroHost.app/BleHubNitroHost.debug.dylib"
    )

    if binary is None:
        candidates = glob.glob(
            str(
                Path.home()
                / "Library/Developer/Xcode/DerivedData/BleHubNitroHost-*"
                / product_path
            )
        )
        if not candidates:
            raise AssertionError("no built iOS BleHubNitroHost.debug.dylib found")

        binary = max(
            (Path(candidate) for candidate in candidates),
            key=lambda path: path.stat().st_mtime,
        )
    if not binary.is_file():
        raise AssertionError(f"single-image binary does not exist: {binary}")
    symbols = subprocess.run(
        ["nm", "-a", str(binary)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    missing = [symbol for symbol in IOS_SYMBOLS if f" {symbol}\n" not in symbols]
    duplicate = [symbol for symbol in IOS_SYMBOLS if symbols.count(f" {symbol}\n") != 1]
    if missing:
        raise AssertionError(
            f"missing production UniFFI symbols in {binary}: {missing}"
        )
    if duplicate:
        raise AssertionError(
            f"duplicate production UniFFI symbols in {binary}: {duplicate}"
        )

    print(f"ios single-image gate passed: {binary}")


def main() -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("platform", choices=("android", "ios"))
    _ = parser.add_argument("binary", nargs="?", type=Path)
    args = parser.parse_args()
    platform = cast(str, args.platform)
    binary = cast(Path | None, args.binary)

    if platform == "android":
        if binary is None:
            parser.error("android requires the built APK path")
        check_android(binary)
    else:
        check_ios(binary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
