"""Bounded read-only file/stream mechanics for host evidence and lease journals.

This module owns the *mechanics* of touching untrusted host files: symlink
refusal, size bounds, open-then-verify identity, change detection while reading,
and strict duplicate-key JSON decoding. It owns no artifact policy, no scenario
vocabulary and no publication.

It exists because live modules (``ios_radio_lease``, ``ios_radio_restoration``)
used to import eleven private helpers out of the RF evidence bundle, which made a
retired scenario runner the de-facto owner of generic file I/O.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, BinaryIO

from tools.react_native import evidence_files
from tools.react_native.evidence_files import signature

MAX_FILE_BYTES = 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024 - 1
MAX_ENTRIES = 20_000
MAX_DOCUMENT_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


class EvidenceError(ValueError):
    """Host evidence cannot support the requested integrity/structural claim."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def safe_name(name: str) -> str:
    return evidence_files.safe_name(name, require=require)


def no_links(path: Path) -> None:
    evidence_files.no_links(path, require=require)


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def open_regular(path: Path) -> BinaryIO:
    no_links(path)
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode), f"not a regular file: {path}")
    require(before.st_size <= MAX_FILE_BYTES, f"oversized file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    stream = os.fdopen(os.open(path, flags), "rb")
    if signature(os.fstat(stream.fileno())) != signature(before):
        stream.close()
        raise EvidenceError(f"file changed at open: {path}")
    return stream


def hash_stream(stream: BinaryIO, limit: int = MAX_FILE_BYTES) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(CHUNK_BYTES):
        size += len(block)
        require(size <= limit, "stream exceeds byte limit")
        digest.update(block)
    return digest.hexdigest(), size


def read_small(path: Path, limit: int = MAX_DOCUMENT_BYTES) -> bytes:
    with open_regular(path) as stream:
        before = os.fstat(stream.fileno())
        require(before.st_size <= limit, f"oversized document: {path}")
        content = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    require(
        len(content) <= limit
        and len(content) == before.st_size
        and signature(before) == signature(after) == signature(path.lstat()),
        f"document changed while reading: {path}",
    )
    return content


def recovery_json(content: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            require(key not in result, f"duplicate recovery JSON key: {key}")
            result[key] = value
        return result

    # Preserve negative zero so it cannot silently become an allowed zero count.
    def integer(value: str) -> int | float:
        return -0.0 if value == "-0" else int(value)

    def constant(value: str) -> Any:
        raise EvidenceError(f"invalid recovery JSON constant: {value}")

    document = json.loads(
        content, object_pairs_hook=unique, parse_int=integer, parse_constant=constant
    )
    require(isinstance(document, dict), "recovery marker JSON must be an object")
    return document


def walk(
    root: Path, *, exclude: frozenset[str] = frozenset()
) -> list[tuple[str, Path, os.stat_result]]:
    """Sorted bounded inventory; each entry's stat is taken without following links."""
    no_links(root)
    require(stat.S_ISDIR(root.lstat().st_mode), f"not a directory: {root}")
    result: list[tuple[str, Path, os.stat_result]] = []
    aliases: set[str] = set()
    total = 0
    entry_count = 0

    def visit(directory: Path) -> None:
        nonlocal total, entry_count
        with os.scandir(directory) as entries:
            bounded_entries = []
            for entry in entries:
                entry_count += 1
                require(entry_count <= MAX_ENTRIES, "too many entries")
                bounded_entries.append(entry)
            for entry in sorted(bounded_entries, key=lambda item: item.name):
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                if relative in exclude:
                    continue
                safe_name(relative)
                require(relative.casefold() not in aliases, f"aliased path: {relative}")
                aliases.add(relative.casefold())
                info = entry.stat(follow_symlinks=False)
                require(
                    stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode),
                    f"symlink/special entry: {relative}",
                )
                result.append((relative, path, info))
                require(len(result) <= MAX_ENTRIES, "too many entries")
                if stat.S_ISDIR(info.st_mode):
                    visit(path)
                else:
                    require(
                        info.st_size <= MAX_FILE_BYTES, f"oversized file: {relative}"
                    )
                    total += info.st_size
                    require(total <= MAX_TOTAL_BYTES, "total bytes exceed limit")

    visit(root)
    return sorted(result, key=lambda item: item[0])


# -- deadlines, identity and stable reads ------------------------------------------------


def check_deadline(deadline: float) -> None:
    """Refuse a non-finite deadline and expire a caller's absolute one."""
    require(
        type(deadline) in (int, float) and math.isfinite(deadline),
        "finite absolute deadline required",
    )
    if time.monotonic() >= deadline:
        raise TimeoutError("evidence deadline exceeded")


def identity(info: os.stat_result) -> tuple[int, ...]:
    """A file identity a same-content replacement cannot reproduce."""
    return (*signature(info), info.st_nlink)


def require_regular(info: os.stat_result, limit: int) -> None:
    require(
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
        "regular non-hardlinked file required",
    )
    require(0 <= info.st_size <= limit, "file size exceeds limit")


def open_stream(path: Path, limit: int) -> BinaryIO:
    """Open one regular file and prove it did not move at open."""
    no_links(path)
    before = path.lstat()
    require_regular(before, limit)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    stream = os.fdopen(os.open(path, flags), "rb")
    if identity(before) != identity(os.fstat(stream.fileno())):
        stream.close()
        raise EvidenceError("file changed at open")
    return stream


def read_bytes(
    path: Path,
    *,
    limit: int,
    deadline: float,
    output: BinaryIO | None = None,
) -> tuple[str, int, bytes]:
    """Deadline-aware stable-descriptor read; only small JSON reads retain bytes."""
    digest, size, blocks = hashlib.sha256(), 0, []
    with open_stream(path, limit) as stream:
        before = os.fstat(stream.fileno())
        while True:
            check_deadline(deadline)
            block = stream.read(min(CHUNK_BYTES, limit - size + 1))
            if not block:
                break
            size += len(block)
            require(size <= limit, "stream exceeds byte limit")
            digest.update(block)
            if output is not None:
                output.write(block)
            elif path.suffix == ".json":
                blocks.append(block)
        require(
            identity(before)
            == identity(os.fstat(stream.fileno()))
            == identity(path.lstat())
            and size == before.st_size,
            "file drift during read/copy",
        )
    check_deadline(deadline)
    return digest.hexdigest(), size, b"".join(blocks)


def strict_json(raw: bytes) -> dict:
    """Decode one JSON object strictly: no duplicate keys, no non-finite numbers."""

    def unique(pairs: list[tuple[str, Any]]) -> dict:
        result: dict[str, Any] = {}
        for key, value in pairs:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def number(text: str) -> float:
        value = float(text)
        require(math.isfinite(value), "invalid JSON number")
        return value

    def invalid(text: str) -> Any:
        raise EvidenceError(f"invalid JSON number: {text}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_float=number,
            parse_constant=invalid,
        )
    except (UnicodeError, RecursionError) as error:
        raise EvidenceError("invalid/deep JSON") from error
    require(type(value) is dict, "JSON object required")
    return value


def read_document(
    path: Path, deadline: float, *, limit: int = MAX_DOCUMENT_BYTES
) -> dict:
    return strict_json(read_bytes(path, limit=limit, deadline=deadline)[2])


# -- bounded tree inventory -------------------------------------------------------------


def snapshot(
    root: Path,
    deadline: float,
    *,
    allowed_directories: set[str],
    allowed_root_files: set[str],
    max_entries: int = MAX_ENTRIES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_document_bytes: int = MAX_DOCUMENT_BYTES,
    skip_files: frozenset[str] = frozenset({"manifest.json"}),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, tuple[int, ...]]]:
    """Inventory a bounded evidence tree, including empty directories and modes.

    The layout is the caller's policy; this function owns only bounded traversal,
    per-file stability and the JSON strictness of every document it walks.
    """
    no_links(root)
    before = root.lstat()
    require(stat.S_ISDIR(before.st_mode), "attempt directory required")
    files: list[dict[str, Any]] = []
    directories: list[dict[str, Any]] = []
    signatures: dict[str, tuple[int, ...]] = {
        "": (before.st_dev, before.st_ino, before.st_mode)
    }
    aliases: set[str] = set()
    count = total = 0

    def visit(directory: Path) -> None:
        nonlocal count, total
        with os.scandir(directory) as entries:
            for entry in entries:
                check_deadline(deadline)
                count += 1
                require(count <= max_entries, "too many evidence entries")
                path = Path(entry.path)
                name = safe_name(path.relative_to(root).as_posix())
                require(name.casefold() not in aliases, "aliased evidence path")
                aliases.add(name.casefold())
                info = entry.stat(follow_symlinks=False)
                top = name.split("/")[0]
                require(
                    top in allowed_directories
                    or ("/" not in name and name in allowed_root_files),
                    "unexpected evidence layout",
                )
                if stat.S_ISDIR(info.st_mode):
                    require(top in allowed_directories, "unexpected evidence directory")
                    directories.append(
                        {"path": name, "mode": stat.S_IMODE(info.st_mode)}
                    )
                    signatures[name] = identity(info)
                    visit(path)
                    require(
                        identity(info) == identity(path.lstat()),
                        "directory drift during inventory",
                    )
                else:
                    require_regular(info, max_file_bytes)
                    require(
                        top not in allowed_directories or "/" in name,
                        "owner evidence must be a directory",
                    )
                    total += info.st_size
                    require(
                        total <= max_total_bytes, "evidence total bytes exceed limit"
                    )
                    if name in skip_files:
                        continue
                    limit = (
                        min(max_file_bytes, max_document_bytes)
                        if path.suffix == ".json"
                        else max_file_bytes
                    )
                    digest, size, raw = read_bytes(path, limit=limit, deadline=deadline)
                    require(
                        identity(info) == identity(path.lstat()),
                        "file drift during inventory",
                    )
                    if path.suffix == ".json":
                        strict_json(raw)
                    files.append(
                        {
                            "path": name,
                            "size": size,
                            "sha256": digest,
                            "mode": stat.S_IMODE(info.st_mode),
                        }
                    )
                    signatures[name] = identity(info)

    visit(root)
    after = root.lstat()
    require(
        (before.st_dev, before.st_ino, before.st_mode)
        == (after.st_dev, after.st_ino, after.st_mode),
        "attempt directory replaced",
    )
    return (
        sorted(files, key=lambda row: row["path"]),
        sorted(directories, key=lambda row: row["path"]),
        signatures,
    )


def freeze_file(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    deadline: float,
    expected_name: str,
    limit: int = MAX_FILE_BYTES,
) -> None:
    """Exclusively freeze stable bytes/hash before any device admission.

    On failure only this function's own destination inode is retracted, never an old
    or substituted evidence file.
    """
    check_deadline(deadline)
    require(
        type(expected_sha256) is str and re.fullmatch(r"[0-9a-f]{64}", expected_sha256),
        "canonical artifact SHA256 required",
    )
    no_links(destination)
    require(source.suffix == ".apk", "artifact source must be an APK")
    require(destination.name == expected_name, f"{expected_name} destination required")
    owned = None
    try:
        source_signature = identity(source.lstat())
        require_regular(source.lstat(), limit)
        require(source.stat().st_size > 0, "empty artifact")
        with destination.open("xb") as output:
            owned = identity(os.fstat(output.fileno()))[:2]
            digest, size, _ = read_bytes(
                source, limit=limit, deadline=deadline, output=output
            )
        require(
            digest == expected_sha256 and size > 0, "artifact expected hash mismatch"
        )
        require(
            read_bytes(destination, limit=limit, deadline=deadline)[:2]
            == (digest, size),
            "frozen artifact drift",
        )
        require(
            read_bytes(source, limit=limit, deadline=deadline)[:2] == (digest, size)
            and identity(source.lstat()) == source_signature,
            "source artifact drift after copy",
        )
        require(
            identity(destination.lstat())[:2] == owned, "frozen artifact inode replaced"
        )
    except BaseException as error:
        if owned is not None:
            try:
                if identity(destination.lstat())[:2] == owned:
                    destination.unlink()
            except OSError as cleanup:
                error.add_note(f"frozen artifact retraction: {cleanup}")
        raise
