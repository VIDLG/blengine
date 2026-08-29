from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the workspace BoltFFI CLI from a selected project directory."
    )
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("boltffi_args", nargs=argparse.REMAINDER)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    project = (repository_root / args.project).resolve()
    boltffi_manifest = (
        repository_root.parent / "contrib" / "boltffi" / "Cargo.toml"
    ).resolve()

    if not (project / "Cargo.toml").is_file():
        raise SystemExit(f"missing Cargo.toml in BoltFFI project: {project}")
    if not boltffi_manifest.is_file():
        raise SystemExit(f"missing local BoltFFI workspace: {boltffi_manifest}")
    if not args.boltffi_args:
        raise SystemExit("at least one BoltFFI CLI argument is required")

    build = subprocess.run(
        [
            "cargo",
            "build",
            "--manifest-path",
            str(boltffi_manifest),
            "-p",
            "boltffi_cli",
        ],
        cwd=repository_root,
        check=False,
    )
    if build.returncode != 0:
        return build.returncode

    cargo_target = Path(
        os.environ.get("CARGO_TARGET_DIR", boltffi_manifest.parent / "target")
    )
    if not cargo_target.is_absolute():
        cargo_target = repository_root / cargo_target
    executable = (
        cargo_target / "debug" / ("boltffi.exe" if os.name == "nt" else "boltffi")
    )
    if not executable.is_file():
        raise SystemExit(f"missing built BoltFFI CLI: {executable}")

    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = str(
        repository_root / ".work" / "boltffi-metadata-target"
    )
    for variable in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"):
        wrapper = environment.get(variable, "")
        if "sccache" in Path(wrapper).name.lower():
            environment.pop(variable)

    # BoltFFI metadata mode changes macro output through cfg/env inputs that are not
    # safely separated from ordinary Rust artifacts by sccache's cache key. Cache the
    # CLI normally, but keep metadata artifacts in a dedicated, uncached Cargo target.
    return subprocess.run(
        [str(executable), *args.boltffi_args],
        cwd=project,
        env=environment,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
