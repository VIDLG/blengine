"""Fail-closed component and packaging guards for Apple UniFFI staging."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.apple import package_uniffi as package


class PackageComponentTests(unittest.TestCase):
    def test_bleng_and_default_binding_surfaces_are_isolated(self) -> None:
        default = package._package_layout()
        bleng = package._package_layout(bleng=True)
        self.assertNotEqual(default.package_root, bleng.package_root)
        self.assertNotEqual(default.target_dir, bleng.target_dir)
        self.assertNotEqual(default.work_root, bleng.work_root)
        self.assertEqual(
            package._expected_binding_files(default.components),
            {"blehub.swift", "blehubFFI.h", "blehubFFI.modulemap"},
        )
        self.assertEqual(
            package._expected_binding_files(bleng.components),
            {
                "blehub.swift",
                "blehubFFI.h",
                "blehubFFI.modulemap",
                "bleng.swift",
                "blengFFI.h",
                "blengFFI.modulemap",
            },
        )
        self.assertEqual(
            package._module_map(bleng.components),
            'module blehubFFI {\n    header "blehubFFI.h"\n    export *\n}\n'
            'module blengFFI {\n    header "blengFFI.h"\n    export *\n}\n',
        )

    def test_generated_bindings_reject_third_component_and_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = package.PackageLayout(
                manifest=Path(temp) / "Cargo.toml",
                target_dir=Path(temp) / "target",
                work_root=Path(temp),
                package_root=Path(temp) / "stage",
                components=("blehub", "bleng"),
                static_library_name="libbleng_uniffi.a",
            )
            layout.bindings_root.mkdir()
            for filename in package._expected_binding_files(layout.components):
                (layout.bindings_root / filename).write_text(
                    "// generated", encoding="utf-8"
                )
            package._validate_generated_surface(layout)
            extra = layout.bindings_root / "toy.swift"
            extra.write_text("// unexpected", encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError, "unexpected Apple UniFFI binding surface"
            ):
                package._validate_generated_surface(layout)
            extra.unlink()
            (layout.bindings_root / "bleng.swift").write_text(
                "BlengForeignProbe", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "test-only symbol"):
                package._validate_generated_surface(layout)

    def test_archive_symbols_are_exactly_the_selected_components(self) -> None:
        archive = {"macos": Path("/unused/libblehub_uniffi.a")}
        bleng = package._package_layout(bleng=True)
        default = package._package_layout()

        def check(symbols: str, layout: package.PackageLayout) -> None:
            with patch.object(
                package.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, symbols, ""),
            ):
                package._validate_component_symbols("nm", archive, layout)

        both = "_uniffi_blehub_checksum_func_x\n_uniffi_bleng_checksum_func_y\n"
        check(both, bleng)
        with self.assertRaisesRegex(RuntimeError, "unexpected macos UniFFI components"):
            check(both, default)
        with self.assertRaisesRegex(RuntimeError, "unexpected macos UniFFI components"):
            check("_uniffi_blehub_checksum_func_x\n", bleng)
        with self.assertRaisesRegex(RuntimeError, "unexpected macos UniFFI components"):
            check(both + "_uniffi_toy_checksum_func_z\n", bleng)
        with self.assertRaisesRegex(RuntimeError, "test-only UniFFI symbols"):
            check(both + "_uniffi_bleng_foreignprobe_x\n", bleng)
        with (
            patch.object(
                package.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 1, both, "nm: unknown failure"
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "nm failed"),
        ):
            package._validate_component_symbols("nm", archive, bleng)
        with patch.object(
            package.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 1, both, "nm: Unknown attribute kind (105)\n"
            ),
        ):
            package._validate_component_symbols("nm", archive, bleng)


if __name__ == "__main__":
    unittest.main()
