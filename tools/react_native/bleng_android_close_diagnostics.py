"""One opt-in log-tag scope; serialized by the managed owner, never phone release.

Construct only when the runner's close_diagnostics is exactly True. Log timestamps
and the original candidate PID are diagnostics, not session authority, Close
completion or an RF verdict. Checks bracket commands; they are not OS attestation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path

from tools.android.state_control import AndroidRadioController

from . import android_sdk
from .capture_io import publish_new_json
from .evidence_files import signature

_TAG = "log.tag.BleHubBlengClose"
_LEVELS = {
    "",
    "V",
    "D",
    "I",
    "W",
    "E",
    "F",
    "A",
    "S",
    "VERBOSE",
    "DEBUG",
    "INFO",
    "WARN",
    "ERROR",
    "FATAL",
    "ASSERT",
    "SILENT",
    "SUPPRESS",
}
_LIMIT = 2 * 1024 * 1024
_LIVE_LIMIT = 8 * 1024 * 1024
LIVE_LOGCAT_TAGS = (
    "appium:I",
    "BleHubBlengClose:D",
    "ReactNativeJS:V",
    "ActivityManager:I",
    "ActivityTaskManager:I",
    "AndroidRuntime:E",
    "am_anr:I",
    "am_crash:I",
    "am_proc_died:I",
    "SurfaceMountingManager:V",
    "*:S",
)
_LIVE_LIMITATION = (
    "Host arrival of merged stdout/stderr bytes only; not request arrival, health or Close proof. "
    "No output is not evidence of health. Tag-filtered system/RN diagnostics may include "
    "other processes; no unrestricted App payload collection. Log timestamps are not host arrival."
)


class AndroidCloseDiagnostics:
    """Own only this run's fixed property; output's parent must already exist.

    prepare is one-shot. restore uses its own absolute cleanup deadline, including
    after failed prepare; repeats only revalidate. require_restored rechecks pinned
    evidence and never clears original mutation/completion/evidence failures.
    """

    def __init__(
        self,
        phone: AndroidRadioController,
        run_id: str,
        output: Path,
        cwd: Path,
        deadline: float,
    ) -> None:
        if type(phone) is not AndroidRadioController or phone.run_id != run_id:
            raise ValueError("same-run actual Android phone controller required")
        self._phone, self._run_id, self._device = phone, run_id, phone.device
        self._output, self._cwd, self._deadline = output.absolute(), cwd, deadline
        self._root = phone.path.absolute().parent
        if self._root.name != run_id or not self._output.is_relative_to(self._root):
            raise ValueError("diagnostics must be inside the phone's managed run")
        self._records: dict[Path, tuple] = {}
        self._failure: BaseException | None = None
        self._integrity: BaseException | None = None
        self._before: str | None = None
        self._prepared = self._ready = self._restore_attempted = self._restored = False
        self._selector: tuple | None = None
        self._selector_raw: bytes | None = None
        self._preclose_attempted = False
        self._sequence = 0
        self._live_attempted = self._live_stopped = False
        self._live_process: subprocess.Popen[bytes] | None = None
        self._live_reader: threading.Thread | None = None
        self._live_stop = threading.Event()
        self._live_data = bytearray()
        self._live_record: dict = {}
        self._live_arrivals: deque[dict] = deque(maxlen=64)
        self._live_error: BaseException | None = None
        self._phone_guard(deadline)
        self._no_links(self._output)
        self._output.mkdir(mode=0o700)  # Exclusive; platform owner creates parent.
        self._directories = {
            p: (p.stat().st_dev, p.stat().st_ino)
            for p in (self._output, *self._output.parents)
        }

    @staticmethod
    def _budget(deadline: float) -> float:
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("finite absolute monotonic deadline required")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Android Close diagnostics deadline exceeded")
        return min(5.0, remaining)

    @staticmethod
    def _no_links(path: Path) -> None:
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise RuntimeError("linked diagnostics path refused")

    def _snapshot(self, path: Path, maximum: int = 16 * _LIMIT) -> tuple[bytes, tuple]:
        self._no_links(path)
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > maximum
        ):
            raise RuntimeError("linked/nonregular/oversized diagnostics evidence")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(maximum + 1)
            finished = os.fstat(stream.fileno())
        after = path.lstat()
        if (
            len(
                {
                    signature(v) + (v.st_nlink,)
                    for v in (before, opened, finished, after)
                }
            )
            != 1
            or len(raw) != before.st_size
        ):
            raise RuntimeError("diagnostics evidence changed during read")
        return raw, (before.st_dev, before.st_ino, hashlib.sha256(raw).hexdigest())

    def _phone_guard(self, deadline: float) -> None:
        self._budget(deadline)
        phone = self._phone
        if (
            type(phone) is not AndroidRadioController
            or phone.device != self._device
            or phone.run_id != self._run_id
            or phone.document.get("device") != self._device
            or phone.document.get("run_id") != self._run_id
            or phone.document.get("actions") != []
            or phone.initial_state is not None
            or phone.last_state is not None
        ):
            raise RuntimeError("Android Close phone/run scope changed")
        phone.require_owner()
        self._budget(deadline)

    def _directory(self) -> None:
        self._no_links(self._output)
        if any(
            not stat.S_ISDIR(p.lstat().st_mode)
            or (p.stat().st_dev, p.stat().st_ino) != pin
            for p, pin in self._directories.items()
        ):
            raise RuntimeError("diagnostics evidence directory changed")

    def _guard(
        self, deadline: float, *, cleanup: bool = False, selector: tuple | None = None
    ) -> None:
        self._phone_guard(deadline)
        try:
            self._directory()
        except Exception as error:
            self._integrity = self._integrity or error
            raise
        try:
            for path, pin in self._records.items():
                if self._snapshot(path)[1] != pin:
                    raise RuntimeError("diagnostics receipt changed")
            if self._integrity is not None:
                raise RuntimeError("diagnostics evidence permanently invalidated")
        except Exception as error:
            self._integrity = self._integrity or error
            if not cleanup:
                raise
        if (
            selector is not None
            and self._snapshot(selector[0], 65536)[1] != selector[1]
        ):
            raise RuntimeError("diagnostic candidate identity changed")
        self._phone_guard(deadline)

    def _publish(self, name: str, record: dict) -> Path:
        self._directory()
        path, committed = self._output / name, []
        try:
            digest = publish_new_json(path, record, on_commit=committed.append)
            _, pin = self._snapshot(path)
            if committed != [pin[:2]] or pin[2] != digest:
                raise RuntimeError("diagnostics publication changed")
            self._records[path] = pin
        except BaseException as error:
            self._integrity = self._integrity or error
            raise
        return path

    def _command(
        self,
        deadline: float,
        *args: str,
        cleanup: bool = False,
        selector: tuple | None = None,
    ) -> tuple[str, Path]:
        guard = lambda: self._guard(deadline, cleanup=cleanup, selector=selector)
        guard()
        argv = android_sdk.adb_command(self._device, *args)
        guard()
        self._sequence += 1
        stem = f"{self._sequence:03d}"
        record = {
            "argv": argv,
            "run_id": self._run_id,
            "stdout": "",
            "returncode": None,
            "error": None,
        }
        if selector is not None:
            record["candidate_identity"] = {
                "path": str(selector[0]),
                "pin": selector[1],
            }
        if args[:2] == ("shell", "setprop"):
            self._publish(
                stem + "-intent.json", record | {"original_value": self._before}
            )
            guard()
        primary: BaseException | None = None
        try:
            result = android_sdk.run_command(
                argv, cwd=self._cwd, timeout=self._budget(deadline), check=False
            )
            record["returncode"] = result.returncode
            raw = result.stdout.encode("utf-8")
            record["stdout"] = raw[:_LIMIT].decode("utf-8", errors="ignore")
            record["stdout_bytes"] = len(raw)
            if result.returncode or len(raw) > _LIMIT:
                raise RuntimeError("failed/oversized Close diagnostics SDK output")
        except BaseException as error:  # noqa: BLE001 -- preserve SDK failure and partial transport output.
            primary = error
            partial = getattr(error, "stdout", None) or getattr(
                error.__cause__, "stdout", None
            )
            if partial is not None:
                raw = partial if isinstance(partial, bytes) else partial.encode("utf-8")
                record["stdout"] = raw[:_LIMIT].decode("utf-8", errors="ignore")
        try:
            guard()
        except BaseException as error:  # noqa: BLE001 -- post-command scope failure must not hide SDK failure.
            if primary is None:
                primary = error
            else:
                primary.add_note(f"Close diagnostics post-command boundary: {error}")
        if primary is not None:
            record["error"] = {
                "type": type(primary).__name__,
                "message": str(primary),
                "cause": repr(primary.__cause__),
                "notes": getattr(primary, "__notes__", []),
            }
        try:
            receipt = self._publish(stem + "-command.json", record)
            guard()
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note(f"Close diagnostics receipt publication: {error}")
        if primary is not None:
            raise primary
        return result.stdout, receipt

    def _value(self, deadline: float, *, cleanup: bool = False) -> str:
        raw, _ = self._command(deadline, "shell", "getprop", _TAG, cleanup=cleanup)
        value = raw.removesuffix("\n")
        if value not in _LEVELS:
            raise ValueError("unknown Android log level refused")
        return value

    def prepare(self) -> None:
        if self._prepared:
            raise RuntimeError("Close diagnostics prepare is one-shot")
        self._prepared = True
        try:
            self._before = self._value(self._deadline)
            self._command(self._deadline, "shell", "setprop", _TAG, "DEBUG")
            if self._value(self._deadline) != "DEBUG":
                raise RuntimeError("Close diagnostics DEBUG readback mismatch")
            self._guard(self._deadline)
            self._ready = True
        except BaseException as error:
            self._failure = error
            raise

    def restore(self, deadline: float) -> None:
        if self._restore_attempted:
            self.require_restored()
            return
        self._restore_attempted, self._cleanup_deadline = True, deadline
        try:
            current = self._value(deadline, cleanup=True)
            if self._before is None or current not in {self._before, "DEBUG"}:
                raise RuntimeError("unknown/unowned Close property; refusing overwrite")
            # adb shell joins arguments: a literal empty argument needs shell quotes.
            self._command(
                deadline, "shell", "setprop", _TAG, self._before or '""', cleanup=True
            )
            if self._value(deadline, cleanup=True) != self._before:
                raise RuntimeError("Close diagnostics restoration readback mismatch")
            self._publish(
                "restored.json",
                {
                    "run_id": self._run_id,
                    "value": self._before,
                    "verified": self._failure is None and self._integrity is None,
                },
            )
            self._guard(deadline, cleanup=True)
            self._restored = True
        except BaseException as error:
            self._failure = self._failure or error
            raise

    def require_restored(self) -> None:
        if self._live_attempted and not self._live_stopped:
            raise RuntimeError("Close diagnostics local capture not stopped")
        if not self._restored:
            raise RuntimeError("Close diagnostics restoration not completed")
        self._guard(self._cleanup_deadline)
        if self._failure is not None or self._integrity is not None:
            raise RuntimeError("Close diagnostics restoration remains uncertain") from (
                self._failure or self._integrity
            )

    def _capture_selector(self, candidate_identity: Path, deadline: float) -> dict:
        self._guard(deadline)
        if candidate_identity != self._root / "candidate/bound-identity.json":
            raise ValueError(
                "same-run controlled candidate/bound-identity.json required"
            )
        if not self._ready or self._restore_attempted or deadline > self._deadline:
            raise RuntimeError("capture outside prepared scenario scope")
        raw, pin = self._snapshot(candidate_identity, 65536)
        if self._selector is not None and self._selector != (candidate_identity, pin):
            raise RuntimeError("original diagnostic candidate identity changed")
        value = json.loads(raw)
        if (
            type(value) is not dict
            or value.get("serial") != self._device
            or type(value.get("pid")) is not int
            or not 0 < value["pid"] <= 2147483647
        ):
            raise ValueError("invalid diagnostic candidate phone/PID selector")
        if self._selector_raw is not None and raw != self._selector_raw:
            raise RuntimeError("original diagnostic candidate bytes changed")
        self._selector, self._selector_raw = (candidate_identity, pin), raw
        return value

    def capture(self, candidate_identity: Path, *, deadline: float) -> Path:
        """Read original-PID logs only; never infer Close success or refresh identity."""
        value = self._capture_selector(candidate_identity, deadline)
        _, receipt = self._command(
            deadline,
            "logcat",
            "-d",
            "-v",
            "threadtime",
            "--pid",
            str(value["pid"]),
            "-t",
            "1000",
            "BleHubBlengClose:D",
            "ReactNativeJS:V",
            "*:S",
            selector=self._selector,
        )
        return receipt

    def start_live(self, identity_path: Path) -> Path:
        """Start once after bound identity publication, before UI Start (host-owned).

        This opens only a tag-whitelisted ADB stream; it does not re-resolve App
        identity or attest that a request arrived. No buffer clear/settings change.
        The host must call stop_live even when start or the scenario fails.
        """
        if self._live_attempted:
            raise RuntimeError("live Close capture is one-shot, including failures")
        self._live_attempted = True
        capture_only = False
        try:
            self._capture_selector(identity_path, self._deadline)
            selector, identity_raw = self._selector, self._selector_raw
            assert selector is not None and identity_raw is not None
            started = time.monotonic()
            self._live_record = {
                "run_id": self._run_id,
                "candidate_identity": {"path": str(selector[0]), "pin": selector[1]},
                "identity_raw": self._publish_raw(
                    "live-bound-identity.json", identity_raw
                ),
                "started_monotonic": started,
                "deadline_monotonic": min(self._deadline, started + 300),
                "maximum_raw_bytes": _LIVE_LIMIT,
                "limitation": _LIVE_LIMITATION,
                "reason": None,
                "error": None,
            }
            argv = android_sdk.adb_command(
                self._device,
                "logcat",
                "-b",
                "main,system,crash,events",
                "-v",
                "threadtime",
                "-T",
                "1",
                *LIVE_LOGCAT_TAGS,
            )
            self._live_record["argv"] = argv
            intent = self._publish("live-intent.json", self._live_record)
            self._guard(self._deadline, selector=selector)
            capture_only = True
            self._live_process = subprocess.Popen(
                argv,
                cwd=self._cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )
            self._live_record["host_pid"] = self._live_process.pid
            self._live_reader = threading.Thread(target=self._drain_live, daemon=True)
            self._live_reader.start()
            capture_only = False
            self._guard(self._deadline, selector=selector)
            self._publish(
                "live-started.json",
                {
                    "host_pid": self._live_process.pid,
                    "spawned_monotonic": time.monotonic(),
                    "limitation": _LIVE_LIMITATION,
                },
            )
            self._guard(self._deadline, selector=selector)
            return intent
        except BaseException as error:
            # Spawn/reader failure is not property uncertainty; local stop is still mandatory.
            if not capture_only:
                self._failure = self._failure or error
            self._live_stop.set()
            try:
                self._publish(
                    "live-start-failed.json",
                    {
                        "error": {"type": type(error).__name__, "message": str(error)},
                        "limitation": _LIVE_LIMITATION,
                    },
                )
            except Exception as publication:  # noqa: BLE001 -- retain the primary start failure/interrupt.
                error.add_note(f"live start failure publication: {publication}")
            raise

    def _end_live_process(self, deadline: float | None = None) -> None:
        # Popen owns exactly this host child. Never signal a PID/group, App or daemon.
        process = self._live_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(
                    timeout=min(0.25, self._budget(deadline))
                    if deadline is not None
                    else 0.25
                )
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(
                    timeout=min(0.25, self._budget(deadline))
                    if deadline is not None
                    else 0.25
                )

    def _drain_live(self) -> None:
        process = self._live_process
        assert process is not None
        try:
            pipe = process.stdout
            if pipe is None:
                raise RuntimeError("live logcat stdout pipe missing")
            os.set_blocking(pipe.fileno(), False)
            while True:
                if self._live_stop.is_set():
                    self._live_record["reason"] = "stop-requested"
                    break
                if time.monotonic() >= self._live_record["deadline_monotonic"]:
                    self._live_record["reason"] = "deadline"
                    break
                try:
                    raw = os.read(
                        pipe.fileno(), min(16384, _LIVE_LIMIT - len(self._live_data))
                    )
                except BlockingIOError:
                    raw = b""
                if raw:
                    self._live_data.extend(raw)
                    arrival = {
                        "offset": len(self._live_data),
                        "monotonic": time.monotonic(),
                    }
                    self._live_record.setdefault("first_arrival", arrival)
                    self._live_arrivals.append(arrival)
                    if len(self._live_data) == _LIVE_LIMIT:
                        self._live_record["reason"] = "byte-limit"
                        break
                    continue
                if process.poll() is not None:
                    raise RuntimeError(
                        f"live logcat exited unexpectedly: {process.returncode}"
                    )
                self._live_stop.wait(0.02)
        except BaseException as error:  # noqa: BLE001 -- transfer worker failures to stop_live, never declare success.
            self._live_error = error
            self._live_record["reason"] = "capture-failed"
        finally:
            try:
                self._end_live_process()
            except BaseException as error:  # noqa: BLE001 -- preserve local child cleanup failure for the host.
                if self._live_error is None:
                    self._live_error = error
                else:
                    self._live_error.add_note(f"live child cleanup: {error}")
            finally:
                # Never read to EOF/communicate: descendants may retain the pipe.
                if process.stdout is not None:
                    process.stdout.close()
                self._live_record["finished_monotonic"] = time.monotonic()

    def stop_live(self, deadline: float) -> Path | None:
        """Bounded local-only cleanup, even after phone/receipt authority is lost."""
        if not self._live_attempted:
            return None
        if self._live_stopped:
            if self._live_error is not None:
                raise self._live_error
            return self._output / "live-stop.json"
        self._live_stop.set()
        capture_only = False
        try:
            budget = self._budget(deadline)
            if self._live_reader is not None and self._live_reader.ident is not None:
                self._live_reader.join(timeout=min(1.0, budget))
                if self._live_reader.is_alive():
                    raise TimeoutError(
                        "live capture local drain did not stop before cleanup deadline"
                    )
            else:
                try:
                    self._end_live_process(deadline)
                finally:
                    if (
                        self._live_process is not None
                        and self._live_process.stdout is not None
                    ):
                        self._live_process.stdout.close()
            if self._live_process is not None and self._live_process.poll() is None:
                raise RuntimeError("live capture host child remains running")
            self._live_stopped = True
            raw = self._publish_raw("live-logcat.raw", bytes(self._live_data))
            self._live_record.update(
                raw=raw,
                arrivals=list(self._live_arrivals),
                returncode=self._live_process.returncode
                if self._live_process
                else None,
                error={
                    "type": type(self._live_error).__name__,
                    "message": str(self._live_error),
                    "notes": list(getattr(self._live_error, "__notes__", [])),
                }
                if self._live_error is not None
                else None,
            )
            receipt = self._publish("live-stop.json", self._live_record)
            self._guard(deadline, cleanup=True, selector=self._selector)
            if self._live_error is not None:
                capture_only = True  # Child reaped, reader stopped, scope checked.
                raise self._live_error
            return receipt
        except BaseException as error:
            if not capture_only:
                self._failure = self._failure or error
            try:
                self._publish(
                    "live-stop-failed.json",
                    {
                        "error": {"type": type(error).__name__, "message": str(error)},
                        "local_stopped": self._live_stopped,
                    },
                )
            except Exception as publication:  # noqa: BLE001 -- retain the primary cleanup failure/interrupt.
                error.add_note(f"live stop failure publication: {publication}")
            raise

    def _publish_raw(self, name: str, raw: bytes) -> dict:
        """Publish closed original bytes, never replacing an existing bundle path."""
        self._directory()
        path = self._output / name
        temporary = path.with_name(f".{name}.{uuid.uuid4().hex}.tmp")
        primary: BaseException | None = None
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                info = os.fstat(stream.fileno())
            os.link(temporary, path)
            temporary.unlink()
            _, pin = self._snapshot(path)
            digest = hashlib.sha256(raw).hexdigest()
            if pin != (info.st_dev, info.st_ino, digest):
                raise RuntimeError("before-Close raw publication changed")
            self._records[path] = pin
            return {"path": str(path), "sha256": digest, "size": len(raw)}
        except BaseException as error:
            primary = error
            self._integrity = self._integrity or error
            raise
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup:
                if primary is None:
                    raise
                primary.add_note(f"before-Close raw temporary cleanup: {cleanup}")

    @staticmethod
    def _rect(value: dict) -> dict:
        if type(value) is not dict or any(
            type(value.get(key)) not in (int, float)
            or not math.isfinite(value[key])
            or (key in {"width", "height"} and value[key] <= 0)
            for key in ("x", "y", "width", "height")
        ):
            raise ValueError(
                "before-Close rect requires finite coordinates/positive size"
            )
        return {key: value[key] for key in ("x", "y", "width", "height")}

    def capture_before_close(
        self,
        driver,
        element,
        *,
        deadline: float,
        require_authority: Callable[[], None],
    ) -> Path:
        """Read this already located element once; no click, retry or verdict.

        XML and unmodified PNG may contain sensitive UI data: keep them only in
        the ignored local run bundle. The host retains all execution authority.
        SDK reads are sequential: PNG, XML, element rect, window rect, element
        rect recheck. This is not an atomic UI/process snapshot; earlier raw files
        remain diagnostic evidence if a later geometry read fails.
        """
        if self._preclose_attempted:
            raise RuntimeError("before-Close capture is one-shot, including failures")
        self._preclose_attempted = True
        record = {
            "run_id": self._run_id,
            "elementId": None,
            "started_monotonic": time.monotonic(),
            "non_atomic": "Sequential SDK reads: PNG -> XML -> element.rect.before "
            "-> window rect -> element.rect.after; not an atomic UI/process snapshot; "
            "earlier raw files may precede a failed geometry read; "
            "no BLE/UI/Close success is inferred.",
            "reads": [],
            "rawfiles": {},
        }
        try:
            if not callable(require_authority):
                raise TypeError("mandatory host require_authority callback required")
            self._guard(deadline)
            require_authority()
            candidate = self._capture_selector(
                self._root / "candidate/bound-identity.json", deadline
            )
            selector = self._selector
            record.update(
                candidate_identity_sha256=selector[1][2],
                serial=candidate["serial"],
                pid=candidate["pid"],
            )
            executor = driver.command_executor
            config = executor.client_config
            session_id, element_id = driver.session_id, element.id
            record["elementId"] = element_id
            if (
                type(session_id) is not str
                or not session_id.strip()
                or type(element_id) is not str
                or not element_id.strip()
                or element.parent is not driver
                or executor._client_config is not config
            ):
                raise ValueError(
                    "fixed native element and same driver/session required"
                )
            original_timeout = expected_timeout = config.timeout

            def guard() -> None:
                try:
                    self._guard(deadline, selector=selector)
                except BaseException as scope:
                    try:
                        require_authority()
                    except BaseException as authority:  # noqa: BLE001 -- retain both independent boundary failures.
                        scope.add_note(f"before-Close host authority: {authority}")
                    raise
                require_authority()
                self._guard(deadline, selector=selector)
                if (
                    driver.command_executor is not executor
                    or executor.client_config is not config
                    or executor._client_config is not config
                    or driver.session_id != session_id
                    or element.parent is not driver
                    or element.id != element_id
                    or config.timeout != expected_timeout
                ):
                    raise RuntimeError(
                        "before-Close SDK element/session/config changed"
                    )
                self._budget(deadline)

            def read(name: str, operation: Callable):
                nonlocal expected_timeout
                guard()
                primary: BaseException | None = None
                observation = {"name": name, "started_monotonic": time.monotonic()}
                record["reads"].append(observation)
                try:
                    expected_timeout = self._budget(deadline)
                    config.timeout = expected_timeout
                    guard()
                    # Authority checks consume the same scenario budget too.
                    expected_timeout = self._budget(deadline)
                    config.timeout = expected_timeout
                    return operation()
                except BaseException as error:
                    primary = error
                    raise
                finally:
                    try:
                        guard()
                    except BaseException as boundary:  # noqa: BLE001 -- preserve SDK primary and post-read failure.
                        if primary is None:
                            primary = boundary
                        else:
                            primary.add_note(
                                f"before-Close post-read boundary: {boundary}"
                            )
                    finally:
                        # Restore only the saved object, never a replacement config.
                        config.timeout = original_timeout
                        expected_timeout = original_timeout
                        observation["finished_monotonic"] = time.monotonic()
                    if primary is not None:
                        raise primary

            def source() -> None:
                value = driver.page_source
                if type(value) is not str:
                    raise ValueError("before-Close XML must be UTF-8 text")
                raw = value.encode("utf-8")
                if len(raw) > 1024 * 1024:
                    raise ValueError("before-Close XML exceeds 1 MiB")
                record["rawfiles"]["xml"] = self._publish_raw("before-close.xml", raw)

            def screenshot() -> None:
                raw = driver.get_screenshot_as_png()
                if (
                    type(raw) is not bytes
                    or len(raw) > 8 * 1024 * 1024
                    or not raw.startswith(b"\x89PNG\r\n\x1a\n")
                ):
                    raise ValueError(
                        "before-Close screenshot must be PNG bytes <= 8 MiB"
                    )
                record["rawfiles"]["png"] = self._publish_raw("before-close.png", raw)

            read("driver.get_screenshot_as_png", screenshot)
            read("driver.page_source", source)
            record["before_rect"] = read(
                "element.rect.before", lambda: self._rect(element.rect)
            )
            record["window_rect"] = read(
                "driver.get_window_rect", lambda: self._rect(driver.get_window_rect())
            )
            record["after_rect"] = read(
                "element.rect.after", lambda: self._rect(element.rect)
            )
            if record["before_rect"] != record["after_rect"]:
                raise RuntimeError("before-Close element rect changed")
            guard()
            record["finished_monotonic"] = time.monotonic()
            receipt = self._publish("before-close.json", record)
            guard()
            return receipt
        except BaseException as error:
            record["finished_monotonic"] = time.monotonic()
            record["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "notes": list(getattr(error, "__notes__", [])),
            }
            try:
                self._publish("before-close-failed.json", record)
            except BaseException as publication:  # noqa: BLE001 -- publication must not hide the capture failure.
                error.add_note(f"before-Close failure publication: {publication}")
            raise
