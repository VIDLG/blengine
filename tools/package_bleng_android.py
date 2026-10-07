"""Formal two-component BLeng Android package and artifact-only build."""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from tools.android_toolchain import toolchain_environment
from tools.build_uniffi_android import DEFAULT_LIBRARY_NAME, TARGETS, _ndk_host_tag

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = PROJECT_ROOT / ".work" / "android-bleng"
TARGET_ROOT = PROJECT_ROOT / ".work" / "cargo-target" / "bleng-uniffi-android"
GENERATED_ROOT = WORK_ROOT / "generated"
MAVEN_ROOT = PROJECT_ROOT / ".work" / "staged" / "android-bleng-maven"
ARTIFACT_PATH = Path("dev/blehub/bleng/0.0.0/bleng-0.0.0.aar")
PACKAGING_PROJECT = PROJECT_ROOT / "tests/ffi/runtime/android-bleng"
CONSUMER_PROJECT = PROJECT_ROOT / "tests/consumers/android-bleng"
CONFIG = PROJECT_ROOT / "tests/ffi/config/uniffi.bleng.android.toml"
MANIFEST = PROJECT_ROOT / "crates/bleng-uniffi/Cargo.toml"
FORBIDDEN = (
    "foreignprobe",
    "foreign_probe",
    "asyncprobe",
    "async_spike",
    "ffi_test",
    "test_harness",
    "toy",
)
BLENG_METHODS = {
    "assemble_backend",
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
    "next_event",
    "shutdown",
}


def run_step(label: str, command: list[str], environment: dict[str, str]) -> None:
    """Keep build output in BLeng logs, retaining failure status and detail."""
    log = WORK_ROOT / "logs" / f"{label}.log"
    print(f"[android-bleng] {label}; log={log}", flush=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.react_native.run_quiet",
            *(["--no-tty", "--progress-seconds", "30"] if os.name == "posix" else []),
            str(log),
            "--",
            *command,
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=1800,
    )
    if result.returncode:
        raise RuntimeError(
            f"{label} failed with exit code {result.returncode}; see {log}"
        )


def validate_bindings(root: Path) -> set[str]:
    """Reject default namespaces, extra components and mechanism-only probe surfaces."""
    expected = {Path("dev/blehub/blehub.kt"), Path("dev/bleng/bleng.kt")}
    actual = {path.relative_to(root) for path in root.rglob("*.kt")}
    if actual != expected:
        raise RuntimeError(f"unexpected BLeng Kotlin files: {sorted(actual)}")
    symbols: set[str] = set()
    for relative, package in (
        (Path("dev/blehub/blehub.kt"), "dev.blehub"),
        (Path("dev/bleng/bleng.kt"), "dev.bleng"),
    ):
        text = (root / relative).read_text(encoding="utf-8")
        if not re.search(rf"^package {re.escape(package)}$", text, re.MULTILINE):
            raise RuntimeError(f"wrong Kotlin package in {relative}")
        if any(token in text.lower() for token in FORBIDDEN):
            raise RuntimeError(f"probe/test surface in {relative}")
        symbols.update(re.findall(r"\b(?:external )?fun ((?:uniffi|ffi)_\w+)\(", text))
    methods = {
        symbol.removeprefix("uniffi_bleng_fn_method_bleng_")
        for symbol in symbols
        if symbol.startswith("uniffi_bleng_fn_method_bleng_")
    }
    if (
        methods != BLENG_METHODS
        or "uniffi_bleng_fn_constructor_bleng_new" not in symbols
    ):
        raise RuntimeError(f"unexpected Bleng object surface: {sorted(methods)}")
    expected_object = {
        "uniffi_bleng_fn_constructor_bleng_new",
        "uniffi_bleng_fn_clone_bleng",
        "uniffi_bleng_fn_free_bleng",
        *(f"uniffi_bleng_fn_method_bleng_{method}" for method in BLENG_METHODS),
    }
    object_symbols = {
        symbol for symbol in symbols if symbol.startswith("uniffi_bleng_fn_")
    }
    if object_symbols != expected_object:
        raise RuntimeError(f"unexpected Bleng object exports: {sorted(object_symbols)}")
    return symbols


def validate_symbols(symbols: set[str], expected: set[str]) -> None:
    """Require precisely the generated two-component FFI surface, not just a marker."""
    exports = {symbol for symbol in symbols if symbol.startswith(("uniffi_", "ffi_"))}
    components = {
        match[1]
        for symbol in exports
        if (match := re.match(r"(?:uniffi|ffi)_([^_]+)_", symbol))
    }
    if components != {"blehub", "bleng"}:
        raise RuntimeError(f"unexpected UniFFI components: {sorted(components)}")
    if any(token in symbol.lower() for symbol in symbols for token in FORBIDDEN):
        raise RuntimeError("probe/test export in BLeng ELF")
    if exports != expected:
        raise RuntimeError(
            f"BLeng FFI export mismatch: missing={sorted(expected - exports)}, extra={sorted(exports - expected)}"
        )


def audit_aar(aar: Path, nm: Path, expected: set[str]) -> None:
    """Inspect the actual AAR ELFs with NDK llvm-nm; one Rust image per ABI."""
    expected_images = {f"jni/{target.abi}/{DEFAULT_LIBRARY_NAME}" for target in TARGETS}
    with zipfile.ZipFile(aar) as archive:
        names = archive.namelist()
        images = {name for name in names if name.endswith(".so")}
        if images != expected_images or any(names.count(name) != 1 for name in images):
            raise RuntimeError(
                f"AAR must contain one BLeng Rust image per ABI: {sorted(images)}"
            )
        with zipfile.ZipFile(io.BytesIO(archive.read("classes.jar"))) as classes:
            class_names = classes.namelist()
            if not {"dev/blehub/BleHub.class", "dev/bleng/Bleng.class"}.issubset(
                class_names
            ):
                raise RuntimeError("AAR lacks both generated component classes")
            if any(
                token in name.lower() for name in class_names for token in FORBIDDEN
            ):
                raise RuntimeError("probe/test class in BLeng AAR")
        for target in TARGETS:
            image = WORK_ROOT / "audit" / target.abi / DEFAULT_LIBRARY_NAME
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(archive.read(f"jni/{target.abi}/{DEFAULT_LIBRARY_NAME}"))
            if image.read_bytes()[:4] != b"\x7fELF":
                raise RuntimeError(f"not an ELF: {image}")
            result = subprocess.run(
                [
                    str(nm),
                    "--dynamic",
                    "--defined-only",
                    "--extern-only",
                    "--format=just-symbols",
                    str(image),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
            validate_symbols(set(result.stdout.split()), expected)
            (image.parent / "exports.txt").write_text(result.stdout, encoding="utf-8")
            print(
                f"bleng_elf_audit_{target.abi}=blehub+bleng single-image",
                flush=True,
            )


def gradle(project: Path, *tasks: str) -> list[str]:
    cache_name = "packaging" if project == PACKAGING_PROJECT else "consumer"
    return [
        "rtk",
        "gradle",
        "-p",
        str(project),
        "--project-cache-dir",
        str(WORK_ROOT / cache_name / "project-cache"),
        *tasks,
        "--no-daemon",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="audit the already staged BLeng AAR",
    )
    args = parser.parse_args()
    try:
        print("[android-bleng] toolchain-preflight started", flush=True)
        environment = toolchain_environment()
        print("[android-bleng] toolchain-preflight passed", flush=True)
        environment["CARGO_TARGET_DIR"] = str(TARGET_ROOT)
        host_tag, _ = _ndk_host_tag()
        nm = (
            Path(environment["ANDROID_NDK_HOME"])
            / "toolchains/llvm/prebuilt"
            / host_tag
            / "bin"
            / ("llvm-nm.exe" if sys.platform == "win32" else "llvm-nm")
        )
        if not nm.is_file():
            raise RuntimeError(f"missing NDK llvm-nm: {nm}")
        if args.audit_only:
            audit_aar(
                MAVEN_ROOT / ARTIFACT_PATH,
                nm,
                validate_bindings(GENERATED_ROOT / "kotlin"),
            )
            return 0
        # Only this BLeng lane's generated and published outputs are replaced.
        print("[android-bleng] resetting staging", flush=True)
        for root in (GENERATED_ROOT, MAVEN_ROOT):
            if root.exists():
                shutil.rmtree(root)
        run_step(
            "host-library",
            [
                "rtk",
                "cargo",
                "build",
                "--quiet",
                "--locked",
                "--manifest-path",
                str(MANIFEST),
                "--lib",
                "--release",
                "--target-dir",
                str(TARGET_ROOT),
            ],
            environment,
        )
        library = (
            TARGET_ROOT
            / "release"
            / (
                "libbleng_uniffi.dll"
                if sys.platform == "win32"
                else "libbleng_uniffi.dylib"
                if sys.platform == "darwin"
                else "libbleng_uniffi.so"
            )
        )
        run_step(
            "generate-kotlin",
            [
                "rtk",
                "cargo",
                "run",
                "--quiet",
                "--locked",
                "--manifest-path",
                str(MANIFEST),
                "--release",
                "--features",
                "bindgen",
                "--target-dir",
                str(TARGET_ROOT),
                "--bin",
                "uniffi-bindgen",
                "--",
                "generate",
                str(library),
                "--language",
                "kotlin",
                "--config",
                str(CONFIG),
                "--out-dir",
                str(GENERATED_ROOT / "kotlin"),
                "--no-format",
            ],
            environment,
        )
        expected = validate_bindings(GENERATED_ROOT / "kotlin")
        run_step(
            "android-native",
            [
                sys.executable,
                "-m",
                "tools.build_uniffi_android",
                "--manifest",
                str(MANIFEST),
                "--library-name",
                "libbleng_uniffi.so",
                "--target-dir",
                str(TARGET_ROOT),
                "--output-root",
                str(GENERATED_ROOT / "jni"),
            ],
            environment,
        )
        run_step(
            "assemble-aar",
            gradle(PACKAGING_PROJECT, ":bindings:assembleRelease"),
            environment,
        )
        built = WORK_ROOT / "packaging/bindings/outputs/aar/bindings-release.aar"
        audit_aar(built, nm, expected)
        run_step(
            "publish-aar",
            gradle(
                PACKAGING_PROJECT,
                ":bindings:publishReleasePublicationToBlengRepository",
            ),
            environment,
        )
        staged = MAVEN_ROOT / ARTIFACT_PATH
        if staged.read_bytes() != built.read_bytes():
            raise RuntimeError("published BLeng AAR differs from audited build")
        audit_aar(staged, nm, expected)
        run_step(
            "consumer-build",
            gradle(
                CONSUMER_PROJECT,
                ":app:verifyBlengArtifact",
                ":app:assembleDebug",
                ":app:assembleDebugAndroidTest",
            ),
            environment,
        )
        print(f"bleng_android_aar={staged}")
        print("bleng_android_consumer=static-build-passed; ART instrumentation not run")
        return 0
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
    ) as error:
        print(f"android-bleng: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
