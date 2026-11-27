"""Fail-closed component and packaging guards for Apple UniFFI staging."""

from __future__ import annotations

import plistlib
import subprocess
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from itertools import permutations
from pathlib import Path
from unittest.mock import patch

from tools.apple import package_uniffi as package


class XcframeworkMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.libraries = [
            {
                "SupportedPlatform": "ios",
                "SupportedArchitectures": ["arm64"],
                "LibraryIdentifier": "ios-arm64",
                "LibraryPath": "libbleng_uniffi.a",
                "BinaryPath": "libbleng_uniffi.a",
                "HeadersPath": "Headers",
            },
            {
                "SupportedPlatformVariant": "simulator",
                "SupportedPlatform": "ios",
                "SupportedArchitectures": ["arm64", "x86_64"],
                "LibraryIdentifier": "ios-arm64_x86_64-simulator",
                "LibraryPath": "libbleng_uniffi.a",
                "BinaryPath": "libbleng_uniffi.a",
                "HeadersPath": "Headers",
            },
            {
                "SupportedPlatform": "macos",
                "SupportedArchitectures": ["arm64", "x86_64"],
                "LibraryIdentifier": "macos-arm64_x86_64",
                "LibraryPath": "libbleng_uniffi.a",
                "BinaryPath": "libbleng_uniffi.a",
                "HeadersPath": "Headers",
            },
        ]
        self.info = {
            "XCFrameworkFormatVersion": "1.0",
            "CFBundlePackageType": "XFWK",
            "AvailableLibraries": self.libraries,
            "CustomMetadata": {"z": ["second", "first"], "a": b"opaque"},
        }

    def test_create_canonicalizes_all_six_slice_permutations(self) -> None:
        expected = plistlib.dumps(self.info, sort_keys=True)
        orders = list(permutations(self.libraries))
        self.assertEqual(len(orders), 6)
        for libraries in orders:
            with (
                self.subTest(order=[lib["LibraryIdentifier"] for lib in libraries]),
                tempfile.TemporaryDirectory() as temp,
            ):
                layout = replace(
                    package._package_layout(bleng=True), package_root=Path(temp)
                )
                destination = layout.package_root / package.XCFRAMEWORK_NAME
                archives = {
                    name: Path(temp) / name / layout.static_library_name
                    for name in ("ios-device", "ios-simulator", "macos")
                }
                headers = {
                    name: path.parent / "Headers" for name, path in archives.items()
                }
                info = dict(self.info, AvailableLibraries=list(libraries))

                def create(
                    command: list[str],
                    *,
                    output: Path = destination,
                    raw: bytes = plistlib.dumps(info, sort_keys=False),
                ) -> None:
                    self.assertEqual(command[:2], ["xcodebuild", "-create-xcframework"])
                    self.assertEqual(command[-2:], ["-output", str(output)])
                    output.mkdir()
                    (output / "Info.plist").write_bytes(raw)

                with patch.object(package, "_run", side_effect=create) as run:
                    self.assertEqual(
                        package._create_xcframework(
                            "xcodebuild", archives, headers, layout
                        ),
                        destination,
                    )
                    run.assert_called_once()
                info_path = destination / "Info.plist"
                self.assertEqual(info_path.read_bytes(), expected)
                self.assertEqual(plistlib.loads(expected), self.info)
                package._canonicalize_xcframework_info(destination)
                self.assertEqual(info_path.read_bytes(), expected)

    def test_canonicalization_preserves_metadata_drift(self) -> None:
        cases = (
            (None, "CFBundlePackageType", "changed"),
            (None, "XCFrameworkFormatVersion", "2.0"),
            (None, "CustomMetadata", {"new": True}),
            (0, "LibraryIdentifier", "changed-id"),
            (0, "SupportedPlatform", "changed"),
            (1, "SupportedPlatformVariant", "changed"),
            (1, "SupportedArchitectures", ["x86_64", "arm64"]),
            (0, "LibraryPath", "changed.a"),
            (0, "BinaryPath", "changed.a"),
            (0, "HeadersPath", "ChangedHeaders"),
            (0, "CustomSliceMetadata", {"z": 2, "a": 1}),
        )
        baseline = plistlib.dumps(self.info, sort_keys=True)
        for index, key, value in cases:
            with (
                self.subTest(index=index, key=key),
                tempfile.TemporaryDirectory() as temp,
            ):
                info = deepcopy(self.info)
                target = info if index is None else info["AvailableLibraries"][index]
                target[key] = value
                xcframework = Path(temp)
                info_path = xcframework / "Info.plist"
                info_path.write_bytes(plistlib.dumps(info, sort_keys=False))
                package._canonicalize_xcframework_info(xcframework)
                expected = dict(
                    info,
                    AvailableLibraries=sorted(
                        info["AvailableLibraries"],
                        key=lambda lib: lib["LibraryIdentifier"],
                    ),
                )
                self.assertEqual(plistlib.loads(info_path.read_bytes()), expected)
                self.assertNotEqual(info_path.read_bytes(), baseline)

    def test_canonicalization_rejects_malformed_and_duplicate_identifiers(self) -> None:
        cases = [
            ([], TypeError, "not a dictionary"),
            ({}, TypeError, "no AvailableLibraries array"),
            ({"AvailableLibraries": {}}, TypeError, "no AvailableLibraries array"),
            ({"AvailableLibraries": ["invalid"]}, TypeError, "invalid library entry"),
            ({"AvailableLibraries": [{}]}, TypeError, "invalid LibraryIdentifier"),
        ]
        for identifier in ("", "  ", 1, True, [], {}):
            cases.append(
                (
                    {"AvailableLibraries": [{"LibraryIdentifier": identifier}]},
                    TypeError,
                    "invalid LibraryIdentifier",
                )
            )
        for duplicate in (
            self.libraries[0],
            dict(self.libraries[1], LibraryIdentifier="ios-arm64"),
        ):
            cases.append(
                (
                    dict(self.info, AvailableLibraries=[*self.libraries, duplicate]),
                    RuntimeError,
                    "duplicate XCFramework LibraryIdentifier",
                )
            )
        for info, error, message in cases:
            with self.subTest(info=info), tempfile.TemporaryDirectory() as temp:
                xcframework = Path(temp)
                info_path = xcframework / "Info.plist"
                original = plistlib.dumps(info, sort_keys=False)
                info_path.write_bytes(original)
                with self.assertRaisesRegex(error, message):
                    package._canonicalize_xcframework_info(xcframework)
                self.assertEqual(info_path.read_bytes(), original)


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

    def test_xcframework_rejects_duplicate_platform_variant_entries(self) -> None:
        base = [
            {"SupportedPlatform": "ios", "SupportedArchitectures": ["arm64"]},
            {
                "SupportedPlatform": "ios",
                "SupportedPlatformVariant": "simulator",
                "SupportedArchitectures": ["arm64", "x86_64"],
            },
            {
                "SupportedPlatform": "macos",
                "SupportedArchitectures": ["arm64", "x86_64"],
            },
        ]
        cases = (
            base + [base[0].copy()],
            [
                {"SupportedPlatform": "ios", "SupportedArchitectures": ["x86_64"]},
                *base,
            ],
        )
        for libraries in cases:
            with (
                self.subTest(libraries=libraries),
                tempfile.TemporaryDirectory() as temp,
            ):
                xcframework = Path(temp) / "BleHub.xcframework"
                xcframework.mkdir()
                with (xcframework / "Info.plist").open("wb") as info_file:
                    plistlib.dump({"AvailableLibraries": libraries}, info_file)
                with self.assertRaisesRegex(
                    RuntimeError, "duplicate XCFramework slice"
                ):
                    package._validate_xcframework(xcframework, ("blehub",))

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
