from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATED_ROOT = PROJECT_ROOT / "tests" / "ffi" / "generated" / "swift-macos"
GENERATED_SWIFT = GENERATED_ROOT / "blehub.swift"
GENERATED_HEADER = GENERATED_ROOT / "blehubFFI.h"
SMOKE_SOURCE = PROJECT_ROOT / "tests" / "ffi" / "runtime" / "swift-host" / "main.swift"
NATIVE_ROOT = PROJECT_ROOT / ".work" / "cargo-target" / "debug"
NATIVE_LIBRARY = NATIVE_ROOT / "libblehub_uniffi.dylib"
OUTPUT_ROOT = PROJECT_ROOT / ".work" / "blehub-uniffi-runtime-swift-macos"
MODULE_MAP = OUTPUT_ROOT / "module.modulemap"
SMOKE_EXECUTABLE = OUTPUT_ROOT / "blehub-uniffi-runtime"


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"missing required macOS executable: {name}")
    return executable


def _require_files() -> None:
    required = [
        GENERATED_SWIFT,
        GENERATED_HEADER,
        SMOKE_SOURCE,
        NATIVE_LIBRARY,
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        joined = "\n".join(str(path) for path in missing)
        raise RuntimeError(f"missing BleHub UniFFI macOS runtime inputs:\n{joined}")


def _stage_c_modules() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    _ = shutil.copy2(GENERATED_HEADER, OUTPUT_ROOT / GENERATED_HEADER.name)
    _ = MODULE_MAP.write_text(
        "\n".join(
            [
                "module blehubFFI {",
                f'    header "{GENERATED_HEADER.name}"',
                "    export *",
                "}",
            ]
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("the BleHub UniFFI Swift runtime must run on Darwin")

    _require_files()
    _stage_c_modules()
    swiftc = _required_executable("swiftc")
    xcodebuild = _required_executable("xcodebuild")
    swift_version = subprocess.run(
        [swiftc, "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    xcode_version = subprocess.run(
        [xcodebuild, "-version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    print(f"swift_version={swift_version}")
    print(f"xcode_version={xcode_version}")

    _ = subprocess.run(
        [
            swiftc,
            "-I",
            str(OUTPUT_ROOT),
            "-module-name",
            "BleHubUniFfi",
            str(GENERATED_SWIFT),
            str(SMOKE_SOURCE),
            "-L",
            str(NATIVE_ROOT),
            "-lblehub_uniffi",
            "-Xlinker",
            "-rpath",
            "-Xlinker",
            str(NATIVE_ROOT),
            "-o",
            str(SMOKE_EXECUTABLE),
        ],
        check=True,
    )

    environment = os.environ.copy()
    environment["DYLD_LIBRARY_PATH"] = os.pathsep.join(
        [str(NATIVE_ROOT), environment.get("DYLD_LIBRARY_PATH", "")]
    )
    _ = subprocess.run([str(SMOKE_EXECUTABLE)], check=True, env=environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
