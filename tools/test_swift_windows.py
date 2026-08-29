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
FIXTURE_ROOT = PROJECT_ROOT / "spikes" / "boltffi-token-ingress"
GENERATED_ROOT = FIXTURE_ROOT / "generated" / "swift" / "BoltFFI"
GENERATED_SWIFT = GENERATED_ROOT / "BlehubPhase0FfiFixtureBoltFFI.swift"
GENERATED_HEADER = GENERATED_ROOT / "boltffi.h"
SMOKE_SOURCE = FIXTURE_ROOT / "platforms" / "swift-windows" / "main.swift"
NATIVE_ROOT = (
    PROJECT_ROOT / ".work" / "cargo-target" / "x86_64-pc-windows-msvc" / "debug"
)
NATIVE_DLL = NATIVE_ROOT / "blehub_phase0_ffi_fixture.dll"
NATIVE_IMPORT_LIBRARY = NATIVE_ROOT / "blehub_phase0_ffi_fixture.dll.lib"
OUTPUT_ROOT = PROJECT_ROOT / ".work" / "swift-windows"
MODULE_MAP = OUTPUT_ROOT / "module.modulemap"
STAGED_HEADER = OUTPUT_ROOT / "boltffi.h"
SMOKE_EXECUTABLE = OUTPUT_ROOT / "token-ingress-smoke.exe"


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
        raise RuntimeError(f"missing Windows Swift smoke inputs:\n{joined}")


def _stage_c_module() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    _ = shutil.copy2(GENERATED_HEADER, STAGED_HEADER)
    _ = MODULE_MAP.write_text(
        'module BleHubPhase0FfiFixtureFFI {\n    header "boltffi.h"\n    export *\n}\n',
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
            "BleHubPhase0WindowsSmoke",
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
