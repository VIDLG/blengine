"""Run the official Tauri iOS CLI with BleHub's machine-local signing config."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from tools.local_config import resolve_local_value

PROJECT_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_ROOT = PROJECT_ROOT / "examples" / "archive" / "tauri-blehub-internal"
TEAM_PATTERN = "ABCDEFGHIJ"


def development_team() -> str:
    value = resolve_local_value(
        "apple", "development_team", "BLEHUB_APPLE_DEVELOPMENT_TEAM", None
    ).value
    if value is None or len(value) != len(TEAM_PATTERN) or not value.isalnum():
        raise RuntimeError(
            "Tauri iOS signing requires apple.development_team or "
            "BLEHUB_APPLE_DEVELOPMENT_TEAM with a 10-character Apple team ID"
        )
    return value


def _environment(require_signing: bool) -> dict[str, str]:
    environment = os.environ.copy()
    if require_signing:
        environment["APPLE_DEVELOPMENT_TEAM"] = development_team()
    return environment


def run_tauri(arguments: list[str], *, require_signing: bool) -> int:
    command = ["npm", "run", "tauri", "--", *arguments]
    return subprocess.run(
        command,
        cwd=EXAMPLE_ROOT,
        env=_environment(require_signing),
        check=False,
    ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "build"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "init":
        return run_tauri(
            ["ios", "init", "--ci", "--skip-targets-install", *args.arguments],
            require_signing=False,
        )
    require_signing = "--no-sign" not in args.arguments
    return run_tauri(
        ["ios", "build", "--ci", *args.arguments], require_signing=require_signing
    )


if __name__ == "__main__":
    raise SystemExit(main())
