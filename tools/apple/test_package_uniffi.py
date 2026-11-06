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
                "bleng_supervisor.swift",
                "bleng_supervisorFFI.h",
                "bleng_supervisorFFI.modulemap",
            },
        )
        self.assertEqual(
            package._module_map(bleng.components),
            'module blehubFFI {\n    header "blehubFFI.h"\n    export *\n}\n'
            'module blengFFI {\n    header "blengFFI.h"\n    export *\n}\n'
            "module bleng_supervisorFFI {\n"
            '    header "bleng_supervisorFFI.h"\n    export *\n}\n',
        )

    def test_generated_bindings_reject_unknown_component_and_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = package.PackageLayout(
                manifest=Path(temp) / "Cargo.toml",
                target_dir=Path(temp) / "target",
                work_root=Path(temp),
                package_root=Path(temp) / "stage",
                components=("blehub", "bleng", "bleng_supervisor"),
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

        component_symbols = (
            "_uniffi_blehub_checksum_func_x\n",
            "_uniffi_bleng_checksum_func_y\n",
            "_uniffi_bleng_supervisor_checksum_func_z\n",
        )
        all_components = "".join(component_symbols)
        check(all_components, bleng)
        check(component_symbols[0], default)
        with (
            self.subTest(case="default rejects BLeng components"),
            self.assertRaisesRegex(RuntimeError, "unexpected macos UniFFI components"),
        ):
            check(all_components, default)
        for missing in component_symbols:
            with (
                self.subTest(missing=missing),
                self.assertRaisesRegex(
                    RuntimeError, "unexpected macos UniFFI components"
                ),
            ):
                check(all_components.replace(missing, ""), bleng)
        for extra in (
            "_uniffi_toy_checksum_func_z",
            "_uniffi_bleng_supervisor_extra_fn_constructor_owner_new",
            "_ffi_toy_uniffi_contract_version",
            "_ffi_toy_rustbuffer_alloc",
            "_ffi_toy_rust_future_poll_void",
        ):
            with (
                self.subTest(extra=extra),
                self.assertRaisesRegex(
                    RuntimeError, "unexpected macos UniFFI components"
                ),
            ):
                check(all_components + extra + "\n", bleng)
        for extra in ("_uniffi_toy_unknown_export", "_ffi_toy_unknown_export"):
            with (
                self.subTest(unrecognized=extra),
                self.assertRaisesRegex(RuntimeError, "unexpected macos UniFFI symbol"),
            ):
                check(all_components + extra + "\n", bleng)
        for probe in (
            "_uniffi_bleng_foreignprobe_x",
            "_uniffi_bleng_fn_method_blengforeignprobe_run",
            "_uniffi_bleng_checksum_method_asyncprobe_run",
            "_uniffi_blehub_fn_func_ffi_test_harness_run",
            "_uniffi_bleng_async_spike_run",
        ):
            with (
                self.subTest(probe=probe),
                self.assertRaisesRegex(RuntimeError, "test-only UniFFI symbols"),
            ):
                check(all_components + probe + "\n", bleng)
        with (
            patch.object(
                package.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 1, all_components, "nm: unknown failure"
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "nm failed"),
        ):
            package._validate_component_symbols("nm", archive, bleng)
        with patch.object(
            package.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 1, all_components, "nm: Unknown attribute kind (105)\n"
            ),
        ):
            package._validate_component_symbols("nm", archive, bleng)

    def test_archive_components_use_generated_symbol_family_boundaries(self) -> None:
        layout = package._package_layout(bleng=True)
        for suffix in (
            "fn_constructor_owner_new",
            "uniffi_contract_version",
            "rustbuffer_alloc",
            "rust_future_poll_void",
        ):
            prefix = "_uniffi_" if suffix.startswith("fn_") else "_ffi_"
            lines = [
                f"{prefix}{component}_{suffix}\n" for component in layout.components
            ]
            for missing in (None, *lines):
                symbols = "".join(line for line in lines if line != missing)
                with (
                    self.subTest(suffix=suffix, missing=missing),
                    patch.object(
                        package.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], 0, symbols, ""),
                    ),
                ):
                    if missing is None:
                        package._validate_component_symbols(
                            "nm",
                            {"ios-device": Path("/unused/libbleng_uniffi.a")},
                            layout,
                        )
                    else:
                        with self.assertRaisesRegex(
                            RuntimeError, "unexpected ios-device UniFFI components"
                        ):
                            package._validate_component_symbols(
                                "nm",
                                {"ios-device": Path("/unused/libbleng_uniffi.a")},
                                layout,
                            )


if __name__ == "__main__":
    unittest.main()
