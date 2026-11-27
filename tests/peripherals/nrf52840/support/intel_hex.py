"""Synthetic Intel HEX data for hardware-free diagnostic artifact tests."""

from __future__ import annotations


def record(kind: int, offset: int = 0, data: bytes = b"") -> str:
    """Encode a synthetic record with its checksum; perform no file or device I/O."""
    body = bytes([len(data), offset >> 8, offset & 255, kind]) + data
    return ":" + (body + bytes([-sum(body) & 255])).hex().upper() + "\n"
