"""Shared path/stat/ZIP-directory mechanics; no artifact or scenario policy.

Callers retain their exception and limits contracts through ``require`` and the
explicit ZIP entry budget. This module neither walks evidence nor publishes it.
"""

from __future__ import annotations

import os
import struct
import unicodedata
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import BinaryIO


def safe_name(name: str, *, require: Callable[[bool, str], None]) -> str:
    require(
        isinstance(name, str) and 0 < len(name.encode("utf-8")) <= 1024,
        "empty/invalid/oversized relative path",
    )
    parts = name.split("/")
    require(
        len(parts) <= 64
        and not any(part in ("", ".", "..") for part in parts)
        and not any(ord(char) < 32 or ord(char) == 127 for char in name)
        and "\\" not in name
        and ":" not in name
        and unicodedata.normalize("NFC", name) == name
        and all(not part.endswith((".", " ")) for part in parts)
        and not PurePosixPath(name).is_absolute(),
        f"unsafe relative path: {name!r}",
    )
    return name


def no_links(path: Path, *, require: Callable[[bool, str], None]) -> None:
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), f"symlink path: {parent}")


def signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def zip_directory_bound(
    stream: BinaryIO, *, max_entries: int, require: Callable[[bool, str], None]
) -> int:
    """Bound central-directory allocation before ZipFile parses the inventory.

    No ZIP64/multi-disk containers; an APK signing block remains in the raw hash.
    The directory byte cap and EOCD rules are the existing RF bundle contract.
    """
    stream.seek(0, os.SEEK_END)
    length = stream.tell()
    stream.seek(max(0, length - 65_557))
    tail = stream.read(65_557)
    offset = tail.rfind(b"PK\x05\x06")
    require(offset >= 0 and len(tail) - offset >= 22, "bad ZIP end record")
    _, disk, directory_disk, disk_entries, entries, directory_size, _, comment = (
        struct.unpack("<4s4H2IH", tail[offset : offset + 22])
    )
    require(offset + 22 + comment == len(tail), "bad ZIP end/comment length")
    require(
        disk == directory_disk == 0
        and disk_entries == entries
        and entries <= max_entries
        and directory_size <= 16 * 1024 * 1024,
        "ZIP64/multi-disk/oversized ZIP directory is unsupported",
    )
    stream.seek(0)
    return entries
