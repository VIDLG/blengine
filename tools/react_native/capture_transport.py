"""Protocol-independent mobile capture transport; scenarios own facts and ACK policy."""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

from tests.peripherals.nrf52840.cancellation_control import read_document
from tools.react_native import android_sdk
from tools.react_native.android_rf_host import wait_for_initial_pid
from tools.react_native.capture_io import MAX_BYTES, PrefixStream, stop_local
from tools.react_native.host_contract import (
    ANDROID_ACTIVITY,
    ANDROID_PACKAGE,
    APP_NAME,
    BUNDLE_ID,
    SCHEME,
)
from tools.react_native.ios_sdk import (
    devicectl_launch_app,
    devicectl_launch_payload,
    devicectl_post_notification,
    devicectl_processes,
    ios_executables_match,
    official_result,
)

ANDROID_COMPONENT = f"{ANDROID_PACKAGE}/{ANDROID_ACTIVITY}"
POLL_SECONDS = 0.05

PROCESS_SECONDS = 0.5
BOOT_SECONDS = 45.0
COMMAND_SECONDS = 1.5


class CaptureTransport:
    """Host SDK mechanics only; subclass hooks frame and validate their scenario."""

    def __init__(
        self,
        platform: str,
        device: str,
        log: Path,
        coordination: Path,
        run_id: str,
        service_uuid: str,
        local_name: str,
        timeout: float,
        guard: Callable[[], None],
        install_json: Path | None,
    ) -> None:
        self.platform, self.device, self.log = platform, device, log
        self.root, self.run_id = coordination, run_id
        self.service, self.name, self.guard = service_uuid, local_name, guard
        self.started = time.monotonic()
        self.deadline = self.started + timeout
        self.boot_deadline = self.deadline
        self.lines: list[str] = []
        self.pid: int | None = None
        self.executable = ""
        self.device_identifier: str | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.output: BinaryIO | None = None
        self.stream: PrefixStream | None = None
        self.progress: dict[str, Any] | None = None
        self.next_guard = self.next_pid = self.started
        self.identity: dict[str, Any] = {
            "schema_version": 1,
            "platform": platform,
            "device": device,
            "run_id": run_id,
            "status": "awaiting-bootstrap",
            "install_json": str(install_json) if install_json else None,
            "executable_scope": "installed package code path; no binary hash claim",
        }
        self.receipt: dict[str, Any] = {
            "schema_version": 1,
            "platform": platform,
            "device": device,
            "run_id": run_id,
            "status": "running",
            "rf_verdict": "parent checker required",
            "actions": [],
            "budgets_seconds": {
                "whole": timeout,
                "bootstrap": BOOT_SECONDS,
                "command_outer": COMMAND_SECONDS,
                "sdk_timeout_floor": 5,
                "poll": POLL_SECONDS,
            },
        }
        if platform == "ios":
            if install_json is None:
                raise ValueError(
                    "iOS capture requires the parent's official install JSON"
                )
            installed = official_result(
                install_json, "devicectl.device.install.app", device
            )
            rows = installed.get("installedApplications")
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise RuntimeError("malformed official installedApplications")
            matches = [r for r in rows if r.get("bundleID") == BUNDLE_ID]
            if len(matches) != 1:
                raise RuntimeError(
                    "install receipt does not uniquely identify the host"
                )
            url = matches[0].get("installationURL")
            if (
                not isinstance(url, str)
                or not url.startswith("file:///")
                or not url.endswith(f"/{APP_NAME}/")
            ):
                raise RuntimeError(
                    "install receipt lacks the full host installation URI"
                )
            self.executable = url + SCHEME
            self.device_identifier = installed["deviceIdentifier"]
            self.identity["executable_scope"] = (
                "official installed CFBundleExecutable URI"
            )

    def _command(self, command: list[str], *, budget: float = COMMAND_SECONDS) -> str:
        """Pump the console even while an SDK query/relay is pending; no ACK delay."""
        deadline = min(self.deadline, time.monotonic() + budget)
        deadline = min(deadline, self._request_deadline())
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(command, budget)
        primary: BaseException | None = None
        with tempfile.TemporaryFile() as output:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                while child.poll() is None:
                    self._poll()
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, budget)
                    time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
                self._poll()
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, budget)
                output.seek(0)
                text = output.read(MAX_BYTES + 1).decode("utf-8", errors="replace")
                if len(text.encode("utf-8")) > MAX_BYTES:
                    raise RuntimeError("SDK/ADB command output exceeds capture budget")
                if child.returncode != 0:
                    raise subprocess.CalledProcessError(child.returncode, command, text)
                return text
            except BaseException as error:
                primary = error
                raise
            finally:
                try:
                    stop_local(child, console=False)
                except BaseException as cleanup:
                    if primary is None:
                        raise
                    primary.add_note(f"capture command cleanup failed: {cleanup}")

    def _verify_process(self, boundary: str) -> None:
        self.guard()
        budget = self._identity_budget(boundary)
        if self.platform == "android":
            output = self._command(
                android_sdk.adb_command(self.device, "shell", "pidof", ANDROID_PACKAGE),
                budget=min(1.0, budget),
            ).strip()
            if output != str(self.pid):
                raise RuntimeError("pinned Android process replaced or absent")
        else:
            path = self.log.with_suffix(f".{boundary}-processes.json")
            path.unlink(missing_ok=True)
            self._command(
                devicectl_processes(self.device, path),
                budget=budget,
            )
            result = official_result(
                path, "devicectl.device.info.processes", self.device
            )
            rows = result.get("runningProcesses")
            if (
                result["deviceIdentifier"] != self.device_identifier
                or not isinstance(rows, list)
                or any(
                    not isinstance(row, dict)
                    or type(row.get("processIdentifier")) is not int
                    or row["processIdentifier"] <= 0
                    for row in rows
                )
            ):
                raise RuntimeError("foreign/malformed official process/device identity")
            matches = [row for row in rows if row["processIdentifier"] == self.pid]
            if len(matches) != 1 or not ios_executables_match(
                self.executable, matches[0].get("executable", "")
            ):
                raise RuntimeError("pinned iOS PID/executable replaced or absent")
        self.guard()
        self.next_pid = time.monotonic() + PROCESS_SECONDS

    def _launch(self) -> None:
        self.guard()
        if self.platform == "android":
            paths = (
                self._command(
                    android_sdk.adb_command(
                        self.device, "shell", "pm", "path", ANDROID_PACKAGE
                    )
                )
                .strip()
                .splitlines()
            )
            if (
                len(paths) != 1
                or not paths[0].startswith("package:/")
                or not paths[0].endswith("/base.apk")
            ):
                raise RuntimeError(
                    "expected exactly one official Android base.apk path"
                )
            # java.io.File.toURI() uses file:/, not Path.as_uri()'s file:///.
            self.executable = "file:" + quote(
                paths[0][len("package:") :], safe="/:@!$&'()*+,;="
            )
            started = time.monotonic()
            self.receipt["launch_started_at"] = started
            self.boot_deadline = min(self.deadline, started + BOOT_SECONDS)
            launch_output = self._command(
                android_sdk.adb_command(
                    self.device,
                    "shell",
                    "am",
                    "start",
                    "-n",
                    ANDROID_COMPONENT,
                    *self._android_launch_arguments(),
                )
            )
            if "Error:" in launch_output or "Exception" in launch_output:
                raise RuntimeError("initial Android explicit-component launch failed")
            self.pid = int(
                wait_for_initial_pid(
                    self.device,
                    self.log,
                    max(0.0, self.deadline - time.monotonic()),
                    guard=self.guard,
                )
            )
            self.identity.update(read_document(self.log.with_suffix(".identity.json")))
            command = android_sdk.adb_command(
                self.device, "logcat", f"--pid={self.pid}", "-v", "threadtime"
            )
        else:
            command = devicectl_launch_app(
                self.device,
                BUNDLE_ID,
                self.log.with_suffix(".launch.json"),
                self._ios_launch_arguments(),
            )
            started = time.monotonic()
            self.receipt["launch_started_at"] = started
            self.boot_deadline = min(self.deadline, started + BOOT_SECONDS)
        self.output = self.log.with_suffix(".raw.log").open("xb")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=self.output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.stream = self._new_stream()
        self.receipt.update(capture_pid=self.process.pid, command=command)
        self._publish()

    def _relay(
        self, url: str, boundary: str, *, notification: str | None = None
    ) -> None:
        """Deliver through the same pumped SDK owner and exact process receipts."""
        if self.platform == "android":
            command = android_sdk.adb_command(
                self.device,
                "shell",
                "am",
                "start",
                "-n",
                ANDROID_COMPONENT,
                "-a",
                "android.intent.action.VIEW",
                "-d",
                url,
            )
        else:
            path = self.log.with_suffix(f".{boundary}-payload.json")
            path.unlink(missing_ok=True)
            command = devicectl_launch_payload(self.device, BUNDLE_ID, url, path)
        if self.platform == "ios" and notification is not None:
            command = devicectl_post_notification(self.device, notification)
        output = self._command(command)
        if "Error:" in output or "Exception" in output:
            raise RuntimeError("payload URL command failed")
        if self.platform == "ios" and notification is None:
            result = official_result(
                path, "devicectl.device.process.launch", self.device
            )
            process = result.get("process")
            arguments = read_document(path)["info"]["arguments"]
            if (
                result["deviceIdentifier"] != self.device_identifier
                or not isinstance(process, dict)
                or type(process.get("processIdentifier")) is not int
                or process["processIdentifier"] != self.pid
                or not ios_executables_match(
                    self.executable, process.get("executable", "")
                )
                or arguments.count("--payload-url") != 1
                or arguments[arguments.index("--payload-url") + 1 :][:1] != [url]
                or arguments[-1:] != [BUNDLE_ID]
                or "--terminate-existing" in arguments
                or "--console" in arguments
            ):
                raise RuntimeError(
                    "official payload launch receipt changed device/PID/URL"
                )
        self._verify_process(f"{boundary}-after")
        self._poll()

    def _identity_budget(self, boundary: str) -> float:
        return COMMAND_SECONDS

    def _request_deadline(self) -> float:
        return self.deadline

    def _poll(self) -> None:
        raise NotImplementedError

    def _publish(self) -> None:
        raise NotImplementedError

    def _android_launch_arguments(self) -> list[str]:
        raise NotImplementedError

    def _ios_launch_arguments(self) -> list[str]:
        raise NotImplementedError

    def _new_stream(self) -> PrefixStream:
        raise NotImplementedError
