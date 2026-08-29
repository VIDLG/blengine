from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a Gradle task and verify an expected compile-time failure."
    )
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--expected", action="append", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_dir = args.project_dir.resolve()
    wrapper = project_dir / ("gradlew.bat" if os.name == "nt" else "gradlew")
    command = [
        str(wrapper),
        "-p",
        str(project_dir),
        args.task,
        "--no-daemon",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)

    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    combined = result.stdout + result.stderr
    if result.returncode == 0:
        print(
            "Expected Gradle compilation to fail, but it succeeded. "
            "Convert this fixture into a positive regression test.",
            file=sys.stderr,
        )
        return 1

    missing = [marker for marker in args.expected if marker not in combined]
    if missing:
        print(
            f"Gradle failed for an unexpected reason; missing markers: {missing!r}",
            file=sys.stderr,
        )
        return 1

    print(f"Confirmed expected Gradle capability gap: {args.expected!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
