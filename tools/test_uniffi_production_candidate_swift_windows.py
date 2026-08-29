from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from tools.swift_toolchain import (
    install_root,
    sdk_root,
    toolchain_environment,
    verify,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "spikes" / "uniffi-production-candidate"
GENERATED_ROOT = FIXTURE_ROOT / "generated" / "swift"
COMPONENTS = ("blehub", "blehub_uniffi_production_candidate")
SMOKE_SOURCE = FIXTURE_ROOT / "platforms" / "swift-host" / "main.swift"
NATIVE_ROOT = PROJECT_ROOT / ".work" / "cargo-target" / "debug"
NATIVE_DLL = NATIVE_ROOT / "blehub_uniffi_production_candidate.dll"
NATIVE_IMPORT_LIBRARY = NATIVE_ROOT / "blehub_uniffi_production_candidate.dll.lib"
OUTPUT_ROOT = PROJECT_ROOT / ".work" / "uniffi-production-candidate-swift-windows"
MODULE_MAP = OUTPUT_ROOT / "module.modulemap"
SMOKE_EXECUTABLE = OUTPUT_ROOT / "uniffi-production-candidate.exe"


def _generated_swift(component: str) -> Path:
    return GENERATED_ROOT / f"{component}.swift"


def _generated_header(component: str) -> Path:
    return GENERATED_ROOT / f"{component}FFI.h"


def _require_files() -> None:
    required = [
        *(_generated_swift(component) for component in COMPONENTS),
        *(_generated_header(component) for component in COMPONENTS),
        SMOKE_SOURCE,
        NATIVE_DLL,
        NATIVE_IMPORT_LIBRARY,
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        joined = "\n".join(str(path) for path in missing)
        raise RuntimeError(f"missing production-candidate Swift inputs:\n{joined}")


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
    _require_files()
    _stage_c_modules()
    swiftc, _, _ = verify()
    root = install_root()
    environment = toolchain_environment(root, swiftc.parent)
    environment["PATH"] = os.pathsep.join(
        [str(NATIVE_ROOT), environment.get("PATH", "")]
    )

    _ = subprocess.run(
        [
            str(swiftc),
            "-sdk",
            str(sdk_root(root)),
            "-I",
            str(OUTPUT_ROOT),
            "-module-name",
            "BleHubUniFfiProductionCandidate",
            *[str(_generated_swift(component)) for component in COMPONENTS],
            str(SMOKE_SOURCE),
            str(NATIVE_IMPORT_LIBRARY),
            "-o",
            str(SMOKE_EXECUTABLE),
        ],
        check=True,
        env=environment,
    )
    _ = subprocess.run([str(SMOKE_EXECUTABLE)], check=True, env=environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
