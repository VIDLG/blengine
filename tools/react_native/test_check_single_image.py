"""Android package-ABI boundaries for the RN native-image gate."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.react_native.check_single_image import check_android


class AndroidSingleImageTests(unittest.TestCase):
    def check_package(self, abis: dict[str, set[str]]) -> None:
        with tempfile.TemporaryDirectory() as temp:
            apk = Path(temp) / "app.apk"
            with zipfile.ZipFile(apk, "w") as archive:
                for abi, libraries in abis.items():
                    for library in libraries:
                        archive.writestr(f"lib/{abi}/{library}", b"native image")
            check_android(apk)

    def test_accepts_complete_native_images_in_each_packaged_abi(self) -> None:
        libraries = {
            "libreactnative.so",
            "libblehub_uniffi.so",
            "libBleHubReactNative.so",
        }
        self.check_package({"arm64-v8a": libraries, "x86_64": libraries})

    def test_rejects_partial_abi_coverage_and_missing_nitro(self) -> None:
        common = {"libreactnative.so", "libBleHubReactNative.so"}
        with self.assertRaisesRegex(AssertionError, "missing native image for APK ABI"):
            self.check_package(
                {
                    "arm64-v8a": common | {"libblehub_uniffi.so"},
                    "x86_64": common,
                }
            )
        with self.assertRaisesRegex(AssertionError, "missing native image for APK ABI"):
            self.check_package(
                {
                    "arm64-v8a": common | {"libblehub_uniffi.so"},
                    "armeabi-v7a": {"libreactnative.so"},
                }
            )
        with self.assertRaisesRegex(AssertionError, "missing native image for APK ABI"):
            self.check_package(
                {
                    "arm64-v8a": common | {"libblehub_uniffi.so"},
                    "x86_64": {"libreactnative.so", "libblehub_uniffi.so"},
                }
            )
        with self.assertRaisesRegex(AssertionError, "contains no libBleHubReactNative"):
            self.check_package({"arm64-v8a": {"libblehub_uniffi.so"}})


if __name__ == "__main__":
    unittest.main()
