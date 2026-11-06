"""Shared bounded Appium infrastructure for maintained mobile UI checks.

Platform runners own only their artifact, device identity, UI assertions, and
attempt evidence. This module owns the Appium server process, Python client
session-independent waits, and cleanup of the server process group.
"""

from __future__ import annotations

import http.client
import io
import json
import math
import os
import re
import signal
import socket
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from appium import webdriver
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)

ROOT = Path(__file__).resolve().parents[1]


class AppiumError(RuntimeError):
    """Raised when a bounded Appium operation cannot prove its contract."""


def _json_value(document: dict[str, Any], *keys: str) -> Any:
    current: Any = document
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _deadline(timeout: float, *, deadline: float | None = None) -> float:
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number of seconds")
    local_deadline = time.monotonic() + timeout
    if not math.isfinite(local_deadline):
        raise ValueError("timeout exceeds the monotonic clock range")
    if deadline is not None:
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("deadline must be a finite absolute monotonic time")
        return min(local_deadline, deadline)
    return local_deadline


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AppiumError("Appium deadline exceeded")
    return remaining


class _HttpBuffer(io.BytesIO):
    def makefile(self, mode: str) -> io.BytesIO:
        return self


def _status_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate Appium status key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"invalid Appium JSON constant: {value}")


def read_loopback_json(base_url: str, path: str, *, deadline: float) -> dict:
    # Only the local owner endpoint is supported: no DNS, proxy or redirect can
    # move readiness to another server. Connection: close bounds the whole wire
    # response (headers included) to 64 KiB before parsing entirely in memory.
    url = urlsplit(base_url)
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or url.port is None
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or any(character.isspace() for character in base_url)
    ):
        raise ValueError("Appium readiness requires an explicit loopback HTTP endpoint")
    if re.fullmatch(r"/[A-Za-z0-9_/-]+", path) is None:
        raise ValueError("explicit canonical local HTTP read path required")
    payload = (
        f"GET {url.path.rstrip('/')}{path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{url.port}\r\nConnection: close\r\n\r\n"
    ).encode("ascii")
    raw = bytearray()
    with socket.create_connection(
        ("127.0.0.1", url.port), timeout=min(5.0, _remaining(deadline))
    ) as connection:
        connection.settimeout(min(5.0, _remaining(deadline)))
        connection.sendall(payload)
        while len(raw) < 65536:
            connection.settimeout(min(5.0, _remaining(deadline)))
            chunk = connection.recv(min(4096, 65536 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        else:
            raise AppiumError("Appium status reached the 64 KiB HTTP limit")
    _remaining(deadline)
    with http.client.HTTPResponse(_HttpBuffer(raw)) as response:
        response.begin()
        if response.status != 200:
            raise AppiumError(f"Appium status returned HTTP {response.status}")
        document = json.loads(
            response.read(65536).decode("utf-8"),
            object_pairs_hook=_status_object,
            parse_constant=_invalid_constant,
        )
        if response.length not in (None, 0):
            raise AppiumError("truncated Appium status body")
    if type(document) is not dict:
        raise AppiumError("local HTTP response must be a JSON object")
    return document


def _server_ready(base_url: str, deadline: float) -> bool:
    document = read_loopback_json(base_url, "/status", deadline=deadline)
    ready = _json_value(document, "value", "ready")
    if type(ready) is not bool:
        raise AppiumError("Appium value.ready must be an explicit boolean")
    return ready


def wait_for_server(
    base_url: str,
    timeout: float,
    *,
    require_owner: Callable[[], None] | None = None,
    deadline: float | None = None,
) -> None:
    """Poll local readiness within one budget; guard both sides of every HTTP try.

    The synchronous caller guard must itself be bounded. Its failures are never
    retried as transport errors, including when HTTP fails or reports not-ready.
    An absolute monotonic deadline also caps the budget across caller handoff.
    """
    deadline = _deadline(timeout, deadline=deadline)
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if require_owner is not None:
            require_owner()
        _remaining(deadline)
        ready = False
        try:
            ready = _server_ready(base_url, deadline)
        except (AppiumError, OSError, ValueError, http.client.HTTPException) as failure:
            last_error = failure
        finally:
            if require_owner is not None:
                require_owner()
        _remaining(deadline)
        if ready:
            return
        time.sleep(min(0.25, _remaining(deadline)))
    detail = f": {last_error}" if last_error else ""
    raise AppiumError(f"Appium server did not become ready{detail}")


def require_attached_driver(
    driver: webdriver.Remote,
    *,
    session_id: str,
    platform: str,
    device: str,
    app_id: str,
) -> None:
    """Verify one explicit attach-only session; this is not device ownership proof."""
    if platform not in {"ios", "android"}:
        raise ValueError("unsupported attached driver platform")
    if (
        not isinstance(session_id, str)
        or not session_id
        or driver.session_id != session_id
    ):
        raise AppiumError("prepared driver session changed")
    caps = driver.capabilities
    if type(caps) is not dict or str(caps.get("platformName", "")).lower() != platform:
        raise AppiumError("prepared driver platform changed")
    app_key = "bundleId" if platform == "ios" else "appPackage"
    for key, expected in (("udid", device), (app_key, app_id)):
        values = [caps[name] for name in (key, "appium:" + key) if name in caps]
        if not values or any(value != expected for value in values):
            raise AppiumError("prepared driver App/device changed")
    for key, expected in (
        ("autoLaunch", False),
        ("forceAppLaunch", False),
        ("shouldTerminateApp", False),
        ("noReset", True),
    ):
        values = [caps[name] for name in (key, "appium:" + key) if name in caps]
        if not values or any(value is not expected for value in values):
            raise AppiumError("prepared driver must be explicitly attach-only")
    if any(
        caps.get(name) is not False
        for name in ("fullReset", "appium:fullReset")
        if name in caps
    ):
        raise AppiumError("prepared driver must be explicitly attach-only")


class AppiumServer:
    """One-shot local process owner; caller always owns the finally: stop() path.

    Startup failure does not implicitly stop the child or clean devices/sessions.
    The loopback bind probe rejects an occupied port but is NOT an atomic port
    reservation: the unique route and child checks also guard the bind race.
    Calls are serialized by the caller; require_running performs no HTTP/device IO.
    """

    def __init__(
        self,
        *,
        node: str,
        entry: Path,
        port: int,
        log: Path,
        timeout: float,
        cwd: Path = ROOT,
        appium_home: Path | None = None,
        debug_logging: bool = False,
    ) -> None:
        _deadline(timeout)
        if type(debug_logging) is not bool:
            raise ValueError("debug_logging requires an explicit boolean")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Appium port must be an integer in 1..65535")
        self._port = port
        base_path = f"/blehub-{uuid.uuid4()}"
        self._base_url = f"http://127.0.0.1:{port}{base_path}"
        self._command = [
            node,
            str(entry),
            "--address",
            "127.0.0.1",
            "--port",
            str(port),
            "--base-path",
            base_path,
            "--log-level",
            "debug" if debug_logging else "info",
        ]
        if debug_logging:
            # Debug may include UI text and payload prefixes: keep it in the
            # caller's local evidence, not console summaries or external uploads.
            self._command.extend(["--log-timestamp", "--log-no-colors"])
        self._cwd = cwd
        self._environment = None
        if appium_home is not None:
            self._environment = dict(
                os.environ, APPIUM_HOME=str(appium_home.absolute())
            )
        self._log = log
        self._timeout = timeout
        self._process: subprocess.Popen[str] | None = None
        self._owned_process: subprocess.Popen[str] | None = None
        self._owned_pid: int | None = None
        self._start_attempted = False
        self._ready = False
        self._stopped = False
        self._cleanup_failure: AppiumError | None = None

    @property
    def base_url(self) -> str:
        """The endpoint pinned to this owner's spawn nonce."""
        return self._base_url

    def _require_child(self) -> None:
        process = self._owned_process
        if (
            self._stopped
            or process is None
            or self._process is not process
            or process.pid != self._owned_pid
        ):
            raise AppiumError("Appium owned child is missing, replaced or stopped")
        if process.poll() is not None:
            raise AppiumError(f"Appium owned child exited: {process.returncode}")

    def require_running(self) -> None:
        """Read-only local check; not a fresh HTTP or device-health assertion."""
        if not self._ready:
            raise AppiumError("Appium owned child has not completed readiness")
        self._require_child()

    def start(
        self,
        *,
        require_owner: Callable[[], None] | None = None,
        deadline: float | None = None,
    ) -> None:
        """Start within self timeout, capped by an optional absolute monotonic deadline."""
        if self._start_attempted or self._stopped:
            raise AppiumError("AppiumServer start is one-shot")
        self._start_attempted = True
        deadline = _deadline(self._timeout, deadline=deadline)
        if require_owner is not None:
            require_owner()
        _remaining(deadline)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", self._port))
        except OSError as failure:
            raise AppiumError(
                f"Appium loopback port unavailable: {failure}"
            ) from failure
        self._log.parent.mkdir(parents=True, exist_ok=True)
        with self._log.open("x", encoding="utf-8") as output:
            if require_owner is not None:
                require_owner()
            _remaining(deadline)
            self._process = self._owned_process = subprocess.Popen(
                self._command,
                cwd=self._cwd,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
                **({"env": self._environment} if self._environment is not None else {}),
            )
            self._owned_pid = self._owned_process.pid

        def guard() -> None:
            if require_owner is not None:
                require_owner()
            self._require_child()

        wait_for_server(
            self.base_url, _remaining(deadline), require_owner=guard, deadline=deadline
        )
        guard()
        _remaining(deadline)
        self._ready = True

    def stop(self, *, timeout: float = 15) -> None:
        """TERM, optionally KILL, then reap within an independent total budget.

        Half the budget is reserved for escalation/reap. Escalation is a cleanup
        failure even if KILL succeeds; repeated calls preserve that failure and
        never signal a possibly reused PID. Reaping this local group needs no
        new phone mutation authority, but child shutdown handlers may themselves
        perform remote cleanup. A reaped child never proves device cleanup.
        """
        deadline = _deadline(timeout)
        if self._stopped:
            if self._cleanup_failure is not None:
                raise self._cleanup_failure
            return
        self._stopped = True
        self._ready = False
        process = self._owned_process
        if process is None:
            return
        failures: list[str] = []
        if self._process is not process or process.pid != self._owned_pid:
            failures.append("Appium owned child identity changed")
        # Never use a replacement process or signal after the leader is already
        # reaped: the numeric group ID may since have been reused by the OS.
        try:
            if process.pid != self._owned_pid:
                failures.append("refused signaling an unowned process group")
            elif process.poll() is not None:
                failures.append("owned child already exited; group cleanup unverified")
            else:
                try:
                    os.killpg(self._owned_pid, signal.SIGTERM)
                except OSError as failure:
                    failures.append(f"TERM: {failure}")
                try:
                    process.wait(timeout=max(0.0, (deadline - time.monotonic()) / 2))
                except (OSError, subprocess.SubprocessError, TimeoutError) as failure:
                    failures.append(f"TERM wait: {failure}")
                    if process.poll() is None:
                        failures.append("escalated to SIGKILL")
                        try:
                            os.killpg(self._owned_pid, signal.SIGKILL)
                        except OSError as kill_failure:
                            failures.append(f"KILL: {kill_failure}")
                        try:
                            process.wait(timeout=max(0.0, deadline - time.monotonic()))
                        except (
                            OSError,
                            subprocess.SubprocessError,
                            TimeoutError,
                        ) as reap_failure:
                            failures.append(f"KILL reap: {reap_failure}")
        except (OSError, subprocess.SubprocessError, TimeoutError) as failure:
            failures.append(f"local cleanup: {failure}")
        except BaseException as failure:
            # Preserve interruption/programming failures without converting them
            # to success on a subsequent finally/close call.
            failures.append(f"{type(failure).__name__}: {failure}")
            self._cleanup_failure = AppiumError(
                "Appium server cleanup incomplete: " + "; ".join(failures)
            )
            raise
        if time.monotonic() > deadline:
            failures.append("cleanup total deadline exceeded")
        if failures:
            self._cleanup_failure = AppiumError(
                "Appium server cleanup failed: " + "; ".join(failures)
            )
            raise self._cleanup_failure


def wait_find_by(driver: webdriver.Remote, strategy: str, value: str, timeout: float):
    """Find one element with a platform-appropriate Appium locator."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return driver.find_element(strategy, value)
        except (NoSuchElementException, StaleElementReferenceException):
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    raise AppiumError(f"element {value!r} did not appear within {timeout:.1f}s")


def wait_displayed_by(
    driver: webdriver.Remote, strategy: str, value: str, timeout: float
) -> None:
    """Wait for a currently displayed element with a platform locator."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            element = driver.find_element(strategy, value)
            if element.is_displayed():
                return
        except (NoSuchElementException, StaleElementReferenceException):
            pass
        time.sleep(0.25)
    raise AppiumError(f"element {value!r} did not become displayed")


def wait_find(driver: webdriver.Remote, identifier: str, timeout: float):
    """Find an accessibility identifier while tolerating tree refreshes."""
    return wait_find_by(driver, AppiumBy.ACCESSIBILITY_ID, identifier, timeout)


def wait_displayed(driver: webdriver.Remote, identifier: str, timeout: float) -> None:
    """Wait for a currently displayed accessibility element."""
    wait_displayed_by(driver, AppiumBy.ACCESSIBILITY_ID, identifier, timeout)


def wait_for_package(driver: webdriver.Remote, package: str, timeout: float) -> None:
    """Require UiAutomator2 to be attached to the requested Android package."""
    deadline = time.monotonic() + timeout
    observed: str | None = None
    while time.monotonic() < deadline:
        try:
            observed = driver.current_package
        except WebDriverException:
            observed = None
        if observed == package:
            return
        time.sleep(0.25)
    raise AppiumError(
        f"UiAutomator2 did not attach to package {package!r}; observed {observed!r}"
    )
