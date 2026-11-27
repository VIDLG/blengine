"""Shared Android instrumentation driving for the formal-App native lanes.

The marker protocol is one implementation; only the test class's logcat tag differs. Two
contracts sit on top of it:

- `run_instrumentation` for a scene that must finish exactly as declared (one passing test,
  an exact ordered stage vocabulary);
- `run_instrumentation_until_process_exit` for a scene the OS is expected to end mid-flight
  (permission death): the declared prefix must appear, and a clean terminal is a failure.

Neither driver is a result source. Every product claim is asserted inside the test process
or proven by host-owned state; stages only route the host's own actions.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from tools import host_command
from tools.android import adb

MAX_LOG_BYTES = 8 * 1024 * 1024
POLL_SECONDS = 0.1
# Every native scene emits a stage at each product step, and the longest declared quiet
# window is a 30 s background dwell plus one bounded pull. A minute of extra silence past
# that is a hung test process, not a slow radio: report it instead of running out the budget.
STAGE_STALL_SECONDS = 180.0
# The one formal-App instrumentation component; every native lane declares the same APK pair.
TEST_COMPONENT = "com.blehubblenghost.test/androidx.test.runner.AndroidJUnitRunner"
# The host's own logcat tag: a capture created with `host_tag=HOST_MARKER_TAG` is scoped to
# one run by a sentinel written under it, and needs no `logcat -c` to hide earlier runs.
HOST_MARKER_TAG = "BLEHUB_RN_HOST"


def _remaining(deadline: float, label: str) -> float:
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError(f"{label} exceeded the run budget")
    return budget


class MarkerCapture:
    """Live stage markers from the test process, bounded and never a result source.

    `tag` is the test class's own logcat tag: the marker protocol is one implementation
    shared by every native-instrumentation lane, and only its tag differs.

    `host_tag` scopes a capture to one run. A logcat stream starts by replaying the device's
    whole buffer for its filter, and that replay arrives in bursts with pauses, so a caller
    cannot tell history from this run by counting lines. With a host tag the capture writes
    one unique sentinel line from the host before returning: everything the stream delivers
    up to that line is history, and `stages()` reports only what came after it.
    """

    def __init__(
        self,
        serial: str,
        deadline: float,
        *,
        tag: str,
        host_tag: str | None = None,
    ) -> None:
        self._tag = tag
        self._host_tag = host_tag
        self._deadline = deadline
        self._lock = threading.Lock()
        # One `(tag, message, line)` per kept logcat line, in stream order.
        self._entries: list[tuple[str, str, str]] = []
        self._bytes = 0
        self._sentinel = f"run-{uuid.uuid4().hex}" if host_tag is not None else None
        self._bounded = host_tag is None
        filters = [
            f"{tag}:I",
            *([f"{host_tag}:I"] if host_tag is not None else []),
            "*:S",
        ]
        try:
            self._process = adb.start_command(
                adb.adb_command(serial, "logcat", "-v", "brief", "-s", *filters)
            )
        except adb.AdbError:
            raise RuntimeError("could not start the logcat marker stream") from None
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        if self._sentinel is not None:
            adb.run_command(
                adb.adb_command(serial, "shell", "log", "-t", host_tag, self._sentinel),
                timeout=_remaining(deadline, "marker sentinel"),
                check=True,
            )

    def _parse(self, line: str) -> tuple[str, str] | None:
        """The exact tag and message of one `brief` line, or `None` for any other line."""
        for tag in (self._tag, self._host_tag):
            if tag is None or tag not in line:
                continue
            # The tag must be exact: a longer tag that merely starts with it is foreign.
            match = re.search(
                re.escape(tag) + r"(?:\s*\(\s*[0-9]+\s*\))?\s*:\s*(.*)$", line
            )
            if match is not None:
                return tag, match.group(1).strip()
        return None

    def _read(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            return
        with stdout:
            for raw in stdout:
                self._keep(raw.decode("utf-8", errors="replace").rstrip("\r\n"))

    def _keep(self, line: str) -> None:
        entry = self._parse(line)
        if entry is None:
            return
        with self._lock:
            if entry == (self._host_tag, self._sentinel):
                # The boundary is known: history was only ever in the way.
                self._entries, self._bytes, self._bounded = [], 0, True
                return
            if self._bytes >= MAX_LOG_BYTES:
                return
            self._bytes += len(line) + 1
            self._entries.append((*entry, line))

    def settled(self) -> bool:
        """Has the stream delivered everything that was logged before this run?"""
        with self._lock:
            return self._bounded

    def require_baseline(self, deadline: float) -> None:
        """Block until the replay before this run's sentinel has been delivered."""
        while not self.settled():
            if time.monotonic() + POLL_SECONDS >= deadline:
                raise TimeoutError(
                    "logcat did not replay its buffer before the deadline"
                )
            time.sleep(POLL_SECONDS)

    def stages(self) -> list[str]:
        """Ordered stage markers this run logged, parsed from `brief` logcat lines only."""
        with self._lock:
            bounded, entries = self._bounded, list(self._entries)
        if not bounded:
            return []
        return [message for tag, message, _ in entries if tag == self._tag]

    def log(self) -> str:
        """The kept logcat lines verbatim, bounded, for diagnostics only."""
        with self._lock:
            return "\n".join(line for _, _, line in self._entries)

    def close(self) -> None:
        host_command.stop(self._process)
        self._reader.join(timeout=5)


def stage_name(stage: str) -> str:
    """The default stage key: the marker before its first colon.

    A lane whose stages are per-link (`subscribed:0`, `subscribed:1`) passes the identity
    key instead, so two links never collapse into one advance.
    """
    return stage.split(":", 1)[0]


def _advance_fixture(
    stages: list[str],
    advanced: set[str],
    stream: TextIO,
    on_stage: Callable[[str], None],
    advance: Callable[[], None],
    *,
    key: Callable[[str], str] = stage_name,
) -> None:
    """Apply every newly observed scene stage to the fixture coordination directory.

    The test process exits immediately after its last marker, so the final poll can race
    its exit; the caller replays this over the complete marker list afterwards.
    """
    for stage in stages:
        identity = key(stage)
        if identity in advanced:
            continue
        advanced.add(identity)
        stream.write(f"[marker] {stage}\n")
        stream.flush()
        advance()
        on_stage(identity)


def _command(
    device: str,
    arguments: dict[str, str],
    *,
    test_class: str,
    method: str,
) -> list[str]:
    command = [*adb.adb_command(device, "shell", "am", "instrument", "-w")]
    for key, value in arguments.items():
        command += ["-e", key, value]
    command += ["-e", "class", f"{test_class}#{method}", TEST_COMPONENT]
    return command


def _drive(
    process: subprocess.Popen[bytes],
    capture: MarkerCapture,
    stream: TextIO,
    *,
    on_stage: Callable[[str], None],
    advance: Callable[[], None],
    deadline: float,
    require_owner: Callable[[], None],
    stage_key: Callable[[str], str],
) -> str:
    """Poll the test process and its markers until it exits; return its console output.

    The process's console is streamed into the log as it arrives. Waiting for exit before reading
    it made a hung test invisible: the log showed only the stages the host itself had routed, so a
    test that died at stage three and a test that never printed anything looked identical.

    Silence is also bounded. Every scene emits a stage per product step, so a long gap between
    stages means the process is stuck; that is reported at [STAGE_STALL_SECONDS] with the stages
    observed so far, instead of running out the whole run budget.
    """
    advanced: set[str] = set()
    collected: list[bytes] = []
    collected_lock = threading.Lock()
    output_closed = threading.Event()

    def read_console() -> None:
        pipe = process.stdout
        if pipe is None:
            output_closed.set()
            return
        try:
            while True:
                chunk = pipe.readline()
                if not chunk:
                    break
                with collected_lock:
                    collected.append(chunk)
                stream.write(chunk.decode("utf-8", errors="replace"))
                stream.flush()
        finally:
            output_closed.set()

    reader = threading.Thread(
        target=read_console, name="android-instrumentation-console", daemon=True
    )
    reader.start()
    last_stage_at = time.monotonic()
    last_count = 0
    try:
        while process.poll() is None:
            _remaining(deadline, "instrumentation")
            require_owner()
            _advance_fixture(
                capture.stages(),
                advanced,
                stream,
                on_stage,
                advance,
                key=stage_key,
            )
            now = time.monotonic()
            if len(advanced) != last_count:
                last_count = len(advanced)
                last_stage_at = now
            elif now - last_stage_at > STAGE_STALL_SECONDS:
                names = [stage_key(stage) for stage in capture.stages()]
                raise RuntimeError(
                    f"no stage progress for {int(now - last_stage_at)}s; "
                    f"observed stages={names}"
                )
            time.sleep(POLL_SECONDS)
    finally:
        _advance_fixture(
            capture.stages(),
            advanced,
            stream,
            on_stage,
            advance,
            key=stage_key,
        )
        host_command.stop(process)
        output_closed.wait(timeout=5.0)
    with collected_lock:
        raw = b"".join(collected)
    return raw.decode("utf-8", errors="replace")


def instrument_command(
    device: str,
    arguments: dict[str, str],
    *,
    test_class: str,
    method: str,
) -> list[str]:
    """The one `am instrument` invocation shape every lane in this repository uses."""
    return _command(device, arguments, test_class=test_class, method=method)


def run_instrumentation(
    device: str,
    arguments: dict[str, str],
    *,
    test_class: str,
    method: str,
    expected_stages: tuple[str, ...],
    capture: MarkerCapture,
    on_stage: Callable[[str], None],
    advance: Callable[[], None],
    deadline: float,
    require_owner: Callable[[], None],
    log: Path,
    stage_key: Callable[[str], str] = stage_name,
) -> dict[str, Any]:
    """Run the class once, advancing the fixture on its own stage markers."""
    command = _command(device, arguments, test_class=test_class, method=method)
    # A run-scoped capture must have delivered its replay before the test process can log:
    # otherwise an earlier run's stage would advance the fixture as this run's.
    capture.require_baseline(deadline)
    started = time.monotonic()
    with log.open("x", encoding="utf-8") as stream:
        process = adb.start_command(command)
        output = _drive(
            process,
            capture,
            stream,
            on_stage=on_stage,
            advance=advance,
            deadline=deadline,
            require_owner=require_owner,
            stage_key=stage_key,
        )
    stages = capture.stages()
    names = [stage_key(stage) for stage in stages]
    # Diagnostic markers are recorded, never required; only the scene stages are contractual.
    expected = list(expected_stages)
    if [name for name in names if name in expected] != expected:
        raise RuntimeError(
            "instrumentation stages differ from the scene contract: "
            f"expected {expected!r}; observed {names!r}"
        )
    if "OK (1 test)" not in output:
        raise RuntimeError("instrumentation did not report exactly one passing test")
    if process.returncode:
        raise RuntimeError(f"instrumentation exited {process.returncode}")
    elapsed = time.monotonic() - started
    return {
        "summary": {
            "returncode": process.returncode,
            "elapsed_seconds": elapsed,
            "stages": names,
        },
        "stages": names,
        "raw_stages": stages,
    }


def run_instrumentation_until_process_exit(
    device: str,
    arguments: dict[str, str],
    *,
    test_class: str,
    method: str,
    required_stages: tuple[str, ...],
    capture: MarkerCapture,
    on_stage: Callable[[str], None],
    advance: Callable[[], None],
    deadline: float,
    require_owner: Callable[[], None],
    log: Path,
    stage_key: Callable[[str], str] = stage_name,
) -> dict[str, Any]:
    """Run a scene that must reach its prefix and then be ended by the OS.

    A clean instrumentation terminal is an explicit failure: the scene's claim is that
    the process died while its work was still pending, so "the test finished" would mean
    the host revoked nothing, or the App closed cleanly.
    """
    command = _command(device, arguments, test_class=test_class, method=method)
    capture.require_baseline(deadline)
    started = time.monotonic()
    with log.open("x", encoding="utf-8") as stream:
        process = adb.start_command(command)
        output = _drive(
            process,
            capture,
            stream,
            on_stage=on_stage,
            advance=advance,
            deadline=deadline,
            require_owner=require_owner,
            stage_key=stage_key,
        )
    stages = capture.stages()
    names = [stage_key(stage) for stage in stages]
    required = list(required_stages)
    if [name for name in names if name in required] != required:
        raise RuntimeError(
            "instrumentation stages differ from the scene contract: "
            f"expected {required!r}; observed {names!r}"
        )
    if "OK (1 test)" in output:
        raise RuntimeError(
            "the death scene finished cleanly; the process was not ended by the OS"
        )
    elapsed = time.monotonic() - started
    return {
        "summary": {
            "returncode": process.returncode,
            "elapsed_seconds": elapsed,
            "stages": names,
            "clean_terminal": False,
        },
        "stages": names,
        "raw_stages": stages,
    }
