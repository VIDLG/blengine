"""Single-image boundaries for the formal Android BLeng package."""

from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import build_uniffi_android as native
from tools import package_bleng_android as package


class BlengPackagingTests(unittest.TestCase):
    def test_export_audit_rejects_extra_components_and_probe_objects(self) -> None:
        expected = {
            "uniffi_blehub_fn_constructor_blehub_new",
            "uniffi_bleng_fn_constructor_bleng_new",
        }
        package.validate_symbols(expected, expected)
        for symbol in (
            "uniffi_toy_fn_constructor_toy_new",
            "uniffi_bleng_fn_constructor_blengforeignprobe_new",
            "uniffi_bleng_fn_method_bleng_unreviewed_method",
        ):
            with self.subTest(symbol=symbol), self.assertRaises(RuntimeError):
                package.validate_symbols(expected | {symbol}, expected)
        with self.assertRaises(RuntimeError):
            package.validate_symbols(
                expected - {"uniffi_bleng_fn_constructor_bleng_new"}, expected
            )

    def test_generated_surface_requires_explicit_packages_and_reviewed_object(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for component in ("blehub", "bleng"):
                destination = root / "dev" / component / f"{component}.kt"
                destination.parent.mkdir(parents=True)
                text = f"package dev.{component}\nfun uniffi_{component}_fn_constructor_{component}_new()\n"
                if component == "bleng":
                    text += "fun uniffi_bleng_fn_clone_bleng()\nfun uniffi_bleng_fn_free_bleng()\n"
                    text += "".join(
                        f"fun uniffi_bleng_fn_method_bleng_{method}()\n"
                        for method in package.BLENG_METHODS
                    )
                destination.write_text(text, encoding="utf-8")
            self.assertIn(
                "uniffi_bleng_fn_constructor_bleng_new", package.validate_bindings(root)
            )
            bleng = root / "dev/bleng/bleng.kt"
            original = bleng.read_text(encoding="utf-8")
            for text in (
                original.replace("package dev.bleng", "package uniffi.bleng"),
                original + "class BlengForeignProbe\n",
                original + "fun uniffi_bleng_fn_constructor_unreviewed_new()\n",
            ):
                bleng.write_text(text, encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    package.validate_bindings(root)

    def test_aar_audit_inspects_each_elf_and_rejects_a_second_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            aar = root / "bleng.aar"
            classes_buffer = io.BytesIO()
            with zipfile.ZipFile(classes_buffer, "w") as classes:
                for name in ("dev/blehub/BleHub.class", "dev/bleng/Bleng.class"):
                    classes.writestr(name, b"class")
            exports = {
                "uniffi_blehub_fn_constructor_blehub_new",
                "uniffi_bleng_fn_constructor_bleng_new",
            }
            with zipfile.ZipFile(aar, "w") as archive:
                archive.writestr("classes.jar", classes_buffer.getvalue())
                for target in native.TARGETS:
                    archive.writestr(
                        f"jni/{target.abi}/{native.DEFAULT_LIBRARY_NAME}",
                        b"\x7fELFfixture",
                    )
            with (
                patch.object(package, "WORK_ROOT", root),
                patch.object(package.subprocess, "run") as run,
            ):
                run.return_value = subprocess.CompletedProcess(
                    [], 0, "\n".join(exports), ""
                )
                package.audit_aar(aar, root / "llvm-nm", exports)
                self.assertEqual(len(native.TARGETS), run.call_count)
                for call in run.call_args_list:
                    self.assertIn("--dynamic", call.args[0])
                    self.assertIn("--defined-only", call.args[0])
            with zipfile.ZipFile(aar, "a") as archive:
                archive.writestr("jni/arm64-v8a/libsecond.so", b"\x7fELF")
            with self.assertRaises(RuntimeError):
                package.audit_aar(aar, root / "llvm-nm", exports)

    def test_native_builder_routes_features_and_outputs_only_to_selected_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ndk = root / "ndk"
            host, suffix = native._ndk_host_tag()
            for target in native.TARGETS:
                linker = (
                    ndk
                    / "toolchains/llvm/prebuilt"
                    / host
                    / "bin"
                    / f"{target.linker}{suffix}"
                )
                linker.parent.mkdir(parents=True, exist_ok=True)
                linker.touch()
                image = (
                    root / "isolated" / target.triple / "release" / "libbleng_uniffi.so"
                )
                image.parent.mkdir(parents=True)
                image.write_bytes(b"bleng")
            environment = {
                "CARGO_TARGET_DIR": str(root / "production"),
                "ANDROID_NDK_HOME": str(ndk),
            }
            argv = [
                "build",
                "--manifest",
                str(root / "bleng-uniffi-Cargo.toml"),
                "--library-name",
                "libbleng_uniffi.so",
                "--target-dir",
                str(root / "isolated"),
                "--output-root",
                str(root / "jni"),
            ]
            with (
                patch.object(native.sys, "argv", argv),
                patch.object(native, "toolchain_environment", return_value=environment),
                patch.object(native.subprocess, "run") as run,
            ):
                run.return_value.returncode = 0
                self.assertEqual(0, native.main())
                for call in run.call_args_list:
                    self.assertEqual(
                        str(root / "isolated"), call.kwargs["env"]["CARGO_TARGET_DIR"]
                    )
                    self.assertIn("--manifest-path", call.args[0])
                    self.assertIn("--target-dir", call.args[0])
            self.assertFalse((root / "production").exists())
            for target in native.TARGETS:
                self.assertEqual(
                    b"bleng",
                    (root / "jni" / target.abi / "libbleng_uniffi.so").read_bytes(),
                )

    def test_native_builder_keeps_default_production_feature_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ndk = root / "ndk"
            host, suffix = native._ndk_host_tag()
            for target in native.TARGETS:
                linker = (
                    ndk
                    / "toolchains/llvm/prebuilt"
                    / host
                    / "bin"
                    / f"{target.linker}{suffix}"
                )
                linker.parent.mkdir(parents=True, exist_ok=True)
                linker.touch()
                image = (
                    root
                    / "production"
                    / target.triple
                    / "release"
                    / native.DEFAULT_LIBRARY_NAME
                )
                image.parent.mkdir(parents=True)
                image.write_bytes(b"production")
            environment = {
                "CARGO_TARGET_DIR": str(root / "production"),
                "ANDROID_NDK_HOME": str(ndk),
            }
            with (
                patch.object(
                    native.sys, "argv", ["build", "--output-root", str(root / "jni")]
                ),
                patch.object(native, "toolchain_environment", return_value=environment),
                patch.object(native.subprocess, "run") as run,
            ):
                run.return_value.returncode = 0
                self.assertEqual(0, native.main())
                for call in run.call_args_list:
                    self.assertNotIn("--features", call.args[0])
                    self.assertNotIn("--target-dir", call.args[0])
                    self.assertEqual(
                        str(root / "production"), call.kwargs["env"]["CARGO_TARGET_DIR"]
                    )
            for target in native.TARGETS:
                self.assertEqual(
                    b"production",
                    (
                        root / "jni" / target.abi / native.DEFAULT_LIBRARY_NAME
                    ).read_bytes(),
                )


if __name__ == "__main__":
    unittest.main()
