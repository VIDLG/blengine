"""Track freshness of staged cross-language artifacts by content fingerprint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
CHUNK_SIZE = 1024 * 1024
IGNORED_DIRECTORY_NAMES = frozenset({".cxx", ".gradle", ".kotlin", "build"})

PROFILE_INPUTS: dict[str, tuple[str, ...]] = {
    "apple-production": (
        "Cargo.toml",
        "Cargo.lock",
        "crates/blehub",
        "crates/blehub-uniffi",
        "just/ffi/mod.just",
        "tools/artifact_fingerprint.py",
        "tools/apple/package_uniffi.py",
    ),
    "android-production": (
        "Cargo.toml",
        "Cargo.lock",
        "crates/blehub",
        "crates/blehub-uniffi",
        "platforms/android",
        "just/ffi/mod.just",
        "tests/ffi/config/uniffi.android.toml",
        "tests/ffi/runtime/android",
        "tools/artifact_fingerprint.py",
        "tools/build_uniffi_android.py",
    ),
}

PROFILE_MANIFESTS = {
    "apple-production": PROJECT_ROOT
    / ".work/staged/apple-production/.blehub-fingerprint.json",
    "android-production": PROJECT_ROOT
    / ".work/staged/android-maven/.blehub-fingerprint.json",
}


class FingerprintError(RuntimeError):
    """Raised when a fingerprint input or manifest is unsafe or invalid."""


def _iter_files(relative_root: str) -> list[Path]:
    root = PROJECT_ROOT / relative_root
    if root.is_symlink():
        raise FingerprintError(
            f"fingerprint input must not be a symlink: {relative_root}"
        )
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise FingerprintError(f"fingerprint input is missing: {relative_root}")

    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if any(
            part in IGNORED_DIRECTORY_NAMES for part in path.relative_to(root).parts
        ):
            continue
        if path.is_symlink():
            raise FingerprintError(f"fingerprint input contains a symlink: {path}")
        if path.is_file():
            files.append(path)
    return files


def _fingerprint(profile: str) -> tuple[str, list[str]]:
    try:
        inputs = PROFILE_INPUTS[profile]
    except KeyError as error:
        raise FingerprintError(f"unknown artifact profile: {profile}") from error

    digest = hashlib.sha256()
    relative_files: list[str] = []
    for relative_root in inputs:
        for path in _iter_files(relative_root):
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            relative_files.append(relative)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            try:
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                        digest.update(chunk)
            except OSError as error:
                raise FingerprintError(
                    f"cannot read fingerprint input {path}: {error}"
                ) from error
            digest.update(b"\0")
    return digest.hexdigest(), relative_files


def _manifest_path(profile: str) -> Path:
    try:
        return PROFILE_MANIFESTS[profile]
    except KeyError as error:
        raise FingerprintError(f"unknown artifact profile: {profile}") from error


def _expected_manifest(profile: str) -> dict[str, Any]:
    fingerprint, files = _fingerprint(profile)
    return {
        "schema": SCHEMA_VERSION,
        "profile": profile,
        "fingerprint": fingerprint,
        "inputs": files,
    }


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def check(profile: str) -> int:
    expected = _expected_manifest(profile)
    actual = _read_manifest(_manifest_path(profile))
    if actual != expected:
        print(f"artifact_fingerprint={profile}:stale", flush=True)
        return 1
    print(f"artifact_fingerprint={profile}:fresh", flush=True)
    return 0


def write(profile: str) -> int:
    manifest = _expected_manifest(profile)
    destination = _manifest_path(profile)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"artifact_fingerprint={profile}:written", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "write"))
    parser.add_argument("profile", choices=tuple(PROFILE_INPUTS))
    arguments = parser.parse_args()
    try:
        return (
            check(arguments.profile)
            if arguments.action == "check"
            else write(arguments.profile)
        )
    except FingerprintError as error:
        print(f"artifact_fingerprint_error={error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
