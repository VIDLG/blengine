"""Fail-closed component accounting for the BLeng API snapshot gate."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import api_snapshots

BLENG_FILES = (
    "kotlin/dev/blehub/ffi/blehub.kt",
    "kotlin/uniffi/bleng/bleng.kt",
    "swift/blehub.swift",
    "swift/bleng.swift",
    "swift/blehubFFI.h",
    "swift/blengFFI.h",
    "swift/blehubFFI.modulemap",
    "swift/blengFFI.modulemap",
)


class BlengComponentTests(unittest.TestCase):
    def test_exact_two_component_surface_and_rejects_extra_or_missing_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in BLENG_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("generated fixture", encoding="utf-8")
            kotlin = root / BLENG_FILES[1]
            imports = """import dev.blehub.ffi.BleBackend
import dev.blehub.ffi.BleEvent
import dev.blehub.ffi.AdapterSnapshot
"""
            kotlin.write_text(imports, encoding="utf-8")
            with (
                patch.object(api_snapshots, "BLENG_GENERATED_ROOT", root),
                patch.object(
                    api_snapshots, "BLENG_KOTLIN_BINDING", root / BLENG_FILES[1]
                ),
                patch.object(
                    api_snapshots, "BLENG_SWIFT_BINDING", root / BLENG_FILES[3]
                ),
                patch.object(
                    api_snapshots,
                    "_rust_snapshot",
                    return_value=("rust", api_snapshots.BLENG_DECLARATIONS["rust"]),
                ),
                patch.object(
                    api_snapshots,
                    "_kotlin_snapshot",
                    return_value=(
                        "kotlin",
                        api_snapshots.BLENG_DECLARATIONS["kotlin"],
                    ),
                ),
                patch.object(
                    api_snapshots,
                    "_swift_snapshot",
                    return_value=(
                        "swift",
                        api_snapshots.BLENG_DECLARATIONS["swift"],
                    ),
                ),
            ):
                self.assertEqual(
                    api_snapshots._generate(bleng=True),
                    {"rust": "rust", "kotlin": "kotlin", "swift": "swift"},
                )
                extra = root / "swift" / "foreign_probe.swift"
                extra.write_text("unexpected", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "unexpected"):
                    api_snapshots._generate(bleng=True)
                extra.unlink()
                kotlin.write_text(
                    imports + "data class AdapterSnapshot()\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(RuntimeError, "duplicates blehub"):
                    api_snapshots._generate(bleng=True)
                kotlin.write_text("generated fixture", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "import canonical"):
                    api_snapshots._generate(bleng=True)
                kotlin.write_text(imports, encoding="utf-8")
                swift = root / BLENG_FILES[3]
                swift.write_text("public struct ScanOptions {}\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "duplicates canonical"):
                    api_snapshots._generate(bleng=True)
                swift.write_text("generated fixture", encoding="utf-8")
                kotlin.unlink()
                with self.assertRaisesRegex(
                    RuntimeError, "generated bindings are missing"
                ):
                    api_snapshots._generate(bleng=True)


if __name__ == "__main__":
    unittest.main()
