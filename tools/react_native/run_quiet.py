"""Run a bounded command with full logs on disk and concise failure output."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType
from typing import TextIO, cast

FAILURE_TAIL_LINES = 160


def _run_without_terminal(
    command: list[str], output: TextIO, *, progress_seconds: int = 0
) -> int:
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    def stop_on_term(signum: int, _frame: FrameType | None) -> None:
        raise SystemExit(128 + signum)

    original_term = signal.signal(signal.SIGTERM, stop_on_term)
    started = time.monotonic()
    try:
        while True:
            try:
                return process.wait(timeout=progress_seconds or None)
            except subprocess.TimeoutExpired:
                print(
                    f"quiet command running: {command[0]} "
                    f"elapsed={time.monotonic() - started:.0f}s "
                    f"pid={process.pid} (full log: {output.name})",
                    flush=True,
                )
    except BaseException:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        raise
    finally:
        signal.signal(signal.SIGTERM, original_term)


def main() -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument(
        "--no-tty",
        action="store_true",
        help="close stdin and detach the command from the terminal (macOS/POSIX)",
    )
    _ = parser.add_argument(
        "--progress-seconds",
        type=int,
        default=0,
        help="emit one running-status line per interval; 0 disables (requires --no-tty)",
    )
    _ = parser.add_argument("log", type=Path)
    _ = parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    log = cast(Path, args.log)
    command = cast(list[str], args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SystemExit("a command is required after --")
    if args.no_tty and os.name != "posix":
        parser.error("--no-tty requires POSIX process groups")
    if args.progress_seconds < 0 or (args.progress_seconds and not args.no_tty):
        parser.error("--progress-seconds must be nonnegative and requires --no-tty")

    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as output:
        if args.no_tty:
            returncode = _run_without_terminal(
                command, output, progress_seconds=args.progress_seconds
            )
        else:
            returncode = subprocess.run(
                command,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            ).returncode

    if returncode != 0:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        message = (
            f"quiet command failed with exit code {returncode}; last "
            f"{min(len(lines), FAILURE_TAIL_LINES)} lines from {log}:"
        )
        print(message, file=sys.stderr)
        print("\n".join(lines[-FAILURE_TAIL_LINES:]), file=sys.stderr)
        return returncode

    print(f"quiet command passed: {command[0]} (full log: {log})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
