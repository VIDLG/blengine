from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run cargo check and verify a specific compile-time capability gap."
    )
    parser.add_argument("--manifest-path", type=Path, required=True)
    parser.add_argument("--expected", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = [
        "cargo",
        "check",
        "--manifest-path",
        str(args.manifest_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)

    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    combined = result.stdout + result.stderr
    if result.returncode == 0:
        print(
            "Expected Cargo to reject the capability, but compilation succeeded. "
            "Convert this fixture into a positive regression test.",
            file=sys.stderr,
        )
        return 1
    if args.expected not in combined:
        print(
            f"Cargo failed for an unexpected reason; missing marker: {args.expected!r}",
            file=sys.stderr,
        )
        return 1

    print(f"Confirmed expected BoltFFI capability gap: {args.expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
