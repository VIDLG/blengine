"""Strict, hardware-free SDK/HTTP samples for Android automation ownership.

Only device-facing subprocess, Appium Remote/process and socket boundaries are
replaced. The controller, inspector, identity reader and HTTP parser stay real.
No helper is installed and every local path belongs to the caller's temp tree.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest.mock import Mock, patch

from tools import mobile_appium
from tools.android.android_appium import create_android_driver
from tools.android.state_control import AndroidRadioController
from tools.react_native import android_sdk
from tools.tests.react_native.support.appium_session import automation_sdk
from tools.tests.react_native.support.automation_observation import (
    COMPONENT,
    SERVICES_HEADER,
    processes,
    record,
)

SETTINGS = "io.appium.settings"
SERVER = "io.appium.uiautomator2.server"
TEST = SERVER + ".test"
PACKAGES = (SETTINGS, SERVER, TEST)

POLICIES = (
    "hidden_api_policy_pre_p_apps",
    "hidden_api_policy_p_apps",
    "hidden_api_policy",
)
BOOT = ("shell", "cat", "/proc/sys/kernel/random/boot_id")
PS = ("shell", "ps", "-A", "-o", "PID,UID,ARGS")
FORWARDS = ("forward", "--list")
GLOBAL_SETTINGS = ("shell", "settings", "list", "global")
GLOBAL_SECRET_MARKER = "synthetic-global-secret-not-for-owner-receipts"
OS_PROCESSES = ("shell", "dumpsys", "activity", "processes")
SERVICES = ("shell", "dumpsys", "activity", "services", SETTINGS)
APPOPS = ("shell", "cmd", "appops", "get", SETTINGS, "android:mock_location")
STORE = ("shell", "cat", "/data/local/tmp/mock_apps.json")
DEFAULT_IME = ("shell", "settings", "get", "secure", "default_input_method")
ENABLED_IMES = ("shell", "settings", "get", "secure", "enabled_input_methods")
EMPTY_SERVICES = SERVICES_HEADER + "  (nothing)\n"
LIVE_SERVICES = (
    SERVICES_HEADER
    + "  User 0 active services:\n"
    + "  * ServiceRecord{abcd u0 io.appium.settings/.SettingsService}\n"
)
DEVICE_SESSION_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
HOST_SESSION_ID = "host-owned-session"


def proc_stat(pid: int, ticks: int = 700) -> str:
    """Linux field 22, including a comm containing spaces and a closing paren."""
    return f"{pid} (app ) worker) S " + "0 " * 18 + f"{ticks} 0\n"


def active_processes(
    *, pid: int = 3210, uid: int = 10102, target_package: str = SERVER
) -> str:
    """Keep the process identity independent of ApplicationInfo's target package."""
    body = (
        record(pid=pid, app_id=uid - 10000)
        .replace(
            f"mTargetInfo=ApplicationInfo{{abcd {SERVER}}}",
            f"mTargetInfo=ApplicationInfo{{abcd {target_package}}}",
        )
        .replace(
            f"        packageName={SERVER}\n", f"        packageName={target_package}\n"
        )
    )
    return processes("  Active instrumentation:\n" + body)


class _ReadSocket:
    """One Connection: close HTTP exchange, dispatched only after sendall."""

    def __init__(self, harness: AndroidAutomationHarness, port: int) -> None:
        self.harness, self.port = harness, port
        self.response: bytes | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def settimeout(self, timeout: float) -> None:
        assert math.isfinite(timeout) and 0 < timeout <= 5
        self.timeout = timeout

    def sendall(self, payload: bytes) -> None:
        assert self.response is None, "one request per fake socket"
        if self.port == self.harness.system_port:
            if payload.startswith(b"GET /status HTTP/1.1\r\n"):
                path = "/status"
                document = self.harness.server_status_response
            else:
                path = "/sessions"
                document = {"value": self.harness.device_sessions}
        else:
            assert self.port == self.harness.host_port, "unknown loopback endpoint"
            path = self.harness.host_base_path + "/status"
            document = {"value": {"ready": True}}
        expected = (
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        assert payload == expected, f"unexpected HTTP request: {payload!r}"
        self.harness.http_calls.append((self.port, path))
        if self.port == self.harness.system_port and path == "/status":
            self.harness.sdk.events.append("fresh-server-status")
            if self.harness.server_status_timeout:
                self.harness.clock += self.timeout
                raise TimeoutError("fresh server status timeout")
        body = json.dumps(document).encode("utf-8")
        self.response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\n\r\n"
            + body
        )

    def recv(self, maximum: int) -> bytes:
        assert self.response is not None, "recv before a validated HTTP request"
        chunk, self.response = self.response[:maximum], self.response[maximum:]
        return chunk


class _AdbReadSocket:
    """Strict smart-socket script, deliberately separate from UiA HTTP."""

    def __init__(
        self, harness: AndroidAutomationHarness, requests: list[bytes]
    ) -> None:
        self.harness, self.requests = harness, requests
        self.response = b""
        self.timeouts: list[float] = []
        self.read_sizes: list[int] = []
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def settimeout(self, timeout: float) -> None:
        assert math.isfinite(timeout) and 0 < timeout <= 3
        self.timeouts.append(timeout)

    def sendall(self, payload: bytes) -> None:
        assert not self.response, "previous ADB reply was not consumed"
        assert self.requests, "unexpected ADB service"
        command = self.requests.pop(0)
        assert payload == f"{len(command):04x}".encode("ascii") + command, payload
        h = self.harness
        h.adb_calls.append(command)
        assert h.adb_responses is not None
        self.response = h.adb_responses.pop(0)
        if h.adb_after_io:
            h.adb_after_io("send")

    def recv(self, maximum: int) -> bytes:
        self.read_sizes.append(maximum)
        h = self.harness
        chunk, self.response = (
            self.response[: min(maximum, h.adb_chunk_size)],
            self.response[min(maximum, h.adb_chunk_size) :],
        )
        if h.adb_after_io:
            h.adb_after_io("recv")
        return chunk


class AndroidAutomationHarness:
    """A read-only command dictionary with explicit idle/active/idle transitions."""

    run_id = "12345678-1234-4234-8234-123456789abc"
    serial = "synthetic-automation-phone"
    app_id = "com.blehubblenghost"
    system_port = 18200
    host_port = 18765
    boot_id = "11111111-2222-4333-8444-555555555555"

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        self.stack = ExitStack()
        self.clock = 100.0
        # Opt-in only: an unexpected 5037 connection must not get a ready/HTTP fake.
        self.adb_responses: list[bytes] | None = None
        self.adb_calls: list[bytes] = []
        self.adb_sockets: list[_AdbReadSocket] = []
        self.adb_chunk_size = 1
        self.adb_after_io: Callable[[str], None] | None = None
        self.deadline = 160.0
        self.appium_home = root / "appium-home"
        driver = self.appium_home / "node_modules/appium-uiautomator2-driver"
        self.apks = dict(
            zip(
                PACKAGES,
                (
                    driver
                    / "node_modules/io.appium.settings/apks/settings_apk-debug.apk",
                    driver
                    / "node_modules/appium-uiautomator2-server/apks/appium-uiautomator2-server-v10.7.6.apk",
                    driver
                    / "node_modules/appium-uiautomator2-server/apks/appium-uiautomator2-server-debug-androidTest.apk",
                ),
                strict=True,
            )
        )
        for package, path in self.apks.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"synthetic helper {package}; never install".encode())
        self.expected = {
            package: hashlib.sha256(path.read_bytes()).hexdigest()
            for package, path in self.apks.items()
        }
        self.paths = {
            package: f"/data/app/~~inventory/{package}-candidate/base.apk"
            for package in PACKAGES
        }
        self.uids = dict(zip(PACKAGES, (10101, 10102, 10103), strict=True))
        self.adb = root / "nonexistent-sdk/adb"
        self.output = root / "owner"
        self.phone = AndroidRadioController(
            self.serial, self.run_id, root / "phone.json", lock_root=root / "leases"
        )
        self.calls: list[tuple[str, ...]] = []
        self.http_calls: list[tuple[int, str]] = []
        self.host_base_path = ""
        self.device_sessions: list[dict] = []
        self.server_status_response = {"value": {"ready": True}}
        self.server_status_timeout = False
        self.after_command: Callable[[tuple[str, ...]], None] | None = None
        self.returncodes: dict[tuple[str, ...], int] = {}
        self.global_baseline = (
            "device_name=Synthetic phone\nstay_on_while_plugged_in=0\n"
            f"unrelated_secret={GLOBAL_SECRET_MARKER}\n"
        )
        self.replies = {
            ("get-state",): "device\n",
            BOOT: self.boot_id + "\n",
            ("shell", "am", "get-current-user"): "0\n",
            (
                "shell",
                "pm",
                "list",
                "users",
            ): "Users:\n\tUserInfo{0:Owner:c13} running\n",
            ("shell", "pm", "list", "packages", "-U", "--user", "0"): "".join(
                f"package:{package} uid:{uid}\n" for package, uid in self.uids.items()
            ),
            ("shell", "pm", "list", "instrumentation"): (
                f"instrumentation:{COMPONENT} (target={SERVER})\n"
            ),
            APPOPS: "android:mock_location: allow\n",
            STORE: json.dumps([SETTINGS, "com.example.other"]) + "\n",
            DEFAULT_IME: "com.android.inputmethod.latin/.LatinIME\n",
            ENABLED_IMES: "com.android.inputmethod.latin/.LatinIME\n",
            SERVICES: EMPTY_SERVICES,
        }
        for package in PACKAGES:
            self.replies[("shell", "pm", "path", package)] = (
                f"package:{self.paths[package]}\n"
            )
            self.replies[("shell", "sha256sum", self.paths[package])] = (
                f"{self.expected[package]}  {self.paths[package]}\n"
            )
        # PID drift is still answered from an exact allowlist, never a catch-all.
        for pid in (3210, 3211):
            self.replies[("shell", "cat", f"/proc/{pid}/stat")] = proc_stat(pid)
            self.replies[("shell", "cat", f"/proc/{pid}/status")] = (
                f"Name:\tserver\nPid:\t{pid}\nUid:\t10102\t10102\t10102\t10102\n"
            )
            self.replies[("shell", "cat", f"/proc/{pid}/cmdline")] = SERVER + "\0"
        self.replies[("shell", "pidof", SERVER)] = "3210\n"
        self.driver = SimpleNamespace(
            session_id=HOST_SESSION_ID,
            capabilities={},
            get_settings=Mock(return_value={"waitForIdleTimeout": 1000}),
            quit=Mock(),
        )
        self.idle()

    def __enter__(self) -> Self:
        # Install all boundaries before acquiring the synthetic phone lease.
        self.sdk = self.stack.enter_context(automation_sdk(self.driver))
        self.sdk.spawn.side_effect = self.spawn_appium
        original_kill = self.sdk.kill.side_effect

        def kill_owned(pid: int, sig: int) -> None:
            assert pid == self.sdk.process.pid, "refuse a foreign process group"
            original_kill(pid, sig)

        self.sdk.kill.side_effect = kill_owned
        self.command = self.stack.enter_context(
            patch.object(android_sdk.subprocess, "run", side_effect=self.sdk_command)
        )
        self.stack.enter_context(
            patch.object(android_sdk, "adb_executable", return_value=self.adb)
        )
        self.stack.enter_context(
            patch("time.monotonic", side_effect=lambda: self.clock)
        )
        # Overlay AFTER automation_sdk: its generic ready response must not swallow
        # direct device /sessions and accidentally prove a nonexistent session.
        self.connection = self.stack.enter_context(
            patch.object(
                mobile_appium.socket, "create_connection", side_effect=self.connect
            )
        )
        self.phone.acquire()
        self.journal = self.phone.path.read_bytes()
        self.identity = self.stack.enter_context(
            patch.object(
                android_sdk, "read_app_identity", wraps=android_sdk.read_app_identity
            )
        )
        return self

    def __exit__(self, *args: object) -> None:
        self.stack.close()

    def idle(self) -> None:
        self.replies[PS] = "PID UID ARGS\n1 0 /system/bin/init\n"
        self.replies[OS_PROCESSES] = processes()
        self.baseline_forward = f"{self.serial} tcp:19000 tcp:9000\n"
        self.replies[FORWARDS] = self.baseline_forward
        self.replies[GLOBAL_SETTINGS] = self.global_baseline
        for policy in POLICIES:
            self.replies[("shell", "settings", "get", "global", policy)] = "null\n"
        self.device_sessions = []

    def active(self) -> None:
        self.replies[PS] += f"3210 {self.uids[SERVER]} {SERVER}\n"
        self.replies[OS_PROCESSES] = active_processes()
        self.replies[FORWARDS] = (
            self.baseline_forward + f"{self.serial} tcp:{self.system_port} tcp:6790\n"
        )
        self.replies[GLOBAL_SETTINGS] = self.global_baseline + "".join(
            f"{policy}=1\n" for policy in POLICIES
        )
        for policy in POLICIES:
            self.replies[("shell", "settings", "get", "global", policy)] = "1\n"
        self.device_sessions = [{"id": DEVICE_SESSION_ID, "capabilities": {}}]

    def spawn_appium(self, command: list[str], **kwargs: object) -> Mock:
        assert len(command) in (10, 12), "only the managed Appium process may spawn"
        assert command[:6] == [
            "synthetic-node",
            str(self.root / "nonexistent-appium.js"),
            "--address",
            "127.0.0.1",
            "--port",
            str(self.host_port),
        ]
        assert command[6] == "--base-path"
        assert command[8:] in (
            ["--log-level", "info"],
            ["--log-level", "debug", "--log-timestamp", "--log-no-colors"],
        )
        base_path = command[7]
        assert base_path.startswith("/blehub-")
        assert str(
            uuid.UUID(base_path.removeprefix("/blehub-"))
        ) == base_path.removeprefix("/blehub-")
        assert kwargs["cwd"] == self.root
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["start_new_session"] is True
        assert kwargs["env"]["APPIUM_HOME"] == str(self.appium_home.absolute())
        self.host_base_path = base_path
        return self.sdk.process

    def connect(
        self, address: tuple[str, int], *, timeout: float
    ) -> _ReadSocket | _AdbReadSocket:
        assert address[0] == "127.0.0.1", "network access forbidden"
        if address[1] == 5037:
            assert self.adb_responses is not None, "ADB smart socket was not opted in"
            assert 0 < timeout <= 3
            scripts = [
                [b"host:version"],
                [f"host-serial:{self.serial}:get-state".encode("ascii")],
                [
                    f"host:transport:{self.serial}".encode("ascii"),
                    b"shell:cat /proc/sys/kernel/random/boot_id",
                ],
            ]
            assert len(self.adb_sockets) < 3, "ADB retry/replay forbidden"
            connection = _AdbReadSocket(self, scripts[len(self.adb_sockets)])
            self.adb_sockets.append(connection)
            if self.adb_after_io:
                self.adb_after_io("connect")
            return connection
        assert address[1] in (self.system_port, self.host_port)
        assert math.isfinite(timeout) and 0 < timeout <= 5
        return _ReadSocket(self, address[1])

    def sdk_command(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert command[:3] == [str(self.adb), "-s", self.serial], command
        args = tuple(command[3:])
        assert args in self.replies, f"unexpected/mutating SDK command: {command}"
        assert kwargs["cwd"] == self.root
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert not kwargs.get("shell", False)
        timeout = kwargs["timeout"]
        assert type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
        self.calls.append(args)
        reply = self.replies[args]
        if self.after_command is not None:
            self.after_command(args)
        self.clock += 0.001
        return subprocess.CompletedProcess(
            command, self.returncodes.get(args, 0), reply
        )

    def attached_driver(self) -> SimpleNamespace:
        """Use the real factory's prepared profile, including its explicit port."""
        return create_android_driver(
            f"http://127.0.0.1:{self.host_port}",
            device=self.serial,
            package=self.app_id,
            activity=".MainActivity",
            auto_launch=False,
            command_timeout=5.0,
            system_port=self.system_port,
        )

    def assert_phone_retained(self) -> None:
        self.phone.require_owner()
        assert self.phone.lock.exists() and not self.phone.finished
        assert self.phone.document["actions"] == []
        assert self.phone.path.read_bytes() == self.journal
