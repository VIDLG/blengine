from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tools.android_toolchain import toolchain_environment

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = PROJECT_ROOT / "spikes" / "uniffi-lifecycle" / "Cargo.toml"
OUTPUT_ROOT = PROJECT_ROOT / "spikes" / "uniffi-lifecycle" / "generated" / "android-jni"
LIBRARY_NAME = "libblehub_uniffi_lifecycle.so"


@dataclass(frozen=True)
class AndroidRustTarget:
    triple: str
    abi: str
    linker: str


TARGETS = (
    AndroidRustTarget(
        triple="aarch64-linux-android",
        abi="arm64-v8a",
        linker="aarch64-linux-android24-clang.cmd",
    ),
    AndroidRustTarget(
        triple="x86_64-linux-android",
        abi="x86_64",
        linker="x86_64-linux-android24-clang.cmd",
    ),
)


def main() -> int:
    environment = toolchain_environment()
    target_dir = Path(environment["CARGO_TARGET_DIR"])
    ndk_root = Path(environment["ANDROID_NDK_HOME"])
    linker_root = (
        ndk_root / "toolchains" / "llvm" / "prebuilt" / "windows-x86_64" / "bin"
    )

    for target in TARGETS:
        linker = linker_root / target.linker
        if not linker.is_file():
            raise SystemExit(f"missing Android NDK linker: {linker}")

        target_environment = environment.copy()
        target_key = target.triple.upper().replace("-", "_")
        target_environment[f"CARGO_TARGET_{target_key}_LINKER"] = str(linker)
        command = [
            "cargo",
            "build",
            "--manifest-path",
            str(MANIFEST),
            "--lib",
            "--release",
            "--target",
            target.triple,
        ]
        result = subprocess.run(command, env=target_environment, check=False)
        if result.returncode != 0:
            return result.returncode

        source = target_dir / target.triple / "release" / LIBRARY_NAME
        if not source.is_file():
            raise SystemExit(
                f"Cargo did not produce expected Android library: {source}"
            )
        destination = OUTPUT_ROOT / target.abi / LIBRARY_NAME
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        print(f"android_native_{target.abi}={destination}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
