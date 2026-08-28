from __future__ import annotations

import argparse
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

    command = [
        "cargo",
        "run",
        "--manifest-path",
        str(boltffi_manifest),
        "-p",
        "boltffi_cli",
        "--",
        *args.boltffi_args,
    ]
    return subprocess.run(command, cwd=project, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
