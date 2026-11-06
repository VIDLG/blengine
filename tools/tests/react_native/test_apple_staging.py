"""Apple RN staging keeps backend metadata shared and native images lane-owned."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from tools.react_native import stage_apple_production, stage_bleng_apple

ROOT = Path(__file__).resolve().parents[3]
BACKEND_PODSPEC = ROOT / "platforms/apple/packaging/BleHubApple.podspec"
LANES = (stage_apple_production, stage_bleng_apple)


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

    def inputs(self, stack: ExitStack, lane: ModuleType) -> dict[str, Path]:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        combined = lane is stage_bleng_apple
        name = "apple-bleng" if combined else "apple-production"
        packaging = root / "integration/packaging/apple"
        paths = {
            "SOURCE_PACKAGE": root / ".work/staged" / name,
            "DESTINATION": root / ".work/staged" / f"{name}-react-native",
            "APPLE_BACKEND_SOURCE": root / "platforms/apple/Sources/BleHubApple",
            "APPLE_PODSPEC": root / "platforms/apple/packaging/BleHubApple.podspec",
        }
        original_packaging = (
            stage_bleng_apple.APPLE_PACKAGING_ROOT
            if combined
            else stage_apple_production.PACKAGING_ROOT
        )
        self.put(
            packaging / "Package.swift",
            (original_packaging / "Package.swift").read_bytes(),
        )
        self.put(packaging / "BleHubApple.podspec", b"stale integration template\n")
        if combined:
            paths["APPLE_BLENG_PACKAGE"] = packaging / "Package.swift"
        else:
            paths["PACKAGING_ROOT"] = packaging
            self.put(
                packaging / "BleHubUniFfi.podspec",
                (original_packaging / "BleHubUniFfi.podspec").read_bytes(),
            )
        components = (
            ("blehub", "bleng", "bleng_supervisor") if combined else ("blehub",)
        )
        for component in components:
            self.put(
                paths["SOURCE_PACKAGE"] / f"Sources/BleHubUniFfi/{component}.swift",
                f"{name}:{component}\n".encode(),
            )
        image = "libbleng_uniffi.a" if combined else "libblehub_uniffi.a"
        self.put(
            paths["SOURCE_PACKAGE"]
            / "BleHubUniFfiNative.xcframework/ios-arm64"
            / image,
            f"{name}:native image\n".encode(),
        )
        self.put(paths["APPLE_BACKEND_SOURCE"] / "Backend.swift", b"backend fixture\n")
        self.put(paths["DESTINATION"] / "previous-output", b"keep until validated\n")
        self.put(root / ".work/staged/other-lane/untouched", b"other lane\n")
        for attribute, value in paths.items():
            stack.enter_context(patch.object(lane, attribute, value))
        paths["PACKAGE_MANIFEST"] = packaging / "Package.swift"
        return paths

    def test_both_lanes_use_canonical_backend_metadata_and_preserve_their_image(
        self,
    ) -> None:
        for lane in LANES:
            with self.subTest(lane=lane.__name__), ExitStack() as stack:
                self.assertEqual(lane.APPLE_PODSPEC, BACKEND_PODSPEC)
                canonical = BACKEND_PODSPEC.read_bytes()
                paths = self.inputs(stack, lane)
                self.put(paths["APPLE_PODSPEC"], canonical)
                source_before = self.files(paths["SOURCE_PACKAGE"])
                expected_uniffi = dict(source_before)
                if lane is stage_bleng_apple:
                    podspec = lane.BLEHUB_UNIFFI_PODSPEC.encode()
                    self.assertIn(b"libbleng_uniffi.a", podspec)
                    self.assertIn(b'#include "bleng_supervisorFFI.h"', podspec)
                    self.assertNotIn(b"libblehub_uniffi.a", podspec)
                else:
                    podspec = (
                        lane.PACKAGING_ROOT / "BleHubUniFfi.podspec"
                    ).read_bytes()
                    self.assertIn(b"libblehub_uniffi.a", podspec)
                    self.assertNotIn(b"libbleng_uniffi.a", podspec)
                expected_uniffi["BleHubUniFfi.podspec"] = podspec

                lane.stage()

                destination = paths["DESTINATION"]
                self.assertEqual(
                    self.files(destination / "BleHubUniFfi"), expected_uniffi
                )
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
                    paths["PACKAGE_MANIFEST"].read_bytes(),
                )
                self.assertFalse((destination / "previous-output").exists())
                self.assertEqual(
                    (destination.parent / "other-lane/untouched").read_bytes(),
                    b"other lane\n",
                )

    def test_missing_canonical_podspec_preserves_output_without_integration_fallback(
        self,
    ) -> None:
        for lane in LANES:
            with self.subTest(lane=lane.__name__), ExitStack() as stack:
                paths = self.inputs(stack, lane)
                before = self.files(paths["DESTINATION"])
                with self.assertRaises(RuntimeError) as raised:
                    lane.stage()
                self.assertIn(str(paths["APPLE_PODSPEC"]), str(raised.exception))
                self.assertEqual(self.files(paths["DESTINATION"]), before)

    def test_missing_lane_inputs_preserve_previous_output(self) -> None:
        for lane in LANES:
            missing_inputs = ["SOURCE_PACKAGE", "APPLE_BACKEND_SOURCE", "Package.swift"]
            if lane is stage_apple_production:
                missing_inputs.append("BleHubUniFfi.podspec")
            for missing in missing_inputs:
                with (
                    self.subTest(lane=lane.__name__, missing=missing),
                    ExitStack() as stack,
                ):
                    paths = self.inputs(stack, lane)
                    self.put(paths["APPLE_PODSPEC"], b"canonical backend template\n")
                    path = paths.get(missing)
                    if path is None:
                        path = paths["PACKAGE_MANIFEST"].with_name(missing)
                    path.rename(path.with_name(path.name + ".missing"))
                    before = self.files(paths["DESTINATION"])
                    with self.assertRaises(RuntimeError) as raised:
                        lane.stage()
                    self.assertIn(str(path), str(raised.exception))
                    self.assertEqual(self.files(paths["DESTINATION"]), before)


if __name__ == "__main__":
    unittest.main()
