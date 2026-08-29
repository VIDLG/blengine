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
FIXTURE_ROOT = PROJECT_ROOT / "spikes" / "uniffi-lifecycle"
GENERATED_ROOT = FIXTURE_ROOT / "generated" / "swift"
GENERATED_SWIFT = GENERATED_ROOT / "blehub_uniffi_lifecycle.swift"
GENERATED_HEADER = GENERATED_ROOT / "blehub_uniffi_lifecycleFFI.h"
SMOKE_SOURCE = FIXTURE_ROOT / "platforms" / "swift-windows" / "main.swift"
NATIVE_ROOT = PROJECT_ROOT / ".work" / "cargo-target" / "debug"
NATIVE_DLL = NATIVE_ROOT / "blehub_uniffi_lifecycle.dll"
NATIVE_IMPORT_LIBRARY = NATIVE_ROOT / "blehub_uniffi_lifecycle.dll.lib"
OUTPUT_ROOT = PROJECT_ROOT / ".work" / "uniffi-swift-windows"
MODULE_MAP = OUTPUT_ROOT / "module.modulemap"
STAGED_HEADER = OUTPUT_ROOT / "blehub_uniffi_lifecycleFFI.h"
SMOKE_EXECUTABLE = OUTPUT_ROOT / "uniffi-lifecycle-smoke.exe"


def _require_files() -> None:
    required = [
        GENERATED_SWIFT,
        GENERATED_HEADER,
        SMOKE_SOURCE,
        NATIVE_DLL,
        NATIVE_IMPORT_LIBRARY,
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        joined = "\n".join(str(path) for path in missing)
        raise RuntimeError(f"missing UniFFI Windows Swift smoke inputs:\n{joined}")


def _stage_c_module() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    _ = shutil.copy2(GENERATED_HEADER, STAGED_HEADER)
    _ = MODULE_MAP.write_text(
        "module blehub_uniffi_lifecycleFFI {\n"
        '    header "blehub_uniffi_lifecycleFFI.h"\n'
        "    export *\n"
        "}\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    _require_files()
    _stage_c_module()
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
            "BleHubUniFfiWindowsSmoke",
            str(GENERATED_SWIFT),
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
