"""Run Android instrumentation with push/pm install instead of Gradle UTP streaming."""

from __future__ import annotations

import argparse
import codecs
import math
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Final, TextIO, cast, final

from tools import host_command
from tools.android import adb
from tools.android_toolchain import adb_environment, select_test_device

DEFAULT_TIMEOUT_SECONDS = 300
PROGRESS_SECONDS = 15
_OUTPUT_QUEUE_SIZE = 128
_OUTPUT_CHUNK_SIZE = 4096


class AndroidInstrumentationError(RuntimeError):
    """Raised when an APK cannot be installed or instrumentation fails."""


@final
class _InstrumentationParser:
    """Validate the machine-readable completion and JUnit status from am output."""

    _STATUS_CODE: Final[re.Pattern[str]] = re.compile(
        r"^\s*INSTRUMENTATION_STATUS_CODE:\s*(-?\d+)\s*$"
    )
    _INSTRUMENTATION_CODE: Final[re.Pattern[str]] = re.compile(
        r"^\s*INSTRUMENTATION_CODE:\s*(-?\d+)\s*$"
    )
    _TEST_COUNT: Final[re.Pattern[str]] = re.compile(
        r"(?:^|\s)numtests\s*=\s*(-?\d+)(?:\s|$)"
    )

    def __init__(self) -> None:
        self._instrumentation_code: int | None = None
        self._status_codes: list[int] = []
        self._test_count: int | None = None
        self._saw_result: bool = False
        self._failure: str | None = None

    @property
    def failure(self) -> str | None:
        """Return a fail-fast failure discovered in the stream, if any."""
        return self._failure

    def feed_line(self, line: str) -> None:
        """Consume one complete output line."""
        if self._failure is not None:
            return
        stripped = line.strip()
        status_match = self._STATUS_CODE.match(stripped)
        if status_match:
            status_code = int(status_match.group(1))
            self._status_codes.append(status_code)
            if status_code < 0:
                self._failure = f"per-test instrumentation status {status_code}"
                return

        instrumentation_match = self._INSTRUMENTATION_CODE.match(stripped)
        if instrumentation_match:
            self._instrumentation_code = int(instrumentation_match.group(1))

        if stripped.startswith("INSTRUMENTATION_RESULT:"):
            self._saw_result = True

        test_count_match = self._TEST_COUNT.search(stripped)
        if test_count_match and (
            stripped.startswith(("INSTRUMENTATION_RESULT:", "INSTRUMENTATION_STATUS:"))
        ):
            self._test_count = int(test_count_match.group(1))

        if any(
            marker in stripped
            for marker in (
                "FAILURES!!!",
                "INSTRUMENTATION_FAILED:",
                "FATAL EXCEPTION",
                "Process crashed",
            )
        ):
            if "FATAL EXCEPTION" in stripped or "Process crashed" in stripped:
                self._failure = f"crash marker in instrumentation output: {stripped}"
            else:
                self._failure = f"failure marker in instrumentation output: {stripped}"

    def finish(self, returncode: int) -> None:
        """Validate a completed process, raising for incomplete or failed output."""
        if self._failure is not None:
            raise AndroidInstrumentationError(self._failure)
        if returncode != 0:
            raise AndroidInstrumentationError(
                f"instrumentation process crashed or exited with code {returncode}"
            )
        if not self._saw_result:
            raise AndroidInstrumentationError(
                "instrumentation result marker is missing"
            )
        if self._test_count is None:
            raise AndroidInstrumentationError("instrumentation test count is missing")
        if self._test_count <= 0:
            raise AndroidInstrumentationError(
                f"instrumentation reported an invalid test count: {self._test_count}"
            )
        if self._instrumentation_code is None:
            raise AndroidInstrumentationError(
                "instrumentation completion marker is missing"
            )
        if self._instrumentation_code != -1:
            raise AndroidInstrumentationError(
                f"instrumentation failed with completion code {self._instrumentation_code}"
            )
        if 0 not in self._status_codes:
            raise AndroidInstrumentationError(
                "instrumentation did not report expected per-test completion"
            )


def parse_instrumentation_output(output: str, *, returncode: int = 0) -> None:
    """Validate complete ``am instrument -r`` output without starting a process."""
    parser = _InstrumentationParser()
    for line in output.splitlines():
        parser.feed_line(line)
    parser.finish(returncode)


def _adb(serial: str, *arguments: str) -> list[str]:
    return adb.adb_command(serial, *arguments)


def _log_name(log: TextIO) -> str:
    return str(getattr(log, "name", "instrumentation log"))


def has_root(serial: str) -> bool:
    """Return whether ``adb shell su`` exposes uid 0 without changing device state."""
    result = adb.run_command(_adb(serial, "shell", "su", "-c", "id"), timeout=15)
    return (
        result.returncode == 0
        and re.search(r"uid=0(?:\(|\s|$)", result.stdout or "") is not None
    )


def install_apk(
    serial: str,
    apk: Path,
    *,
    test_only: bool,
    use_root: bool = False,
    grant: bool = False,
    log: TextIO,
    deadline: float | None = None,
    require_owner: Callable[[], None] | None = None,
    cwd: Path | None = None,
) -> None:
    """Install once; optional owner/deadline bracket every device command.

    Cleanup is limited to this invocation's attempted upload, under the same
    authority and remaining budget. A failed cleanup is never reported as success.
    """
    if deadline is not None and (
        type(deadline) not in (int, float) or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite absolute monotonic time")
    if not apk.is_file():
        raise AndroidInstrumentationError(f"missing APK: {apk}")
    remote = f"/data/local/tmp/blehub-instrumentation-{uuid.uuid4().hex}.apk"
    upload_attempted = False

    def command(
        arguments: list[str],
        *,
        timeout: float,
        label: str,
        success_marker: str | None = None,
        upload: bool = False,
    ) -> None:
        nonlocal upload_attempted

        def guard() -> None:
            if require_owner is not None:
                require_owner()
            if deadline is not None and deadline <= time.monotonic():
                raise TimeoutError("Android install deadline exceeded")

        guard()
        budget = (
            timeout if deadline is None else min(timeout, deadline - time.monotonic())
        )
        if budget <= 0:
            raise TimeoutError("Android install deadline exceeded")
        if upload:
            upload_attempted = True
        try:
            _run_logged(
                _adb(serial, *arguments),
                log=log,
                timeout=budget,
                label=label,
                success_marker=success_marker,
                cwd=cwd,
                deadline=deadline,
            )
        except BaseException as error:
            try:
                guard()
            except Exception as boundary:  # noqa: BLE001 -- preserve the primary device failure.
                error.add_note(f"Android post-install-command boundary: {boundary}")
            raise
        guard()

    def cleanup() -> None:
        if upload_attempted:
            command(
                ["shell", "rm", "-f", remote], timeout=15, label="temporary APK cleanup"
            )

    try:
        if use_root:
            for stale_package in (
                "com.android.packageinstaller",
                "com.oplus.appdetail",
            ):
                command(
                    ["shell", "am", "force-stop", stale_package],
                    timeout=15,
                    label=f"force-stop stale installer {stale_package}",
                )
        command(
            ["push", str(apk), remote],
            upload=True,
            timeout=60,
            label=f"push {apk.name}",
        )
        install_flags = "-r" + (" -g" if grant else "") + (" -t" if test_only else "")
        if use_root:
            install_arguments = [
                "shell",
                "su",
                "-c",
                f"pm install {install_flags} {remote}",
            ]
        else:
            install_arguments = ["shell", "pm", "install", "-r"]
            if grant:
                install_arguments.append("-g")
            if test_only:
                install_arguments.append("-t")
            install_arguments.append(remote)
        command(
            install_arguments,
            timeout=90,
            label=f"install {apk.name}",
            success_marker="Success",
        )
    except BaseException as error:
        try:
            cleanup()
        except Exception as cleanup_error:  # noqa: BLE001 -- preserve primary and cleanup separately.
            error.add_note(f"temporary APK cleanup failed: {cleanup_error}")
            try:
                log.write(f"temporary APK cleanup failed: {cleanup_error}\n")
                log.flush()
            except Exception as publication:  # noqa: BLE001 -- do not replace the install failure.
                error.add_note(f"Android cleanup evidence: {publication}")
        raise
    cleanup()


def _run_logged(
    command: list[str],
    *,
    log: TextIO,
    timeout: float,
    label: str,
    success_marker: str | None = None,
    cwd: Path | None = None,
    deadline: float | None = None,
) -> None:
    _ = log.write(f"$ {' '.join(command)}\n")
    _ = log.flush()
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("Android install deadline exceeded before dispatch")
    try:
        result = adb.run_command(command, cwd=cwd, timeout=timeout, check=False)
    except (host_command.HostCommandError, subprocess.SubprocessError) as error:
        partial = (
            getattr(error, "stdout", "") or getattr(error.__cause__, "stdout", "") or ""
        )
        try:
            log.write(
                partial.decode(errors="replace")
                if isinstance(partial, bytes)
                else partial
            )
            log.write(f"\n{label} failed: {error}\n")
            log.flush()
        except Exception as publication:  # noqa: BLE001 -- keep subprocess failure primary.
            error.add_note(f"Android command evidence: {publication}")
        raise
    output = result.stdout or ""
    _ = log.write(output)
    _ = log.flush()
    if result.returncode:
        raise AndroidInstrumentationError(
            f"{label} failed with exit code {result.returncode}: {output.strip()[-1000:]}"
        )
    if success_marker is not None and (
        output.splitlines().count(success_marker) != 1
        or output.splitlines()[-1:] != [success_marker]
    ):
        raise AndroidInstrumentationError(
            f"{label} did not report {success_marker!r}: {output.strip()[-1000:]}"
        )


def _read_output(
    stream: BinaryIO,
    chunks: queue.Queue[bytes | None],
    stop: threading.Event,
) -> None:
    """Drain a child pipe without relying on selectable text streams."""
    # Buffered read(size) can withhold a short status until EOF, deadlocking a
    # child that needs the host to act on that status before it can continue.
    read = getattr(stream, "read1", stream.read)
    try:
        while not stop.is_set():
            chunk = read(_OUTPUT_CHUNK_SIZE)
            if not chunk:
                break
            while not stop.is_set():
                try:
                    chunks.put(chunk, timeout=0.1)
                    break
                except queue.Full:
                    continue
    finally:
        while not stop.is_set():
            try:
                chunks.put(None, timeout=0.1)
                break
            except queue.Full:
                continue


def _run_instrumentation(
    command: list[str],
    *,
    log: TextIO,
    timeout: int,
    progress_label: str,
    cleanup: Callable[[], None] | None = None,
) -> None:
    """Run instrumentation while validating output and cleaning up on every exit."""
    _ = log.write(f"$ {' '.join(command)}\n")
    _ = log.flush()
    process = adb.start_command(command)
    assert process.stdout is not None
    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=_OUTPUT_QUEUE_SIZE)
    stop_reader = threading.Event()
    reader = threading.Thread(
        target=_read_output,
        args=(process.stdout, chunks, stop_reader),
        name="android-instrumentation-output",
        daemon=True,
    )
    reader.start()

    parser = _InstrumentationParser()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending_line = ""
    started = time.monotonic()
    last_progress = started
    reader_done = False
    failure: AndroidInstrumentationError | None = None
    returncode: int | None = None

    def consume(text: str) -> None:
        nonlocal pending_line, failure
        if not text:
            return
        _ = log.write(text)
        _ = log.flush()
        pending_line += text
        lines = pending_line.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            pending_line = lines.pop()
        else:
            pending_line = ""
        for line in lines:
            parser.feed_line(line.rstrip("\r\n"))
            if parser.failure is not None:
                failure = AndroidInstrumentationError(parser.failure)
                return

    try:
        while not reader_done and failure is None:
            now = time.monotonic()
            elapsed = now - started
            if elapsed >= timeout:
                failure = AndroidInstrumentationError(
                    f"instrumentation timed out after {timeout}s; see {_log_name(log)}"
                )
                break
            wait_for = min(0.25, timeout - elapsed)
            try:
                chunk = chunks.get(timeout=wait_for)
            except queue.Empty:
                chunk = b""
            if chunk is None:
                reader_done = True
            elif chunk:
                consume(decoder.decode(chunk))
            now = time.monotonic()
            if now - last_progress >= PROGRESS_SECONDS:
                print(
                    f"android_instrumentation={progress_label} elapsed={int(now - started)}s",
                    flush=True,
                )
                last_progress = now

        if failure is None:
            consume(decoder.decode(b"", final=True))
            if pending_line:
                parser.feed_line(pending_line)
                pending_line = ""
                if parser.failure is not None:
                    failure = AndroidInstrumentationError(parser.failure)

        if failure is None:
            try:
                returncode = process.wait(
                    timeout=max(0.1, timeout - (time.monotonic() - started))
                )
            except subprocess.TimeoutExpired:
                failure = AndroidInstrumentationError(
                    f"instrumentation timed out after {timeout}s; see {_log_name(log)}"
                )
            else:
                parser.finish(returncode)
    except AndroidInstrumentationError as error:
        failure = error
    finally:
        stop_reader.set()
        if process.poll() is None:
            try:
                host_command.stop(process)
            except (OSError, ProcessLookupError, subprocess.SubprocessError) as error:
                if failure is None:
                    failure = AndroidInstrumentationError(
                        f"could not terminate instrumentation process: {error}"
                    )
        reader.join(timeout=5)
        if reader.is_alive():
            try:
                process.stdout.close()
            except OSError:
                pass
            reader.join(timeout=1)
        else:
            process.stdout.close()
        if cleanup is not None:
            try:
                cleanup()
            except (
                OSError,
                subprocess.SubprocessError,
                AndroidInstrumentationError,
            ) as error:
                if failure is None:
                    failure = AndroidInstrumentationError(
                        f"instrumentation cleanup failed: {error}"
                    )

    if failure is not None:
        raise failure


def _package_from_component(component: str) -> str:
    """Return the exact package owned by an instrumentation component."""
    package, separator, runner = component.partition("/")
    if (
        not separator
        or not package
        or not runner
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*", package)
    ):
        raise AndroidInstrumentationError(
            f"invalid instrumentation component: {component!r}"
        )
    return package


def run(args: argparse.Namespace) -> None:
    device_mode = cast(str, args.device)
    serial = cast(str | None, args.serial)
    app_apk = cast(Path | None, args.app_apk)
    test_apk = cast(Path, args.test_apk)
    test_component = cast(str, args.test_component)
    test_class = cast(str | None, args.test_class)
    log_path = cast(Path, args.log)
    timeout = cast(int, args.timeout)
    environment = adb_environment()
    if serial:
        environment["BLEHUB_ANDROID_SERIAL"] = serial
    device = select_test_device(device_mode, environment=environment)
    test_package = _package_from_component(test_component)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        use_root = has_root(device.serial)
        print(f"android_instrumentation_device={device.serial}", flush=True)
        print(f"android_instrumentation_root={str(use_root).lower()}", flush=True)
        print(f"android_instrumentation_log={log_path}", flush=True)
        _run_logged(
            _adb(device.serial, "wait-for-device"),
            log=log,
            timeout=30,
            label="wait for device",
        )
        if app_apk is not None:
            install_apk(
                device.serial,
                app_apk,
                test_only=False,
                use_root=use_root,
                log=log,
            )
        install_apk(
            device.serial,
            test_apk,
            test_only=True,
            use_root=use_root,
            log=log,
        )
        instrument = _adb(
            device.serial,
            "shell",
            "am",
            "instrument",
            "-w",
            "-r",
        )
        if test_class:
            instrument.extend(("-e", "class", test_class))
        instrument.append(test_component)
        _run_instrumentation(
            instrument,
            log=log,
            timeout=timeout,
            progress_label=test_component,
            cleanup=lambda: _run_logged(
                _adb(device.serial, "shell", "am", "force-stop", test_package),
                log=log,
                timeout=30,
                label=f"force-stop {test_package}",
            ),
        )
    print("android_instrumentation=passed", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--device", choices=("auto", "physical", "emulator"), default="auto"
    )
    _ = parser.add_argument(
        "--serial", help="explicit adb serial; overrides automatic selection"
    )
    _ = parser.add_argument("--app-apk", type=Path)
    _ = parser.add_argument("--test-apk", type=Path, required=True)
    _ = parser.add_argument("--test-component", required=True)
    _ = parser.add_argument("--test-class")
    _ = parser.add_argument("--log", type=Path, required=True)
    _ = parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    timeout = cast(int, args.timeout)
    if timeout < 1:
        parser.error("--timeout must be positive")
    try:
        run(args)
    except (AndroidInstrumentationError, OSError, subprocess.SubprocessError) as error:
        print(f"android-instrumentation: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
