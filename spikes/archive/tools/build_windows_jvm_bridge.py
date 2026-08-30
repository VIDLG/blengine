from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import tomllib


def required_environment_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return Path(value).resolve()


def run(command: list[str], *, environment: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, env=environment, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise SystemExit(f"missing Pixi-managed executable: {name}")
    return executable


def parse_project() -> str:
    parser = argparse.ArgumentParser(
        description="Build a BoltFFI Rust library and its generated Windows JNI bridge."
    )
    _ = parser.add_argument(
        "--project",
        default="spikes/archive/boltffi-lifecycle",
        help="project directory relative to PIXI_PROJECT_ROOT",
    )
    return cast(str, parser.parse_args().project)


def configured_package(project: Path) -> str:
    config = project / "boltffi.toml"
    if not config.is_file():
        raise SystemExit(f"missing BoltFFI project configuration: {config}")
    with config.open("rb") as stream:
        package_table = tomllib.load(stream).get("package")
    if not isinstance(package_table, dict):
        raise SystemExit(f"missing [package] table in {config}")
    package = cast(dict[str, object], package_table).get("name")
    if not isinstance(package, str) or not package:
        raise SystemExit(f"missing [package].name in {config}")
    return package


def main() -> int:
    if os.name != "nt":
        raise SystemExit("the Windows JVM bridge can only be built on Windows")

    project = parse_project()
    project_root = required_environment_path("PIXI_PROJECT_ROOT")
    java_home = required_environment_path("JAVA_HOME")
    cargo_target = required_environment_path("CARGO_TARGET_DIR")
    compiler = required_executable("clang-cl")
    compiler_cache = required_executable("sccache")
    _ = required_executable("lld-link")

    spike_root = (project_root / project).resolve()
    package = configured_package(spike_root)
    generated_root = spike_root / "generated" / "kotlin" / "jni"
    generated = generated_root / "jni_glue.c"
    manifest = spike_root / "Cargo.toml"
    rust_output = cargo_target / "x86_64-pc-windows-msvc" / "debug"
    output_directory = project_root / ".work" / "jvm-native"
    output_directory.mkdir(parents=True, exist_ok=True)
    bridge_object = output_directory / f"{package}_jni.obj"
    output = output_directory / f"{package}_jni.dll"

    for required in (
        java_home / "include" / "jni.h",
        java_home / "include" / "win32" / "jni_md.h",
        generated,
        generated_root / f"{package}.h",
    ):
        if not required.is_file():
            raise SystemExit(f"missing bridge build input: {required}")

    run(
        [
            "cargo",
            "build",
            "--manifest-path",
            str(manifest),
            "--target",
            "x86_64-pc-windows-msvc",
        ]
    )

    rust_library = rust_output / f"{package}.dll"
    rust_import_library = rust_output / f"{package}.dll.lib"
    for required in (rust_library, rust_import_library):
        if not required.is_file():
            raise SystemExit(f"missing Rust MSVC build output: {required}")

    run(
        [
            compiler_cache,
            compiler,
            "/nologo",
            "/std:c11",
            "/MD",
            "/c",
            f"/I{java_home / 'include'}",
            f"/I{java_home / 'include' / 'win32'}",
            f"/I{generated_root}",
            str(generated),
            f"/Fo{bridge_object}",
        ]
    )
    run(
        [
            compiler,
            "/nologo",
            "-fuse-ld=lld",
            "/LD",
            str(bridge_object),
            str(rust_import_library),
            "/link",
            f"/OUT:{output}",
        ]
    )
    if not output.is_file():
        raise SystemExit(f"JNI bridge compiler succeeded without producing {output}")

    _ = shutil.copy2(rust_library, output_directory / rust_library.name)
    print(f"project={spike_root}")
    print(f"package={package}")
    print("abi=msvc")
    print(f"compiler={compiler}")
    print(f"rust_library={output_directory / rust_library.name}")
    print(f"jni_library={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
