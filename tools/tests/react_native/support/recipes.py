"""Bounded Just expansion for offline recipe wiring checks; never execute recipes."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def dry_run(
    recipe: str,
    *arguments: str,
    module: str = "react-native",
    include_dependencies: bool = False,
) -> list[list[str]]:
    """Expand recipes without executing any command, optionally including dependencies."""
    command = ["just", "--dry-run"]
    if not include_dependencies:
        command.append("--no-deps")
    result = subprocess.run(
        [*command, module, recipe, *arguments],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=True,
        timeout=15,
    )
    return [shlex.split(line) for line in result.stdout.splitlines() if line.strip()]
