from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tools.android_toolchain import toolchain_environment

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "crates" / "blehub-uniffi" / "Cargo.toml"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "tests" / "ffi" / "generated" / "android-jni"
DEFAULT_LIBRARY_NAME = "libblehub_uniffi.so"


@dataclass(frozen=True)
class AndroidRustTarget:
    triple: str
    abi: str
    linker: str


TARGETS = (
    AndroidRustTarget(
        triple="aarch64-linux-android",
        abi="arm64-v8a",
        linker="aarch64-linux-android24-clang",
    ),
    AndroidRustTarget(
        triple="x86_64-linux-android",
        abi="x86_64",
        linker="x86_64-linux-android24-clang",
    ),
)


def _ndk_host_tag() -> tuple[str, str]:
    if sys.platform == "win32":
        return "windows-x86_64", ".cmd"
    if sys.platform == "darwin":
        return "darwin-x86_64", ""
    if sys.platform.startswith("linux"):
        return "linux-x86_64", ""
    raise SystemExit(f"unsupported Android NDK host platform: {sys.platform}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the production BleHub UniFFI library for Android ABIs"
    )
    _ = parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    _ = parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    _ = parser.add_argument("--library-name", default=DEFAULT_LIBRARY_NAME)
    _ = parser.add_argument("--features", help="optional Cargo feature list")
    _ = parser.add_argument(
        "--target-dir", type=Path, help="isolated Cargo output root"
    )
    arguments = parser.parse_args()
    manifest = cast(Path, arguments.manifest)
    output_root = cast(Path, arguments.output_root)
    library_name = cast(str, arguments.library_name)

    environment = toolchain_environment()
    target_dir = (
        cast(Path, arguments.target_dir).resolve()
        if arguments.target_dir is not None
        else Path(environment["CARGO_TARGET_DIR"])
    )
    if arguments.target_dir is not None:
        environment["CARGO_TARGET_DIR"] = str(target_dir)
    ndk_root = Path(environment["ANDROID_NDK_HOME"])
    host_tag, executable_suffix = _ndk_host_tag()
    linker_root = ndk_root / "toolchains" / "llvm" / "prebuilt" / host_tag / "bin"

    for target in TARGETS:
        linker = linker_root / f"{target.linker}{executable_suffix}"
        if not linker.is_file():
            raise SystemExit(f"missing Android NDK linker: {linker}")

        target_environment = environment.copy()
        target_key = target.triple.upper().replace("-", "_")
        target_environment[f"CARGO_TARGET_{target_key}_LINKER"] = str(linker)
        command = [
            "cargo",
            "build",
            "--quiet",
            "--locked",
            "--manifest-path",
            str(manifest),
            "--lib",
            "--release",
            "--target",
            target.triple,
        ]
        if arguments.features:
            command.extend(["--features", cast(str, arguments.features)])
        if arguments.target_dir is not None:
            command.extend(["--target-dir", str(target_dir)])
        result = subprocess.run(command, env=target_environment, check=False)
        if result.returncode != 0:
            return result.returncode

        source = target_dir / target.triple / "release" / library_name
        if not source.is_file():
            raise SystemExit(
                f"Cargo did not produce expected Android library: {source}"
            )
        destination = output_root / target.abi / library_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(source, destination)
        print(f"android_native_{target.abi}={destination}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
