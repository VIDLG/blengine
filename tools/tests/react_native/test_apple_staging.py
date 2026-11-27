"""Apple RN staging keeps backend metadata shared and the native image lane-owned."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tools.react_native import stage_bleng_apple

ROOT = Path(__file__).resolve().parents[3]
BACKEND_PODSPEC = ROOT / "platforms/apple/packaging/BleHubApple.podspec"


class AppleStagingTests(unittest.TestCase):
    @staticmethod
    def put(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def files(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def inputs(self, stack: ExitStack) -> dict[str, Path]:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        packaging = root / "integration/packaging/apple"
        paths = {
            "SOURCE_PACKAGE": root / ".work/staged/apple-bleng",
            "DESTINATION": root / ".work/staged/apple-bleng-react-native",
            "APPLE_BACKEND_SOURCE": root / "platforms/apple/Sources/BleHubApple",
            "APPLE_PODSPEC": root / "platforms/apple/packaging/BleHubApple.podspec",
            "APPLE_PACKAGING_ROOT": packaging,
            "APPLE_BLENG_PACKAGE": packaging / "Package.swift",
        }
        self.put(
            packaging / "Package.swift",
            (stage_bleng_apple.APPLE_PACKAGING_ROOT / "Package.swift").read_bytes(),
        )
        for component in ("blehub", "bleng", "bleng_supervisor"):
            self.put(
                paths["SOURCE_PACKAGE"] / f"Sources/BleHubUniFfi/{component}.swift",
                f"apple-bleng:{component}\n".encode(),
            )
        self.put(
            paths["SOURCE_PACKAGE"]
            / "BleHubUniFfiNative.xcframework/ios-arm64"
            / "libbleng_uniffi.a",
            b"apple-bleng:native image\n",
        )
        self.put(paths["APPLE_BACKEND_SOURCE"] / "Backend.swift", b"backend fixture\n")
        self.put(paths["DESTINATION"] / "previous-output", b"keep until validated\n")
        self.put(root / ".work/staged/other-lane/untouched", b"other lane\n")
        for attribute, value in paths.items():
            stack.enter_context(patch.object(stage_bleng_apple, attribute, value))
        return paths

    def test_staging_shares_backend_metadata_and_preserves_the_bleng_image(
        self,
    ) -> None:
        with ExitStack() as stack:
            self.assertEqual(stage_bleng_apple.APPLE_PODSPEC, BACKEND_PODSPEC)
            canonical = BACKEND_PODSPEC.read_bytes()
            paths = self.inputs(stack)
            self.put(paths["APPLE_PODSPEC"], canonical)
            source_before = self.files(paths["SOURCE_PACKAGE"])
            expected_uniffi = dict(source_before)
            podspec = stage_bleng_apple.BLEHUB_UNIFFI_PODSPEC.encode()
            self.assertIn(b"libbleng_uniffi.a", podspec)
            self.assertIn(b'#include "bleng_supervisorFFI.h"', podspec)
            self.assertNotIn(b"libblehub_uniffi.a", podspec)
            expected_uniffi["BleHubUniFfi.podspec"] = podspec

            stage_bleng_apple.stage()

            destination = paths["DESTINATION"]
            self.assertEqual(self.files(destination / "BleHubUniFfi"), expected_uniffi)
            self.assertEqual(self.files(paths["SOURCE_PACKAGE"]), source_before)
            self.assertEqual(
                (destination / "BleHubApple/BleHubApple.podspec").read_bytes(),
                canonical,
            )
            self.assertEqual(
                self.files(destination / "BleHubApple/Sources/BleHubApple"),
                self.files(paths["APPLE_BACKEND_SOURCE"]),
            )
            self.assertEqual(
                (destination / "BleHubApple/Package.swift").read_bytes(),
                paths["APPLE_BLENG_PACKAGE"].read_bytes(),
            )
            self.assertFalse((destination / "previous-output").exists())
            self.assertEqual(
                (destination.parent / "other-lane/untouched").read_bytes(),
                b"other lane\n",
            )

    def test_missing_canonical_podspec_preserves_output_without_integration_fallback(
        self,
    ) -> None:
        with ExitStack() as stack:
            paths = self.inputs(stack)
            before = self.files(paths["DESTINATION"])
            with self.assertRaises(RuntimeError) as raised:
                stage_bleng_apple.stage()
            self.assertIn(str(paths["APPLE_PODSPEC"]), str(raised.exception))
            self.assertEqual(self.files(paths["DESTINATION"]), before)

    def test_missing_lane_inputs_preserve_previous_output(self) -> None:
        for missing in (
            "SOURCE_PACKAGE",
            "APPLE_BACKEND_SOURCE",
            "APPLE_BLENG_PACKAGE",
        ):
            with (
                self.subTest(missing=missing),
                ExitStack() as stack,
            ):
                paths = self.inputs(stack)
                self.put(paths["APPLE_PODSPEC"], b"canonical backend template\n")
                path = paths[missing]
                path.rename(path.with_name(path.name + ".missing"))
                before = self.files(paths["DESTINATION"])
                with self.assertRaises(RuntimeError) as raised:
                    stage_bleng_apple.stage()
                self.assertIn(str(path), str(raised.exception))
                self.assertEqual(self.files(paths["DESTINATION"]), before)


if __name__ == "__main__":
    unittest.main()
