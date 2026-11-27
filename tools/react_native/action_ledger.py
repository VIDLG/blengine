"""The run's own action ledger, as the App publishes it for a scene.

`lifecycle-evidence` is the human view and lives inside the scroll container, so a scene
may not read it: Android drops off-viewport children from the accessibility tree and a
swipe is only as accurate as input injection. The App therefore publishes the same
bounded list as one pinned node (`action-ledger`), and this module is the strict decoder.

Format (`src/actionLedger.ts` owns the encoder):

```text
omitted=0
1|start|succeeded|
2|begin-scan|succeeded|
3|notify-pull|succeeded|ended: disabled
```

Line 0 is `omitted=<n>`; every further line is `sequence|action|result|detail`, with
`detail` empty when there is none. A scene asserts the exact ordered actions it caused,
so anything ambiguous — a foreign line, a gap in the sequence, an empty action — is
drift and fails the run instead of being skipped.
"""

from __future__ import annotations

import re

HEADER = "omitted="
_OMITTED = re.compile(r"omitted=([0-9]+)")
_ACTION = re.compile(r"[a-z][a-z0-9-]*")
_RESULT = re.compile(r"[a-z][a-z0-9-]*")
_MAX_ENTRIES = 64
_MAX_DETAIL = 512


class ActionLedgerError(ValueError):
    """The ledger frame is not this contract."""


def parse(text: str) -> tuple[int, list[tuple[int, str, str, str]]]:
    """Decode one frame into `(omitted, [(sequence, action, result, detail), ...])`."""
    if type(text) is not str:
        raise ActionLedgerError("the action ledger must be text")
    lines = text.split("\n")
    header = _OMITTED.fullmatch(lines[0].strip())
    if header is None:
        raise ActionLedgerError(f"invalid action-ledger header: {lines[0][:60]!r}")
    omitted = int(header.group(1))
    entries: list[tuple[int, str, str, str]] = []
    previous = 0
    for line in lines[1:]:
        if line == "":
            raise ActionLedgerError("empty action-ledger line")
        parts = line.split("|")
        if len(parts) != 4:
            raise ActionLedgerError(f"invalid action-ledger line: {line[:80]!r}")
        sequence, action, result, detail = parts
        if not sequence.isdigit() or int(sequence) <= previous:
            raise ActionLedgerError(
                f"action-ledger sequence is not increasing: {line!r}"
            )
        if _ACTION.fullmatch(action) is None or _RESULT.fullmatch(result) is None:
            raise ActionLedgerError(f"invalid action-ledger action/result: {line!r}")
        if len(detail) > _MAX_DETAIL:
            raise ActionLedgerError("action-ledger detail is unbounded")
        previous = int(sequence)
        entries.append((previous, action, result, detail))
    if len(entries) > _MAX_ENTRIES:
        raise ActionLedgerError("action ledger exceeds its bound")
    return omitted, entries


def actions(text: str) -> list[tuple[str, str]]:
    """The ordered `(action, result)` pairs, which is what a scene asserts against."""
    return [(action, result) for _, action, result, _ in parse(text)[1]]


__all__ = ["HEADER", "ActionLedgerError", "actions", "parse"]
