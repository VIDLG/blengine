"""Narrow POSIX process/transport mechanics for an explicit Pods refresh.

No CLI, build policy, installation, receipt, or refresh owner lives here. Callers
supply commands, working directories, environments and budgets. Commands must not
escape the owned session/group (setsid/setpgid daemons are not supported). Callers
must not reap these children or install an auto-reaping SIGCHLD policy.
"""

from __future__ import annotations

import builtins
import math
import os
import select
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_LOG_BYTES = 256 * 1024 * 1024
TERM_GRACE_SECONDS = 0.25
KILL_WAIT_SECONDS = 2.0
POLL_SECONDS = 0.025


class PodRefreshError(RuntimeError):
    """Process, output, transport-policy, or owned-group retirement failure.

    Messages deliberately exclude command arguments, environment and child output.
    """


class _ExitWatch:
    """Observe termination without reaping: keep the owned PGID reserved."""

    def __init__(self) -> None:
        if os.name != "posix":
            raise PodRefreshError("Pods refresh requires POSIX process groups")
        self.queue = select.kqueue() if hasattr(select, "kqueue") else None
        if self.queue is None and not all(
            hasattr(os, name) for name in ("waitid", "WNOWAIT", "WEXITED")
        ):
            raise PodRefreshError("non-reaping child exit observation unavailable")

    def register(self, pid: int) -> None:
        if self.queue is not None:
            self.queue.control(
                [
                    select.kevent(
                        pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                        fflags=select.KQ_NOTE_EXIT,
                    )
                ],
                0,
                0,
            )

    def exited(self, pid: int) -> bool:
        if self.queue is not None:
            return bool(self.queue.control(None, 1, 0))
        return (
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        )

    def close(self) -> None:
        if self.queue is not None:
            self.queue.close()


def _signal_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _retire(process: subprocess.Popen[bytes]) -> None:
    # Do not poll()/wait() before both signals: even an exited leader reserves its
    # PID/PGID until reaped. No discovery, adoption or signals after reaping.
    try:
        _signal_group(process.pid, signal.SIGTERM)
    except OSError:
        # Darwin may report EPERM for a zombie-only group. Only confirmed
        # disappearance below can make a signalling failure harmless.
        pass
    finally:
        try:
            time.sleep(TERM_GRACE_SECONDS)
        finally:
            try:
                _signal_group(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait(timeout=KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                raise PodRefreshError(
                    "owned refresh child retirement timed out"
                ) from None
    deadline = time.monotonic() + KILL_WAIT_SECONDS
    while True:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            # Orphan zombies may await the system reaper briefly (Darwin EPERM).
            # Still no further signals after reaping our group leader.
            pass
        if time.monotonic() >= deadline:
            raise PodRefreshError(
                "owned refresh group retirement could not be confirmed"
            )
        time.sleep(POLL_SECONDS)


def _sanitize_failure(error: BaseException, *, cleanup: bool) -> BaseException:
    """Convert implementation failures to stable, secret-free failure leaves."""
    if isinstance(error, PodRefreshError):
        return error
    if not cleanup and isinstance(error, KeyboardInterrupt):
        return error
    if cleanup:
        return PodRefreshError("refresh cleanup failed")
    if isinstance(error, (OSError, subprocess.SubprocessError)):
        return PodRefreshError("refresh process or output I/O failed")
    return PodRefreshError("refresh process failed")


def _failure_group(failures: list[BaseException]) -> BaseException | None:
    if not failures:
        return None
    if len(failures) == 1:
        return failures[0]
    # BaseExceptionGroup automatically narrows to ExceptionGroup when all leaves
    # are ordinary Exceptions. KeyboardInterrupt therefore remains inspectable.
    return builtins.BaseExceptionGroup("refresh operation failed", failures)


def _close_process(
    process: subprocess.Popen[bytes], *, retire: bool
) -> list[BaseException]:
    failures: list[BaseException] = []
    if retire:
        try:
            _retire(process)
        except BaseException as error:  # noqa: BLE001
            failures.append(_sanitize_failure(error, cleanup=True))
    for stream in (process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except BaseException as error:  # noqa: BLE001
            failures.append(_sanitize_failure(error, cleanup=True))
    return failures


def _budget(timeout: float, deadline: float | None) -> float:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if deadline is not None and not math.isfinite(deadline):
        raise ValueError("deadline must be finite")
    end = time.monotonic() + timeout
    if deadline is not None:
        end = min(end, deadline)
    if end <= time.monotonic():
        raise PodRefreshError("refresh deadline expired before submission")
    return end


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    end: float,
    environment: Mapping[str, str] | None,
    log: BinaryIO | None = None,
) -> bytes:
    if not command or isinstance(command, (str, bytes)):
        raise ValueError("command must be a nonempty argument sequence")
    captured = bytearray()
    total = 0
    process = None
    retirement_attempted = False
    failures: list[BaseException] = []
    result: bytes | None = None
    watch = _ExitWatch()
    try:
        try:
            with selectors.DefaultSelector() as selector:
                if time.monotonic() >= end:
                    raise PodRefreshError("refresh deadline expired before submission")
                process = subprocess.Popen(
                    list(command),
                    cwd=cwd,
                    env=None if environment is None else dict(environment),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    close_fds=True,
                )
                watch.register(process.pid)
                for stream in (process.stdout, process.stderr):
                    assert stream is not None
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                exited = False
                while selector.get_map() or not exited:
                    if time.monotonic() >= end:
                        raise PodRefreshError("refresh process deadline exceeded")
                    if not exited:
                        exited = watch.exited(process.pid)
                        if exited:
                            # Descendants may retain pipes or close them and keep
                            # running. Retire the group independently of pipe EOF.
                            cleanup_started = time.monotonic()
                            retirement_attempted = True
                            _retire(process)
                            end += time.monotonic() - cleanup_started
                    ready = selector.select(
                        min(POLL_SECONDS, max(0, end - time.monotonic()))
                    )
                    for key, _ in ready:
                        chunk = os.read(key.fd, 64 * 1024)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        maximum = MAX_SNAPSHOT_BYTES if log is None else MAX_LOG_BYTES
                        if total > maximum:
                            raise PodRefreshError("refresh output limit exceeded")
                        if log is None:
                            # stderr consumes the same bound but never contaminates JSON.
                            if key.fileobj is process.stdout:
                                captured.extend(chunk)
                        elif log.write(chunk) != len(chunk):
                            raise PodRefreshError("refresh log write failed")
                if log is not None:
                    log.flush()
                if process.returncode != 0:
                    raise PodRefreshError(
                        "refresh process failed (nonzero exit status)"
                    )
                result = bytes(captured)
        except BaseException as error:  # noqa: BLE001
            failures.append(_sanitize_failure(error, cleanup=False))
    finally:
        if process is not None:
            failures.extend(_close_process(process, retire=not retirement_attempted))
        try:
            watch.close()
        except BaseException as error:  # noqa: BLE001
            failures.append(_sanitize_failure(error, cleanup=True))
    failure = _failure_group(failures)
    if failure is not None:
        raise failure from None
    assert result is not None
    return result


def run_snapshot(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str] | None = None,
    deadline: float | None = None,
) -> str:
    """Return strict UTF-8 stdout; bound stdout + stderr together to 16 MiB.

    Compatible with ios_pod_preparation's Run callback (bind environment/deadline
    with functools.partial). timeout is seconds; deadline is absolute monotonic
    time. Cleanup has a separate fixed allowance (TERM grace + two KILL waits).
    No output is printed or included in exceptions; stderr is drained/discarded.
    """
    raw = _run(
        command,
        cwd=cwd,
        end=_budget(timeout, deadline),
        environment=environment,
    )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PodRefreshError("refresh snapshot is not UTF-8") from None


def run_logged(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    log_path: Path,
    environment: Mapping[str, str] | None = None,
    deadline: float | None = None,
) -> None:
    """Stream stdout/stderr to a new private log, bounded to 256 MiB.

    Existing paths (including symlinks) are refused. The caller owns the parent
    directory and log retention/redaction: child output is raw and may be private.
    No argv/environment is logged. Cross-stream ordering is observation order.
    """
    end = _budget(timeout, deadline)
    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb", buffering=0) as log:
            _run(command, cwd=cwd, end=end, environment=environment, log=log)
    except OSError:
        raise PodRefreshError("refresh log creation or output I/O failed") from None


def _curl_arguments(arguments: Sequence[str]) -> list[str]:
    """Canonicalize only RN's single-URL GET/HEAD curl vocabulary.

    Unknown flags, abbreviations, config/next/parallel, tracing, method changes,
    protocol/time/retry controls and non-HTTPS URLs fail closed. Short clusters
    support RN's -sL, -Lo and -Iw; option values are never reparsed as options.
    """
    flags = {
        "s": "--silent",
        "S": "--show-error",
        "L": "--location",
        "I": "--head",
        "f": "--fail",
    }
    values = {"o": "--output", "A": "--user-agent", "w": "--write-out"}
    result: list[str] = []
    urls: list[str] = []
    seen: set[str] = set()
    pending = iter(arguments)

    def value_option(option: str, value: str) -> None:
        if option in seen or not value or any(ord(c) < 32 for c in value):
            raise PodRefreshError("unsupported RN curl argument")
        if option == "--write-out" and value not in {
            "%{http_code}",
            "%{url_effective}",
        }:
            raise PodRefreshError("unsupported RN curl write-out")
        seen.add(option)
        result.extend((option, value))

    for argument in pending:
        if argument in flags.values():
            result.append(argument)
        elif argument in values.values():
            value_option(argument, next(pending, ""))
        elif argument.startswith("--"):
            raise PodRefreshError("unsupported RN curl option")
        elif argument.startswith("-"):
            cluster = argument[1:]
            if not cluster:
                raise PodRefreshError("unsupported RN curl argument")
            while cluster:
                letter, cluster = cluster[0], cluster[1:]
                if letter in flags:
                    result.append(flags[letter])
                elif letter in values:
                    value_option(values[letter], cluster or next(pending, ""))
                    break
                else:
                    raise PodRefreshError("unsupported RN curl option")
        else:
            if (
                not argument.startswith("https://")
                or any(ord(c) <= 32 for c in argument)
                or "@" in argument.split("/", 3)[2]
                or not argument.split("/", 3)[2]
            ):
                raise PodRefreshError("RN curl requires a credential-free HTTPS URL")
            urls.append(argument)
    if len(urls) != 1:
        raise PodRefreshError("RN curl requires exactly one URL")
    maximum = "12" if "--head" in result else "180"
    return [
        "-q",
        *result,
        "--connect-timeout",
        "6",
        "--max-time",
        maximum,
        "--retry",
        "0",
        "--max-redirs",
        "4",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--globoff",
        "--",
        *urls,
    ]


def _exec_curl(executable: str, arguments: Sequence[str]) -> None:
    """Wrapper implementation; never emit supplied arguments or environment."""
    try:
        command = _curl_arguments(arguments)
        environment = dict(os.environ)
        # curl/libssl can otherwise write TLS secrets or QUIC logs implicitly.
        for key in ("SSLKEYLOGFILE", "QLOGDIR"):
            environment.pop(key, None)
        os.execve(executable, [executable, *command], environment)
    except (PodRefreshError, OSError, ValueError):
        sys.stderr.write("RN curl wrapper refused arguments or execution\n")
        raise SystemExit(2) from None


@contextmanager
def curl_environment(
    environment: Mapping[str, str] | None = None,
) -> Generator[dict[str, str], None, None]:
    """Yield a copied environment with a temporary bare-curl PATH wrapper.

    Resolve the actual curl *before* prepending the private directory; use its
    absolute real path on every invocation. Absolute-path CocoaPods downloader
    calls bypass this RN-only shim. Keep this context alive through process/group
    retirement. No global environment, curlrc, user file or third-party edit.
    This is transport bounding, not a sandbox for untrusted Podfiles.
    """
    effective = dict(os.environ if environment is None else environment)
    search_path = effective.get("PATH", os.defpath)
    executable = shutil.which("curl", path=search_path)
    if executable is None:
        raise PodRefreshError("local curl executable unavailable")
    executable = str(Path(executable).resolve(strict=True))
    module = str(Path(__file__).resolve())
    # A fixed Python interpreter avoids dependence on the prepended PATH, and
    # run_path avoids importing another module/owner or relying on the child's cwd.
    with tempfile.TemporaryDirectory(prefix="blehub-rn-curl-") as directory:
        wrapper = Path(directory) / "curl"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import runpy, sys\n"
            f"runpy.run_path({module!r})['_exec_curl']({executable!r}, sys.argv[1:])\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        effective["PATH"] = directory + os.pathsep + search_path
        yield effective
