"""Continuous10 marker-to-file mapping, independent of capture and ACK protocols.

The prefix adapter replays a caller-owned full log; the line adapter keeps only
its current flow. Neither owns, normalizes, deduplicates or publishes log records.
Relay/identity validation and signal/abort ordering remain the caller's concern.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

MODES = (
    "rf",
    "rf-connect",
    "rf-gatt",
    "rf-io",
    "rf-rssi",
    "rf-notification",
    "rf-indication",
    "rf-passive-disconnect",
    "rf-cancellation",
    "rf-watchdog",
)
_RUNTIME_PREFIX = "BLEHUB_NITRO_RUNTIME "
_STARTED = r"rf-lab-suite:flow-started:index=([0-9]+):mode=([^\s:]+)"
_PASSED = r"rf-lab-suite:flow-passed:index=([0-9]+):mode=([^\s:]+)"
_FAILED = r"rf-lab-suite:flow-failed:index=([0-9]+):mode=([^\s:]+):"


class RfSuiteCoordinationError(RuntimeError):
    """A flow marker does not belong to the continuous10 index/mode mapping."""


def _validate(index: int, mode: str, event: str) -> None:
    if index >= len(MODES) or MODES[index] != mode:
        raise RfSuiteCoordinationError(
            f"unexpected suite flow {event} index={index} mode={mode}"
        )


def _directory(root: Path, index: int, mode: str) -> Path:
    return root / f"flow-{index}-{mode}"


def _step_files(mode: str, text: str) -> Iterator[str]:
    if mode in ("rf-notification", "rf-indication"):
        label = "indication" if mode == "rf-indication" else "notification"
        if f"rf:step:{label}-enable:passed:" in text:
            yield "subscription-enabled"
        if f"rf:step:{label}-disable:passed:" in text:
            yield "subscription-disabled"
    elif mode == "rf-passive-disconnect":
        if "rf:step:notification-enable:passed:" in text:
            yield "subscription-enabled"
        if "rf:step:notification-value:passed:" in text:
            yield "connected"


def _ready(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ready\n", encoding="ascii")


def advance_prefix(log: str, root: Path) -> None:
    """Replay a full runtime-prefixed log, retaining the legacy ready-file schema.

    Steps belong to the segment from a start through the next start. Terminal
    markers are validated independently, after all start/step files, with passes
    before failures. Repeated prefixes/records are deliberately not deduplicated.
    The caller owns byte-prefix continuity, decoding and complete-line framing.
    """
    starts = list(re.finditer(_RUNTIME_PREFIX + _STARTED, log))
    for position, match in enumerate(starts):
        index, mode = int(match[1]), match[2]
        _validate(index, mode, "start")
        end = starts[position + 1].start() if position + 1 < len(starts) else len(log)
        directory = _directory(root, index, mode)
        _ready(directory / "started")
        for name in _step_files(mode, log[match.start() : end]):
            _ready(directory / name)

    for pattern, event, name in (
        (_PASSED, "pass", "done"),
        (_FAILED, "failure", "abort"),
    ):
        for match in re.finditer(_RUNTIME_PREFIX + pattern, log):
            index, mode = int(match[1]), match[2]
            _validate(index, mode, event)
            _ready(_directory(root, index, mode) / name)


class LineCoordination:
    """Map caller-framed iOS console lines to empty marker files.

    Unlike prefix replay, terminals only belong to the current started flow.
    This adapter does not require the runtime prefix, matching the existing iOS
    console boundary. It has no ACK state, timers or log accumulation.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._active: tuple[int, str] | None = None

    def observe_line(self, line: str) -> None:
        started = re.search(_STARTED, line)
        if started is not None:
            index, mode = int(started[1]), started[2]
            try:
                _validate(index, mode, "start")
            except RfSuiteCoordinationError as error:
                raise RfSuiteCoordinationError(
                    "invalid RF Lab suite flow order"
                ) from error
            self._active = index, mode
            directory = _directory(self.root, index, mode)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "started").touch()
        if self._active is None:
            return
        index, mode = self._active
        directory = _directory(self.root, index, mode)
        for name in _step_files(mode, line):
            (directory / name).touch()
        for pattern, name in ((_PASSED, "done"), (_FAILED, "abort")):
            match = re.search(pattern, line)
            if match is not None and (match[1], match[2]) == (str(index), mode):
                (directory / name).touch()
