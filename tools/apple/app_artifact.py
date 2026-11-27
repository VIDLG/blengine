"""Pure prebuilt App tree fingerprints, preserving the historical HIL digest."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path

_CHUNK_SIZE = 65536


def _snapshot(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def fingerprint(app: Path, *, require_current: Callable[[], None] | None = None) -> str:
    """Hash sorted relative file paths + NUL + bytes, never metadata or signatures.

    Reject links and special files; directory link counts are filesystem-managed,
    not regular-file hardlinks. Check open descriptors against their directory
    entries and recheck the whole tree after reading. This detects observed drift,
    not an atomic filesystem snapshot or a guarantee against subsequent mutation.
    The optional guard runs during traversal and before/after every bounded read;
    its exceptions propagate unchanged. It cannot preempt an in-flight filesystem
    operation, so callers must supply a local artifact, not a blocking remote FS.
    """
    digest = hashlib.sha256()

    def guard() -> None:
        if require_current is not None:
            require_current()

    def tree(*, read_content: bool) -> dict[Path, tuple[int, ...]]:
        observed: dict[Path, tuple[int, ...]] = {}

        def visit(name: str | Path, relative: Path, parent: int | None) -> None:
            guard()
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            directory = stat.S_ISDIR(before.st_mode)
            if (
                (relative == Path() and not directory)
                or (not directory and not stat.S_ISREG(before.st_mode))
                or (not directory and before.st_nlink != 1)
            ):
                raise ValueError(f"linked or nonregular App artifact: {app / relative}")
            expected = _snapshot(before)
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory:
                flags |= os.O_DIRECTORY
            fd = os.open(name, flags, dir_fd=parent)
            try:
                guard()
                if _snapshot(os.fstat(fd)) != expected:
                    raise RuntimeError(
                        f"App artifact changed before read: {app / relative}"
                    )
                observed[relative] = expected
                if directory:
                    names: list[str] = []
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            guard()
                            names.append(entry.name)
                    # Path ordering compares components, so sorted depth-first
                    # traversal preserves sorted(app.rglob("*")) file ordering.
                    for child in sorted(names):
                        visit(child, relative / child, fd)
                elif read_content:
                    digest.update(relative.as_posix().encode() + b"\0")
                    size = 0
                    while True:
                        guard()
                        chunk = os.read(fd, _CHUNK_SIZE)
                        guard()
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > before.st_size:
                            raise RuntimeError(
                                f"App artifact grew during read: {app / relative}"
                            )
                        digest.update(chunk)
                    if size != before.st_size:
                        raise RuntimeError(
                            f"App artifact shrank during read: {app / relative}"
                        )
                guard()
                if (
                    _snapshot(os.fstat(fd)) != expected
                    or _snapshot(os.stat(name, dir_fd=parent, follow_symlinks=False))
                    != expected
                ):
                    raise RuntimeError(
                        f"App artifact changed during read: {app / relative}"
                    )
            finally:
                os.close(fd)

        visit(app, Path(), None)
        return observed

    guard()
    if not app.is_dir() or app.is_symlink():
        raise ValueError("an explicit existing, unlinked prebuilt .app is required")
    before = tree(read_content=True)
    if tree(read_content=False) != before:
        raise RuntimeError(f"App artifact tree changed during fingerprint: {app}")
    return digest.hexdigest()
