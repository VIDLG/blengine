"""Borrow nrftest's physical-device lock authority without importing its source."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from builtins import BaseExceptionGroup
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Self

from tools.local_config import resolve_local_value

STARTUP_TIMEOUT_SECONDS = 15
CLOSE_TIMEOUT_SECONDS = 5
READY_LINE_LIMIT = 4096


class ResourceLeaseError(RuntimeError):
    """The selected physical fixture cannot be exclusively owned or verified."""


def canonical_identity(kind: str, serial: str) -> str:
    """Match the broker's wire identity, including decimal J-Link USB aliases."""
    if kind not in {"jlink", "nrf-usb"}:
        raise ResourceLeaseError(f"unsupported physical resource kind: {kind!r}")
    if not isinstance(serial, str) or not serial or serial != serial.strip():
        raise ResourceLeaseError("physical fixture requires an explicit stable serial")
    if kind == "jlink":
        if not serial.isascii() or not serial.isdecimal() or int(serial) <= 0:
            raise ResourceLeaseError(
                "J-Link serial must be a positive ASCII decimal integer"
            )
        serial = str(int(serial))
    else:
        if not serial.isascii() or any(
            ord(char) < 33 or ord(char) == 127 for char in serial
        ):
            raise ResourceLeaseError(
                "nRF USB serial must be printable ASCII without whitespace"
            )
        serial = serial.upper()
    return f"{kind}:{serial}"


def nrftest_root() -> Path:
    """Resolve the explicitly configured checkout; never assume a sibling path."""
    configured = resolve_local_value(
        "toolchains", "nrftest_root", "BLEHUB_NRFTEST_ROOT", None
    )
    if configured.value is None:
        raise ResourceLeaseError(
            "physical fixture lease requires toolchains.nrftest_root or BLEHUB_NRFTEST_ROOT"
        )
    root = Path(configured.value).expanduser().resolve()
    if not (root / "pixi.toml").is_file():
        raise ResourceLeaseError(
            f"configured nrftest checkout is missing pixi.toml: {root}"
        )
    return root


class DeviceLease:
    """Hold a broker via a private stdin pipe; the broker releases its lock on EOF.

    Borrowers must validate this active object in the owning process. There is no
    CLI token or skip-lock mode. On POSIX the launcher starts in its own session,
    isolated from the RF process group; EOF and forced group shutdown are tested.
    Windows descendant shutdown and parent-death EOF through Pixi are unverified:
    process.kill() targets only the immediate launcher, not its broker descendants.
    This is not a verified Windows process-tree cleanup guarantee.
    """

    def __init__(
        self,
        kind: str,
        serial: str,
        *,
        purpose: str,
        nrftest_root: Path | None = None,
    ) -> None:
        self.identity = canonical_identity(kind, serial)
        self._kind = kind
        self._serial = serial
        self._purpose = purpose
        self._root = nrftest_root
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr: BinaryIO | None = None
        self._pid: int | None = None
        self._held = False

    def _command(self) -> tuple[list[str], Path]:
        root = self._root if self._root is not None else nrftest_root()
        pixi = shutil.which("pixi")
        if pixi is None:
            raise ResourceLeaseError(
                "Pixi is required for the nrftest physical lease broker"
            )
        return [
            pixi,
            "run",
            "python",
            "-m",
            "host.nrftest.resource_lease",
            "--kind",
            self._kind,
            "--serial",
            self._serial,
            "--purpose",
            self._purpose,
        ], root

    def acquire(self) -> None:
        if self._pid is not None:
            raise ResourceLeaseError("physical lease is already held or inherited")
        command, root = self._command()
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115 - owned until release, not acquire exit
        try:
            self._pid = os.getpid()
            self._process = subprocess.Popen(
                command,
                cwd=root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                start_new_session=True,
                close_fds=True,
            )
            process = self._process
            result: queue.Queue[bytes | BaseException] = queue.Queue(maxsize=1)

            def read_ready() -> None:
                try:
                    assert process.stdout is not None
                    result.put(process.stdout.readline(READY_LINE_LIMIT + 1))
                except BaseException as error:  # noqa: BLE001 - transport reader interruptions belong to the acquiring owner too
                    result.put(error)

            reader = threading.Thread(target=read_ready, daemon=True)
            reader.start()
            try:
                line = result.get(timeout=STARTUP_TIMEOUT_SECONDS)
            except queue.Empty as error:
                raise ResourceLeaseError(
                    "physical lease broker readiness timed out"
                ) from error
            if isinstance(line, BaseException) and not isinstance(line, Exception):
                raise line
            if isinstance(line, Exception):
                raise ResourceLeaseError(
                    f"physical lease broker read failed: {line}"
                ) from line
            if not line or len(line) > READY_LINE_LIMIT or not line.endswith(b"\n"):
                raise ResourceLeaseError(
                    "physical lease broker did not return a bounded ready line"
                )
            ready = json.loads(line)
            if ready != {"resource_lease": "held", "identity": self.identity}:
                raise ResourceLeaseError(
                    f"physical lease broker identity mismatch: {ready!r}"
                )
            if process.poll() is not None:
                raise ResourceLeaseError("physical lease broker exited during startup")
            self._held = True
        except BaseException as error:
            failure = (
                error
                if not isinstance(error, Exception)
                or isinstance(error, ResourceLeaseError)
                else ResourceLeaseError(f"physical lease broker failed: {error}")
            )
            try:
                self.release()
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve acquisition and release interruptions independently
                raise BaseExceptionGroup(
                    "physical lease acquisition and release failed",
                    [error, cleanup_error],
                ) from None
            if failure is error:
                raise
            raise failure from error

    def require(self, kind: str, serial: str) -> None:
        if (
            self._pid != os.getpid()
            or not self._held
            or self._process is None
            or self._process.poll() is not None
            or self.identity != canonical_identity(kind, serial)
        ):
            raise ResourceLeaseError(
                "borrowed lease is not active for the selected physical fixture"
            )

    def release(self) -> None:
        process = self._process
        if process is None:
            if self._stderr is not None:
                self._stderr.close()
                self._stderr = None
            self._pid = None
            return
        if self._pid != os.getpid():
            raise ResourceLeaseError(
                "an inherited lease cannot be released by another process"
            )
        self._held = False
        forced = False
        errors: list[BaseException] = []
        try:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except BaseException as error:  # noqa: BLE001 - reap even when sending EOF is interrupted
                    errors.append(error)
                    forced = True
            if not forced:
                try:
                    process.wait(timeout=CLOSE_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    forced = True
                except BaseException as error:  # noqa: BLE001 - interruption must still force bounded cleanup
                    errors.append(error)
                    forced = True
            if forced:
                # Kill submission and reap share one forced-shutdown budget.
                # Retain interruptions without letting one abandon this child.
                until = time.monotonic() + CLOSE_TIMEOUT_SECONDS
                while True:
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            # Windows: launcher-only; descendant cleanup is unverified.
                            process.kill()
                        break
                    except ProcessLookupError:
                        break
                    except BaseException as error:  # noqa: BLE001 - still attempt independent reaping after kill failure
                        errors.append(error)
                        if isinstance(error, Exception) or time.monotonic() >= until:
                            break
                while True:
                    remaining = until - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "physical lease forced reap deadline exceeded"
                        )
                    try:
                        process.wait(timeout=remaining)
                        break
                    except BaseException as error:
                        if isinstance(error, Exception):
                            raise
                        errors.append(error)
            detail = ""
            if self._stderr is not None:
                self._stderr.seek(0)
                detail = (
                    self._stderr.read(READY_LINE_LIMIT)
                    .decode("utf-8", errors="replace")
                    .strip()
                )
            if forced or process.returncode != 0:
                raise ResourceLeaseError(
                    f"physical lease broker exit={process.returncode}, forced={forced}: {detail}"
                )
        except BaseException as error:  # noqa: BLE001 - preserve the first failure after all cleanup
            errors.append(error)
        finally:
            exited = process.poll() is not None
            # Do not close stdout while its readiness reader can still be blocked.
            streams = (process.stdin, process.stdout if exited else None, self._stderr)
            for stream in streams:
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except BaseException as error:  # noqa: BLE001 - close the remaining streams even after interruption
                        errors.append(error)
            self._stderr = None
            if exited:
                self._process = None
                self._pid = None
        if len(errors) > 1 and any(
            not isinstance(error, Exception) for error in errors
        ):
            raise BaseExceptionGroup("physical lease release failed", errors)
        if errors:
            error = errors[0]
            for cleanup_error in errors[1:]:
                error.add_note(f"lease broker cleanup failed: {cleanup_error}")
            raise error

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, _type: object, error: BaseException | None, _tb: object) -> None:
        try:
            self.release()
        except Exception as cleanup_error:
            if error is None:
                raise
            error.add_note(f"lease broker cleanup failed: {cleanup_error}")


@contextlib.contextmanager
def owned_lease(
    kind: str, serial: str, *, purpose: str, lease: DeviceLease | None = None
) -> Iterator[DeviceLease]:
    """Validate a borrowed in-process owner or hold a new broker for the whole scope."""
    if lease is not None:
        if not isinstance(lease, DeviceLease):
            raise ResourceLeaseError(
                "borrowed physical lease must be a DeviceLease object"
            )
        lease.require(kind, serial)
        yield lease
    else:
        with DeviceLease(kind, serial, purpose=purpose) as owner:
            yield owner


@dataclass(frozen=True)
class PortIdentity:
    kind: str
    serial: str
    vid: int
    pid: int
    location: str | None


def _port_identity(port: str) -> PortIdentity:
    from serial.tools import list_ports

    matches = [entry for entry in list_ports.comports() if entry.device == port]
    if len(matches) != 1:
        raise ResourceLeaseError(
            f"expected one enumerated USB identity for serial port {port}"
        )
    entry = matches[0]
    kind = {(0x1366, 0x1061): "jlink", (0x2FE3, 0x0004): "nrf-usb"}.get(
        (entry.vid, entry.pid)
    )
    if kind is None or not entry.serial_number:
        raise ResourceLeaseError(
            "serial port has unsupported USB identity or no stable serial"
        )
    identity = canonical_identity(kind, entry.serial_number)
    return PortIdentity(
        kind, identity.split(":", 1)[1], entry.vid, entry.pid, entry.location
    )


def identity_for_port(port: str) -> str:
    """Pin the enumerated physical serial without opening or claiming a USB port."""
    selected = _port_identity(port)
    return canonical_identity(selected.kind, selected.serial)


@contextlib.contextmanager
def port_lease(
    port: str, *, purpose: str, lease: DeviceLease | None = None
) -> Iterator[DeviceLease]:
    """Select stable USB serial, then lock and revalidate before any serial open."""
    selected = _port_identity(port)
    with owned_lease(
        selected.kind, selected.serial, purpose=purpose, lease=lease
    ) as owner:
        if _port_identity(port) != selected:
            raise ResourceLeaseError(
                "USB identity changed while acquiring the physical lease"
            )
        yield owner
