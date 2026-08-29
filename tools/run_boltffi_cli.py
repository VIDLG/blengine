from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from .boltffi_toolchain import verify


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
    if not (project / "Cargo.toml").is_file():
        raise SystemExit(f"missing Cargo.toml in BoltFFI project: {project}")
    if not args.boltffi_args:
        raise SystemExit("at least one BoltFFI CLI argument is required")

    try:
        executable = verify()
    except RuntimeError as error:
        raise SystemExit(str(error)) from error

    try:
        project_relative = project.relative_to(repository_root)
    except ValueError as error:
        raise SystemExit(
            f"BoltFFI project must be inside {repository_root}: {project}"
        ) from error
    target_key = (
        "root" if project_relative == Path(".") else "__".join(project_relative.parts)
    )

    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = str(
        repository_root / ".work" / "boltffi-metadata-target" / target_key
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
