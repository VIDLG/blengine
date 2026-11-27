"""Shared, platform-neutral helpers for React Native RF runners."""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TextIO, final

RUNTIME_PREFIX = "BLEHUB_NITRO_RUNTIME"
DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_MULTI_RF_TIMEOUT_SECONDS = 180.0
MULTI_RF_SOAK_SECONDS_PER_CYCLE = 30.0
# Every producer CLI offers the same optional progress sink, under the same name.
JOURNAL_FLAG = "--progress-journal"
# A producer journals beside its evidence by default; a sequence names one file for all
# its scenes, so a harness polls one small file instead of tailing a log.
JOURNAL_NAME = "progress.jsonl"
_RUNTIME_MARKER = re.compile(rf"{re.escape(RUNTIME_PREFIX)} [^\r\n]*")


def multi_rf_timeout_seconds(
    timeout_seconds: float | None,
    *,
    runtime_mode: str,
    soak_cycles: int,
) -> float:
    """Return one bounded budget for a multi-RF runner invocation."""
    if runtime_mode not in {"rf-multi", "rf-multi-soak"}:
        raise ValueError(f"unsupported multi RF runtime mode: {runtime_mode}")
    if soak_cycles < 1 or soak_cycles > 20:
        raise ValueError("multi RF soak cycles must be between 1 and 20")
    if timeout_seconds is not None:
        if timeout_seconds <= 0:
            raise ValueError("multi RF timeout must be positive")
        return timeout_seconds
    if runtime_mode == "rf-multi-soak":
        return max(
            DEFAULT_MULTI_RF_TIMEOUT_SECONDS,
            MULTI_RF_SOAK_SECONDS_PER_CYCLE * soak_cycles,
        )
    return DEFAULT_MULTI_RF_TIMEOUT_SECONDS


def runtime_markers(content: str) -> list[str]:
    """Return runtime markers in log order, without forwarding unrelated logs."""
    return [match.group(0) for match in _RUNTIME_MARKER.finditer(content)]


def runtime_failure(content: str) -> str | None:
    """Return the first structured runtime failure or failure marker line."""
    for line in content.splitlines():
        _, prefix, payload = line.partition(f"{RUNTIME_PREFIX} ")
        if prefix and (":failed:" in payload or ":failure:" in payload):
            return line.strip()
    return None


def check_runtime_process(
    log_path: Path,
    runtime: subprocess.Popen[str],
    marker: str,
    progress: ProgressReporter | None,
    *,
    allow_zero_exit: bool = False,
) -> bool:
    """Check a runtime log and process without hiding a structured failure."""
    content = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
    failed_line = runtime_failure(content)
    if failed_line is not None:
        if progress is not None:
            progress.failed(f"runtime:{failed_line}")
        raise RuntimeError(
            f"RN RF gate failed before {marker!r}: {failed_line}; inspect {log_path}"
        )
    return_code = runtime.poll()
    if return_code is not None:
        if return_code == 0 and allow_zero_exit:
            return True
        if progress is not None:
            progress.failed(f"runtime-exit:{return_code}")
        raise subprocess.CalledProcessError(return_code, runtime.args)
    return False


def wait_for_runtime_log_marker(
    log_path: Path,
    marker: str,
    runtime: subprocess.Popen[str],
    timeout: float,
    occurrence: int = 1,
    *,
    progress: ProgressReporter | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for a runtime marker while failing fast on process/log failures."""
    phase = marker.removeprefix(f"{RUNTIME_PREFIX} ").strip(":")
    if progress is not None:
        progress.phase_started(phase)
    deadline = clock() + timeout
    while clock() < deadline:
        content = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
        failed_line = runtime_failure(content)
        if failed_line is not None:
            if progress is not None:
                progress.failed(f"runtime:{failed_line}")
            raise RuntimeError(
                f"RN RF gate failed before {marker!r}: {failed_line}; inspect {log_path}"
            )
        if content.count(marker) >= occurrence:
            if progress is not None:
                progress.observed(f"{marker}#{occurrence}")
                progress.phase_passed(phase, occurrence=occurrence)
            return
        _ = check_runtime_process(log_path, runtime, marker, progress)
        if progress is not None:
            progress.waiting(f"log:{marker}")
        sleeper(0.2)
    if progress is not None:
        progress.failed(f"timeout:{phase}")
    raise TimeoutError(f"timed out waiting for RN marker {marker!r} in {log_path}")


def wait_for_process_exit(
    runtime: subprocess.Popen[str],
    timeout: float,
    *,
    progress: ProgressReporter | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for a runtime controller without hiding a post-marker hang."""
    deadline = clock() + timeout
    while clock() < deadline:
        return_code = runtime.poll()
        if return_code is not None:
            if return_code != 0:
                if progress is not None:
                    progress.failed(f"runtime-exit:{return_code}")
                raise subprocess.CalledProcessError(return_code, runtime.args)
            return
        if progress is not None:
            progress.waiting("runtime-exit")
        sleeper(0.2)
    if progress is not None:
        progress.failed(f"timeout:runtime-exit:{timeout:g}s")
    raise subprocess.TimeoutExpired(runtime.args, timeout)


def _open_journal(path: Path | None) -> TextIO | None:
    if path is None:
        return None
    try:
        # The first run into a new evidence root opens the journal before the producer has
        # created that root; a journal that silently never opens is worse than none.
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("a", encoding="utf-8")
    except OSError:
        return None


def _close_quietly(journal: TextIO | None) -> None:
    if journal is None:
        return
    try:
        journal.close()
    except OSError:
        pass


@final
class ProgressReporter:
    """Emit sparse scenario progress without forwarding build or runtime logs.

    A phase boundary costs one line each way, so a caller can drive a long host run
    from a single blocking invocation instead of polling a log. `phase` reports the
    stage it owns and marks a stage that raised as failed rather than passed.

    `auto_heartbeat` adds one daemon thread that repeats the open phase while it is
    running; without it a phase that blocks for minutes looks like a hang. The
    ticker only prints, so it can never extend or abort the run.

    `journal` is a file that receives the same events as one JSON line each, for a
    harness that polls a file instead of a log. It is never inside sealed evidence.
    """

    def __init__(
        self,
        scenario: str,
        *,
        prefix: str = "rn_rf",
        stream: TextIO | None = None,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        auto_heartbeat: bool = False,
        journal: Path | None = None,
    ) -> None:
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        self.scenario: str = scenario
        self.prefix: str = prefix
        self.stream: TextIO = stream or sys.stdout
        self.heartbeat_seconds: float = heartbeat_seconds
        self.clock: Callable[[], float] = clock
        self.started_at: float = clock()
        self.next_heartbeat: float = self.started_at + heartbeat_seconds
        self.last_marker: str = "none"
        self.phase_started_at: float = self.started_at
        self.auto_heartbeat: bool = auto_heartbeat
        self._open_phases: list[str] = []
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self._ticker: threading.Thread | None = None
        # A file a harness can poll instead of a log. Like the stream it is observation, not
        # evidence: one that cannot be opened or written never decides how a run ends.
        self._journal: TextIO | None = _open_journal(journal)

    def _emit(self, **fields: object) -> None:
        details = " ".join(f"{key}={value}" for key, value in fields.items())
        with self._lock:
            self._journal_line(fields)
            try:
                print(
                    f"{self.prefix} scenario={self.scenario} {details}",
                    file=self.stream,
                    flush=True,
                )
            except (OSError, ValueError):
                # Progress is observation, not evidence: a closed or broken stream must
                # never decide whether a prepared run passes or aborts.
                return

    def _journal_line(self, fields: dict[str, object]) -> None:
        """Append one event as one JSON line; a journal that stops taking writes is dropped."""
        if self._journal is None:
            return
        try:
            self._journal.write(
                json.dumps(
                    {"scenario": self.scenario, "at": time.time(), **fields},
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
            self._journal.flush()
        except (OSError, ValueError, TypeError):
            _close_quietly(self._journal)
            self._journal = None

    def phase_started(self, phase: str, **fields: object) -> None:
        with self._lock:
            self.phase_started_at = self.clock()
        self._emit(phase=phase, status="started", **fields)

    def phase_passed(self, phase: str, **fields: object) -> None:
        with self._lock:
            elapsed = self.clock() - self.phase_started_at
        self._emit(
            phase=phase,
            status="passed",
            phase_elapsed=f"{elapsed:.0f}s",
            **fields,
        )

    def phase_failed(self, phase: str, error: BaseException) -> None:
        with self._lock:
            elapsed = self.clock() - self.phase_started_at
        self._emit(
            phase=phase,
            status="failed",
            phase_elapsed=f"{elapsed:.0f}s",
            error=f"{type(error).__name__}:{error}",
        )

    @contextlib.contextmanager
    def phase(self, phase: str, **fields: object) -> Iterator[None]:
        """Report one named stage; it never nests and never swallows the exception."""
        if type(phase) is not str or not phase:
            raise ValueError("a nonempty phase name is required")
        self.phase_started(phase, **fields)
        with self._lock:
            self._open_phases.append(phase)
        try:
            yield
        except BaseException as error:
            with self._lock:
                self._open_phases.remove(phase)
            self.phase_failed(phase, error)
            raise
        else:
            with self._lock:
                self._open_phases.remove(phase)
            self.phase_passed(phase, **fields)

    def heartbeat(self) -> None:
        """Repeat the open phase once; a closed reporter reports nothing."""
        with self._lock:
            open_phase = self._open_phases[-1] if self._open_phases else None
        if open_phase is not None:
            self.waiting(open_phase)

    def _tick(self) -> None:
        while not self._stopped.wait(self.heartbeat_seconds):
            try:
                self.heartbeat()
            except Exception:  # noqa: BLE001 -- progress output must never abort the run.
                return

    def start(self) -> ProgressReporter:
        if self._ticker is not None:
            raise RuntimeError("progress ticker is one-shot")
        self._stopped.clear()
        self._ticker = threading.Thread(
            target=self._tick, name="rn-progress", daemon=True
        )
        self._ticker.start()
        return self

    def stop(self) -> None:
        self._stopped.set()
        ticker, self._ticker = self._ticker, None
        if ticker is not None:
            ticker.join(timeout=self.heartbeat_seconds + 5)
        journal, self._journal = self._journal, None
        _close_quietly(journal)

    def __enter__(self) -> ProgressReporter:
        return self.start() if self.auto_heartbeat else self

    def __exit__(self, *interruption: object) -> None:
        self.stop()

    def observed(self, marker: str) -> None:
        self.last_marker = marker

    def marker(self, marker: str, **fields: object) -> None:
        """Emit a newly observed runtime marker immediately."""
        self.observed(marker)
        self._emit(marker=marker, **fields)

    def waiting(self, marker: str, **fields: object) -> None:
        with self._lock:
            now = self.clock()
            if now < self.next_heartbeat:
                return
            self.next_heartbeat = now + self.heartbeat_seconds
        self._emit(
            waiting=marker,
            elapsed=f"{now - self.started_at:.0f}s",
            phase_elapsed=f"{now - self.phase_started_at:.0f}s",
            last_marker=self.last_marker,
            **fields,
        )

    def failed(self, reason: str) -> None:
        self._emit(status="failed", reason=reason, last_marker=self.last_marker)


def add_journal_argument(parser: argparse.ArgumentParser) -> None:
    """Declare the one optional progress sink every producer CLI offers."""
    parser.add_argument(
        JOURNAL_FLAG,
        type=Path,
        help=(
            "append this run's progress events as JSON lines "
            f"(default: <output>/{JOURNAL_NAME})"
        ),
    )


def reporter(
    scenario: str,
    *,
    prefix: str,
    output: Path | None = None,
    journal: Path | None = None,
) -> ProgressReporter:
    """The reporter a producer CLI uses: heartbeating, and journalling where it is told.

    A producer that states its evidence root always journals: a blocking run that can only
    be watched by tailing a log is a run whose state nobody can ask for.
    """
    if journal is None and output is not None:
        journal = output / JOURNAL_NAME
    return ProgressReporter(
        scenario, prefix=prefix, auto_heartbeat=True, journal=journal
    )


@contextlib.contextmanager
def phase_scope(
    progress: ProgressReporter | None, phase: str, **fields: object
) -> Iterator[None]:
    """Report one stage when a reporter is installed, and stay silent otherwise.

    Preparation code is shared by diagnostic and formal callers, so the reporter is
    optional at the call site rather than a required parameter of every helper.
    """
    if progress is None:
        yield
        return
    with progress.phase(phase, **fields):
        yield


class ReportedObservations(list[str]):
    """A scenario's own observation journal that also narrates its boundaries.

    A scene already appends each fact it establishes, in order, as the single list it
    may claim. Reporting from that append means one seam covers every scene: the log
    says which boundary was reached and how long the previous segment took, without a
    scene carrying a second progress parameter or reporting anything it did not record.

    The value is still a plain list for evidence: JSON publication, equality against
    the expected trace and length checks all behave exactly as before.
    """

    def __init__(
        self,
        progress: ProgressReporter | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._progress = progress
        self._clock = clock
        self._last = clock()

    def append(self, observation: str) -> None:
        super().append(observation)
        if self._progress is None:
            return
        now = self._clock()
        self._progress.marker(observation, since_previous=f"{now - self._last:.1f}s")
        self._last = now
