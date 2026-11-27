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

# Reviewed generated contract; fixtures must not follow the package allowlist.
TYPED_OPERATIONS = {
    "InitializeOperation": "begin_initialize",
    "StartScanOperation": "begin_start_scan",
    "StopScanOperation": "begin_stop_scan",
    "ConnectOperation": "begin_connect",
    "DisconnectOperation": "begin_disconnect",
    "DiscoverGattOperation": "begin_discover_gatt",
    "ReadOperation": "begin_read_characteristic",
    "WriteOperation": "begin_write_characteristic",
    "EnableNotificationsOperation": "begin_enable_notifications",
    "DisableNotificationsOperation": "begin_disable_notifications",
    "ReadRssiOperation": "begin_read_rssi",
    "MtuOperation": "begin_request_mtu",
    "SetConnectionPriorityOperation": "begin_set_connection_priority",
    "BondOperation": "begin_create_bond",
    "ReadDescriptorOperation": "begin_read_descriptor",
    "WriteDescriptorOperation": "begin_write_descriptor",
    "SetAdapterEnabledOperation": "begin_set_adapter_enabled",
    "QueryDevicesOperation": "begin_query_devices",
    "RefreshGattCacheOperation": "begin_refresh_gatt_cache",
}
BLENG_METHODS = (
    {
        "assemble_backend",
        "cancel_operation",
        "initialize",
        "start_scan",
        "stop_scan",
        "connect",
        "disconnect",
        "discover_gatt",
        "read_characteristic",
        "write_characteristic",
        "enable_notifications",
        "disable_notifications",
        "read_rssi",
        "request_mtu",
        "query_mtu",
        "set_connection_priority",
        "create_bond",
        "remove_bond",
        "read_descriptor",
        "write_descriptor",
        "next_event",
        "shutdown",
    }
    | set(TYPED_OPERATIONS.values())
    | {
        "begin_query_mtu",
        "begin_remove_bond",
        "begin_connect_with_options",
        "connect_with_options",
        "set_adapter_enabled",
        "query_devices",
        "refresh_gatt_cache",
    }
)
TYPED_EXPORTS = {
    symbol
    for name in TYPED_OPERATIONS
    for symbol in (
        f"uniffi_bleng_fn_clone_{name.lower()}",
        f"uniffi_bleng_fn_free_{name.lower()}",
        f"uniffi_bleng_fn_method_{name.lower()}_command_id",
        f"uniffi_bleng_fn_method_{name.lower()}_cancel",
        f"uniffi_bleng_fn_method_{name.lower()}_wait",
    )
}
SUPERVISOR_EXPORTS = {
    "uniffi_bleng_supervisor_fn_constructor_supervisor_new",
    "uniffi_bleng_supervisor_fn_clone_supervisor",
    "uniffi_bleng_supervisor_fn_free_supervisor",
    *(
        f"uniffi_bleng_supervisor_fn_method_supervisor_{method}"
        for method in package.SUPERVISOR_METHODS
    ),
}
EXPECTED_EXPORTS = (
    {
        "uniffi_blehub_fn_constructor_blehub_new",
        "uniffi_bleng_fn_constructor_bleng_new",
        "uniffi_bleng_fn_clone_bleng",
        "uniffi_bleng_fn_free_bleng",
        *(f"uniffi_bleng_fn_method_bleng_{method}" for method in BLENG_METHODS),
    }
    | TYPED_EXPORTS
    | SUPERVISOR_EXPORTS
)
EXPECTED_CLASSES = {
    "dev/blehub/BleHub.class",
    "dev/bleng/Bleng.class",
    *(f"dev/bleng/{name}.class" for name in TYPED_OPERATIONS),
    "dev/bleng_supervisor/Supervisor.class",
    "dev/bleng_supervisor/ForeignPlatformOwnerSession.class",
    "dev/bleng_supervisor/ForeignPlatformOwnerSessionFactory.class",
    "dev/bleng_supervisor/ForeignOwnerContext.class",
    "dev/bleng_supervisor/ForeignResourceCount.class",
    "dev/bleng_supervisor/ForeignSupervisorConfig.class",
    "dev/bleng_supervisor/SupervisorFfiException.class",
}


def write_bindings(root: Path, symbols: set[str] = EXPECTED_EXPORTS) -> None:
    for component, relative in (
        ("blehub", Path("dev/blehub/blehub.kt")),
        ("bleng", Path("dev/bleng/bleng.kt")),
        ("bleng_supervisor", Path("dev/bleng_supervisor/bleng_supervisor.kt")),
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        text = (
            "package dev.bleng_supervisor\n"
            if component == "bleng_supervisor"
            else "package dev.bleng\n"
            if component == "bleng"
            else "package dev.blehub\n"
        )
        text += "".join(
            f"    external fun {symbol}()\n"
            for symbol in sorted(symbols)
            if symbol.startswith(f"uniffi_{component}_")
        )
        destination.write_text(text, encoding="utf-8")


def write_aar(
    aar: Path,
    classes: set[str] = EXPECTED_CLASSES,
    consumer_rules: str | None = None,
) -> None:
    classes_buffer = io.BytesIO()
    with zipfile.ZipFile(classes_buffer, "w") as archive:
        for name in sorted(classes):
            archive.writestr(name, b"class")
    if consumer_rules is None:
        consumer_rules = "\n".join(package.REQUIRED_CONSUMER_RULES) + "\n"
    with zipfile.ZipFile(aar, "w") as archive:
        archive.writestr("classes.jar", classes_buffer.getvalue())
        archive.writestr("proguard.txt", consumer_rules)
        for target in native.TARGETS:
            archive.writestr(
                f"jni/{target.abi}/{package.BLENG_LIBRARY_NAME}", b"\x7fELFfixture"
            )


class BlengPackagingTests(unittest.TestCase):
    def test_consumer_rules_preserve_all_generated_namespaces(self) -> None:
        rules = (package.PACKAGING_PROJECT / "bindings/consumer-rules.pro").read_text(
            encoding="utf-8"
        )
        for namespace in ("dev.blehub.**", "dev.bleng.**", "dev.bleng_supervisor.**"):
            with self.subTest(namespace=namespace):
                self.assertIn(f"-keep class {namespace} {{ *; }}", rules)

    def test_export_audit_rejects_extra_components_and_probe_objects(self) -> None:
        expected = EXPECTED_EXPORTS
        package.validate_symbols(expected, expected)
        for symbol in (
            "uniffi_toy_fn_constructor_toy_new",
            "uniffi_bleng_fn_constructor_blengforeignprobe_new",
            "uniffi_bleng_fn_method_bleng_unreviewed_method",
            "uniffi_bleng_fn_clone_unknownoperation",
            "uniffi_bleng_fn_method_readoperation_unreviewed_method",
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
            write_bindings(root)
            self.assertEqual(EXPECTED_EXPORTS, package.validate_bindings(root))
            self.assertEqual(BLENG_METHODS, package.BLENG_METHODS)
            bleng = root / "dev/bleng/bleng.kt"
            original = bleng.read_text(encoding="utf-8")
            for text in (
                original.replace("package dev.bleng", "package uniffi.bleng"),
                original + "class BlengForeignProbe\n",
                original + "fun uniffi_bleng_fn_constructor_unreviewed_new()\n",
                original + "fun uniffi_bleng_fn_clone_unknownoperation()\n",
                original + "fun uniffi_bleng_fn_free_unknownoperation()\n",
                original + "fun uniffi_bleng_fn_method_unknownoperation_wait()\n",
                original + "fun uniffi_bleng_fn_method_unknownoperation_cancel()\n",
                original
                + "fun uniffi_bleng_fn_method_readoperation_unreviewed_method()\n",
            ):
                bleng.write_text(text, encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    package.validate_bindings(root)

    def test_each_typed_export_and_begin_is_required(self) -> None:
        required = TYPED_EXPORTS | {
            f"uniffi_bleng_fn_method_bleng_{method}"
            for method in TYPED_OPERATIONS.values()
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for symbol in sorted(required):
                with self.subTest(symbol=symbol):
                    exports = EXPECTED_EXPORTS - {symbol}
                    write_bindings(root, exports)
                    with self.assertRaisesRegex(
                        RuntimeError, "unexpected Bleng object"
                    ):
                        package.validate_bindings(root)
                    with self.assertRaisesRegex(RuntimeError, "FFI export mismatch"):
                        package.validate_symbols(exports, EXPECTED_EXPORTS)

    def test_each_typed_class_is_required_before_elf_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            aar = root / "bleng.aar"
            for name in sorted(EXPECTED_CLASSES):
                with (
                    self.subTest(missing=name),
                    patch.object(package, "WORK_ROOT", root),
                    patch.object(package.subprocess, "run") as run,
                ):
                    write_aar(aar, EXPECTED_CLASSES - {name})
                    run.return_value = subprocess.CompletedProcess(
                        [], 0, "\n".join(EXPECTED_EXPORTS), ""
                    )
                    with self.assertRaisesRegex(RuntimeError, "AAR lacks"):
                        package.audit_aar(
                            aar,
                            root / "llvm-nm",
                            EXPECTED_EXPORTS,
                            library_name=package.BLENG_LIBRARY_NAME,
                        )
                    run.assert_not_called()

    def test_aar_audit_requires_generated_consumer_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            aar = root / "bleng.aar"
            write_aar(aar, consumer_rules="")
            with (
                patch.object(package, "WORK_ROOT", root),
                patch.object(package, "validate_symbols"),
                patch.object(package.subprocess, "run") as run,
            ):
                run.return_value = subprocess.CompletedProcess(
                    [], 0, "\n".join(EXPECTED_EXPORTS), ""
                )
                with self.assertRaisesRegex(RuntimeError, "consumer rules"):
                    package.audit_aar(
                        aar,
                        root / "llvm-nm",
                        EXPECTED_EXPORTS,
                        library_name=package.BLENG_LIBRARY_NAME,
                    )
                run.assert_not_called()

    def test_aar_audit_inspects_each_elf_and_rejects_a_second_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            aar = root / "bleng.aar"
            exports = EXPECTED_EXPORTS
            write_aar(aar)
            with (
                patch.object(package, "WORK_ROOT", root),
                patch.object(package.subprocess, "run") as run,
            ):
                run.return_value = subprocess.CompletedProcess(
                    [], 0, "\n".join(exports), ""
                )
                package.audit_aar(
                    aar,
                    root / "llvm-nm",
                    exports,
                    library_name=package.BLENG_LIBRARY_NAME,
                )
                self.assertEqual(len(native.TARGETS), run.call_count)
                for call in run.call_args_list:
                    self.assertIn("--dynamic", call.args[0])
                    self.assertIn("--defined-only", call.args[0])
            with zipfile.ZipFile(aar, "a") as archive:
                archive.writestr("jni/arm64-v8a/libsecond.so", b"\x7fELF")
            with self.assertRaises(RuntimeError):
                package.audit_aar(
                    aar,
                    root / "llvm-nm",
                    exports,
                    library_name=package.BLENG_LIBRARY_NAME,
                )

    def test_aar_rejects_probe_classes_and_duplicate_native_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            aar = root / "bleng.aar"
            with patch.object(package.subprocess, "run") as run:
                write_aar(aar, EXPECTED_CLASSES | {"dev/bleng/BlengForeignProbe.class"})
                with self.assertRaisesRegex(RuntimeError, "probe/test class"):
                    package.audit_aar(
                        aar,
                        root / "llvm-nm",
                        EXPECTED_EXPORTS,
                        library_name=package.BLENG_LIBRARY_NAME,
                    )
                write_aar(aar)
                with (
                    zipfile.ZipFile(aar, "a") as archive,
                    self.assertWarns(UserWarning),
                ):
                    archive.writestr(
                        f"jni/{native.TARGETS[0].abi}/{package.BLENG_LIBRARY_NAME}",
                        b"\x7fELFduplicate",
                    )
                with self.assertRaisesRegex(
                    RuntimeError, "one BLeng Rust image per ABI"
                ):
                    package.audit_aar(
                        aar,
                        root / "llvm-nm",
                        EXPECTED_EXPORTS,
                        library_name=package.BLENG_LIBRARY_NAME,
                    )
                run.assert_not_called()

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
