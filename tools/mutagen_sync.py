from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tools.local_config import PROJECT_ROOT, resolve_local_value

GITIGNORE = PROJECT_ROOT / ".gitignore"
PROJECT_FILE = PROJECT_ROOT / ".work" / "mutagen" / "mutagen.yml"
EXTRA_IGNORES = ("NUL",)


@dataclass(frozen=True)
class MutagenSettings:
    executable: str
    host: str | None
    user: str
    remote_path: str
    session_name: str


def gitignore_patterns() -> list[str]:
    patterns: list[str] = []
    for raw_line in GITIGNORE.read_text(encoding="utf-8").splitlines():
        pattern = raw_line.strip()
        if pattern and not pattern.startswith("#"):
            patterns.append(pattern)
    patterns.extend(EXTRA_IGNORES)
    return patterns


def _configured_value(
    arguments: argparse.Namespace,
    argument_name: str,
    environment_variable: str,
    default: str | None,
) -> str | None:
    explicit = getattr(arguments, argument_name)
    if explicit is not None and explicit.strip():
        return explicit.strip()
    return resolve_local_value(
        "mutagen", argument_name, environment_variable, default
    ).value


def _required_value(value: str | None, name: str) -> str:
    if value is None:
        raise RuntimeError(
            f"Mutagen {name} is not configured; set mutagen.{name} in "
            "blehub.local.toml, use the corresponding BLEHUB_MUTAGEN_* "
            "environment variable, or pass an explicit Just argument"
        )
    return value


def _resolve_executable(value: str) -> str:
    expanded = os.path.expandvars(value)
    candidate = Path(expanded).expanduser()
    if candidate.is_absolute() or "/" in expanded or "\\" in expanded:
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        resolved = candidate.resolve(strict=False)
        if not resolved.is_file():
            raise RuntimeError(f"Mutagen executable does not exist: {resolved}")
        return str(resolved)

    resolved = shutil.which(value)
    if resolved is None:
        raise RuntimeError(
            f"Mutagen executable {value!r} was not found on PATH; configure "
            "mutagen.executable in blehub.local.toml or BLEHUB_MUTAGEN_EXE"
        )
    return resolved


def resolve_settings(arguments: argparse.Namespace) -> MutagenSettings:
    executable = _required_value(
        _configured_value(arguments, "executable", "BLEHUB_MUTAGEN_EXE", "mutagen"),
        "executable",
    )
    return MutagenSettings(
        executable=_resolve_executable(executable),
        host=_configured_value(arguments, "host", "BLEHUB_MUTAGEN_HOST", None),
        user=_required_value(
            _configured_value(arguments, "user", "BLEHUB_MUTAGEN_USER", "vision"),
            "user",
        ),
        remote_path=_required_value(
            _configured_value(
                arguments,
                "remote_path",
                "BLEHUB_MUTAGEN_REMOTE_PATH",
                "~/projects/blehub",
            ),
            "remote_path",
        ),
        session_name=_required_value(
            _configured_value(
                arguments,
                "session_name",
                "BLEHUB_MUTAGEN_SESSION_NAME",
                "blehub",
            ),
            "session_name",
        ),
    )


def render_project(settings: MutagenSettings) -> str:
    host = _required_value(settings.host, "host")
    endpoint = f"{settings.user}@{host}:{settings.remote_path}"
    lines = [
        "sync:",
        "  defaults:",
        "    mode: two-way-safe",
        "    flushOnCreate: true",
        "    ignore:",
        "      vcs: true",
        "      paths:",
    ]
    lines.extend(f"        - {json.dumps(pattern)}" for pattern in gitignore_patterns())
    lines.extend(
        (
            f"  {json.dumps(settings.session_name)}:",
            f"    alpha: {json.dumps(str(PROJECT_ROOT))}",
            f"    beta: {json.dumps(endpoint)}",
        )
    )
    return "\n".join(lines) + "\n"


def write_project(settings: MutagenSettings) -> None:
    PROJECT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROJECT_FILE.write_text(render_project(settings), encoding="utf-8", newline="\n")


def project_command(action: str, settings: MutagenSettings) -> list[str]:
    return [
        settings.executable,
        "project",
        action,
        "--project-file",
        str(PROJECT_FILE),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage BleHub's Mutagen project.")
    parser.add_argument(
        "command", choices=("start", "list", "flush", "terminate", "render")
    )
    parser.add_argument("--executable")
    parser.add_argument("--host")
    parser.add_argument("--user")
    parser.add_argument("--remote-path")
    parser.add_argument("--session-name")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    settings = resolve_settings(arguments)

    if arguments.command in ("start", "render"):
        write_project(settings)
    elif not PROJECT_FILE.is_file():
        raise RuntimeError(
            f"Mutagen project file does not exist: {PROJECT_FILE}; run the start recipe"
        )

    if arguments.command == "render":
        print(PROJECT_FILE)
        return 0

    command = project_command(arguments.command, settings)
    if arguments.command == "start":
        command.append("--no-global-configuration")
    if arguments.dry_run:
        print(subprocess.list2cmdline(command))
        return 0
    subprocess.run(command, check=True, cwd=PROJECT_ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
