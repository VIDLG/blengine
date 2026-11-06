"""Bounded reader for the iOS startup-off native journal, not a gate verdict.

AdapterMarkerJournal in AdapterStartupGate.swift owns the envelope and limits.
Keep the downloaded bytes unchanged; runtime_text is a separate LF projection.
This reader accepts partial/failed journals for diagnosis. Scenario semantics,
Settings control, process provenance and physical/RF evidence belong elsewhere.
"""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from tools.react_native.rf_evidence_bundle import (
    RAW_PREFIX,
    EvidenceError,
    _recovery_json,
    _require,
)

PREFIX = RAW_PREFIX + "rf-adapter-transition:"
MAX_RECORDS = 256
MAX_BYTES = 1024 * 1024
MAX_LINE_BYTES = 32768
MAX_ERROR_BYTES = 128


def _uuid(value: str) -> None:
    try:
        valid = (
            type(value) is str and len(value) == 36 and str(uuid.UUID(value)) == value
        )
    except ValueError:
        valid = False
    _require(valid, "canonical startup-off run_id required")


def _text(value: object, maximum: int, label: str) -> str:
    _require(type(value) is str and bool(value), f"{label} must be nonempty text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as error:
        raise EvidenceError(f"{label} must be valid UTF-8") from error
    _require(size <= maximum, f"{label} exceeds UTF-8 byte limit")
    _require(
        not any(unicodedata.category(char) == "Cc" for char in value),
        f"{label} contains control characters",
    )
    return value


@dataclass(frozen=True)
class AdapterStartupJournal:
    """Validated envelope with original bytes, including any persisted failure."""

    raw: bytes
    run_id: str
    records: tuple[str, ...]
    error: str | None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    @property
    def runtime_text(self) -> str:
        """Lossless record projection, not the original JSON file's byte hash."""
        return "".join(record + "\n" for record in self.records)


def parse_journal(raw: bytes, run_id: str) -> AdapterStartupJournal:
    """Validate one complete native envelope for an explicit canonical attempt.

    Reject malformed UTF-8/JSON, duplicate keys, unknown fields, foreign attempts,
    overflow and ambiguous marker lines. No records are repaired or discarded.
    A non-null error is preserved, never interpreted as a successful transcript.
    """
    _uuid(run_id)
    _require(
        type(raw) is bytes and len(raw) <= MAX_BYTES,
        "journal bytes exceed limit or wrong type",
    )
    try:
        document = _recovery_json(raw.decode("utf-8"))
    except (ValueError, TypeError, RecursionError) as error:
        raise EvidenceError(f"invalid startup-off journal JSON: {error}") from error
    _require(
        set(document) == {"schema_version", "run_id", "records", "error"},
        "journal exact envelope schema required",
    )
    _require(
        type(document["schema_version"]) is int and document["schema_version"] == 1,
        "journal schema_version must be integer 1",
    )
    _require(document["run_id"] == run_id, "foreign journal run_id")
    records = document["records"]
    _require(
        type(records) is list and len(records) <= MAX_RECORDS,
        "journal record count/type limit",
    )
    for record in records:
        _text(record, MAX_LINE_BYTES, "journal record")
        _require(
            record.startswith(PREFIX)
            and len(record) > len(PREFIX)
            and record.count(RAW_PREFIX) == 1,
            "foreign or ambiguous journal marker prefix",
        )
    error = document["error"]
    if error is not None:
        _text(error, MAX_ERROR_BYTES, "journal error")
    return AdapterStartupJournal(
        raw=raw, run_id=run_id, records=tuple(records), error=error
    )


def read_journal(path: Path, run_id: str) -> AdapterStartupJournal:
    """Read at most the native byte budget plus one overflow byte from a file."""
    _uuid(run_id)
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    return parse_journal(raw, run_id)
