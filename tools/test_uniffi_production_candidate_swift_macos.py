from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "spikes" / "uniffi-production-candidate"
GENERATED_ROOT = FIXTURE_ROOT / "generated" / "swift-macos"
COMPONENTS = ("blehub", "blehub_uniffi_production_candidate")
SMOKE_SOURCE = FIXTURE_ROOT / "platforms" / "swift-host" / "main.swift"
NATIVE_ROOT = PROJECT_ROOT / ".work" / "cargo-target" / "debug"
NATIVE_LIBRARY = NATIVE_ROOT / "libblehub_uniffi_production_candidate.dylib"
OUTPUT_ROOT = PROJECT_ROOT / ".work" / "uniffi-production-candidate-swift-macos"
MODULE_MAP = OUTPUT_ROOT / "module.modulemap"
SMOKE_EXECUTABLE = OUTPUT_ROOT / "uniffi-production-candidate"


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"missing required macOS executable: {name}")
    return executable


def _generated_swift(component: str) -> Path:
    return GENERATED_ROOT / f"{component}.swift"


def _generated_header(component: str) -> Path:
    return GENERATED_ROOT / f"{component}FFI.h"


def _require_files() -> None:
    required = [
        *(_generated_swift(component) for component in COMPONENTS),
        *(_generated_header(component) for component in COMPONENTS),
        SMOKE_SOURCE,
        NATIVE_LIBRARY,
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        joined = "\n".join(str(path) for path in missing)
        raise RuntimeError(f"missing production-candidate macOS inputs:\n{joined}")


def _stage_c_modules() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    module_definitions: list[str] = []
    for component in COMPONENTS:
        header_name = f"{component}FFI.h"
        _ = shutil.copy2(_generated_header(component), OUTPUT_ROOT / header_name)
        module_definitions.extend(
            [
                f"module {component}FFI {{",
                f'    header "{header_name}"',
                "    export *",
                "}",
            ]
        )
    _ = MODULE_MAP.write_text(
        "\n".join(module_definitions) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("the production-candidate Swift smoke must run on Darwin")

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
            "BleHubUniFfiProductionCandidate",
            *[str(_generated_swift(component)) for component in COMPONENTS],
            str(SMOKE_SOURCE),
            "-L",
            str(NATIVE_ROOT),
            "-lblehub_uniffi_production_candidate",
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
