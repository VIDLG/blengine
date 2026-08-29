from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

import tomllib

from tools import llvm_mingw


def required_environment_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return Path(value).resolve()


def run(command: list[str], *, environment: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, env=environment, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def prepare_rust_gnu_compatibility_libraries(toolchain: Path, work_root: Path) -> Path:
    compatibility_root = work_root / "llvm-mingw-rust-gnu-compat" / "lib"
    compatibility_root.mkdir(parents=True, exist_ok=True)
    mappings = {
        toolchain / "x86_64-w64-mingw32" / "lib" / "libunwind.a": (
            compatibility_root / "libgcc_eh.a"
        ),
        toolchain
        / "lib"
        / "clang"
        / "23"
        / "lib"
        / "windows"
        / "libclang_rt.builtins-x86_64.a": compatibility_root / "libgcc.a",
    }
    for source, destination in mappings.items():
        if not source.is_file():
            raise SystemExit(f"missing LLVM-MinGW runtime library: {source}")
        if (
            not destination.is_file()
            or source.stat().st_mtime_ns != destination.stat().st_mtime_ns
        ):
            shutil.copy2(source, destination)
    return compatibility_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a BoltFFI Rust library and its generated Windows JNI bridge."
    )
    parser.add_argument(
        "--project",
        default="spikes/boltffi-lifecycle",
        help="project directory relative to PIXI_PROJECT_ROOT",
    )
    return parser.parse_args()


def configured_package(project: Path) -> str:
    config = project / "boltffi.toml"
    if not config.is_file():
        raise SystemExit(f"missing BoltFFI project configuration: {config}")
    with config.open("rb") as stream:
        package = tomllib.load(stream).get("package", {}).get("name")
    if not isinstance(package, str) or not package:
        raise SystemExit(f"missing [package].name in {config}")
    return package


def main() -> int:
    if os.name != "nt":
        raise SystemExit("the Windows JVM bridge can only be built on Windows")

    args = parse_args()
    project_root = required_environment_path("PIXI_PROJECT_ROOT")
    java_home = required_environment_path("JAVA_HOME")
    cargo_target = required_environment_path("CARGO_TARGET_DIR")
    toolchain = llvm_mingw.install()
    compiler = toolchain / "bin" / "x86_64-w64-mingw32-clang.exe"
    compiler_cache = shutil.which("sccache")
    if compiler_cache is None:
        raise SystemExit("missing Pixi-managed sccache executable")
    compatibility_libraries = prepare_rust_gnu_compatibility_libraries(
        toolchain, project_root / ".work"
    )

    spike_root = (project_root / args.project).resolve()
    package = configured_package(spike_root)
    generated_root = spike_root / "generated" / "kotlin" / "jni"
    generated = generated_root / "jni_glue.c"
    manifest = spike_root / "Cargo.toml"
    rust_output = cargo_target / "x86_64-pc-windows-gnu" / "debug"
    output_directory = project_root / ".work" / "jvm-native"
    output_directory.mkdir(parents=True, exist_ok=True)
    bridge_object = output_directory / f"{package}_jni.o"
    output = output_directory / f"{package}_jni.dll"

    for required in (
        java_home / "include" / "jni.h",
        java_home / "include" / "win32" / "jni_md.h",
        generated,
        generated_root / f"{package}.h",
    ):
        if not required.is_file():
            raise SystemExit(f"missing bridge build input: {required}")

    cargo_environment = os.environ.copy()
    cargo_environment["CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER"] = str(compiler)
    cargo_environment["CC_x86_64_pc_windows_gnu"] = str(compiler)
    rustflags = cargo_environment.get("RUSTFLAGS", "")
    cargo_environment["RUSTFLAGS"] = (
        f"{rustflags} -Lnative={compatibility_libraries}"
    ).strip()
    run(
        [
            "cargo",
            "build",
            "--manifest-path",
            str(manifest),
            "--target",
            "x86_64-pc-windows-gnu",
        ],
        environment=cargo_environment,
    )

    rust_library = rust_output / f"{package}.dll"
    rust_import_library = rust_output / f"lib{package}.dll.a"
    for required in (rust_library, rust_import_library):
        if not required.is_file():
            raise SystemExit(f"missing Rust GNU build output: {required}")

    run(
        [
            compiler_cache,
            str(compiler),
            "-std=c11",
            "-c",
            "-I",
            str(java_home / "include"),
            "-I",
            str(java_home / "include" / "win32"),
            "-I",
            str(generated_root),
            str(generated),
            "-o",
            str(bridge_object),
        ]
    )
    run(
        [
            str(compiler),
            "-shared",
            str(bridge_object),
            "-L",
            str(rust_output),
            f"-l{package}",
            "-o",
            str(output),
        ]
    )
    if not output.is_file():
        raise SystemExit(f"JNI bridge compiler succeeded without producing {output}")

    shutil.copy2(rust_library, output_directory / rust_library.name)
    print(f"project={spike_root}")
    print(f"package={package}")
    print(f"toolchain={toolchain}")
    print(f"rust_library={output_directory / rust_library.name}")
    print(f"jni_library={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
